"""Read path over the materialized graph (``graph.db``, built by
``build_graph_db.py``) -- everything ``app.py`` used to get by walking
``ir_datasets.v2``'s live ``Graph`` object, now served from the pyoxigraph
store via direct triple-pattern lookups (the store is a few thousand triples;
no need for the SPARQL query engine to answer "what points at this node").

Every function here returns exactly the shape the Jinja templates already
consume (``(subject, kind, object, other)`` triples, ``(name, type)`` pairs,
...), so ``app.py``'s routes and the templates don't need to change.
"""
import json
import re

from rdf_schema import (
    FROZEN_FIELDS, RDF_TYPE, RDFS_SUBCLASS_OF, edge_iri, edge_kind, graph_name,
    meta_field, node_iri, node_name, type_iri, type_name,
)

#: Fields that were emitted once per list element (see registry.row_triples)
#: -- collect repeated (name, field) triples back into a list instead of
#: letting the last one silently win.
_LIST_FIELDS = {'sources', 'metrics'}
#: Fields whose single literal is a JSON blob of a dict, decoded back to one
#: (`samples_json` -> `samples`, `defs_json` -> `defs`).
_JSON_SUFFIX = '_json'


class NodeView:
    """A stand-in for a live ``Node`` object, built entirely from the store --
    exposes the same attributes the templates read (``qualified_name``,
    ``type``, ``metadata``, ``entity``, ``_frozen()``) without
    ever importing/resolving the real node."""

    def __init__(self, store, name, type_, metadata, frozen, entity=None):
        self.qualified_name = name
        self.type = type_
        self.metadata = metadata
        self.entity = entity
        self._frozen_row = frozen
        self._store = store

    def _frozen(self):
        return self._frozen_row

    def edge(self, kind_suffix):
        """The single target of this node's ``irds:<kind_suffix>`` edge (a
        Benchmark facet: docs/queries/qrels/scoreddocs/docpairs), or ``None``.
        Mirrors the live ``Node.edge()`` used by ``node_benchmark.html``."""
        quads = self._store.quads_for_pattern(
            node_iri(self.qualified_name), edge_iri(f'irds:{kind_suffix}'), None)
        for quad in quads:
            return node_data(self._store, node_name(quad.object))
        return None


#: type -> entity, the inverse of ``nodes.TABLE_TYPES`` -- derivable from the
#: type alone, so it doesn't need its own triple.
_ENTITY_BY_TYPE = {
    'irds:DocTable': 'docs', 'irds:QueryTable': 'queries',
    'irds:QrelTable': 'qrels', 'irds:RunTable': 'scoreddocs',
    'irds:DocPairTable': 'docpairs',
}


def node_data(store, name):
    """A ``NodeView`` built from every literal/type triple this node has, or
    ``None`` if it isn't in the store."""
    type_ = None
    metadata = {}
    frozen = {}
    found = False
    for quad in store.quads_for_pattern(node_iri(name), None, None):
        found = True
        if quad.predicate == RDF_TYPE:
            type_ = type_name(quad.object)
            continue
        field = meta_field(quad.predicate)
        if field is None:
            continue  # an edge triple -- not this node's own literal data
        value = quad.object.value
        if field.endswith(_JSON_SUFFIX):
            field, value = field[:-len(_JSON_SUFFIX)], json.loads(value)
        elif field in ('count', 'size'):
            value = int(value)
        bucket = frozen if field in FROZEN_FIELDS else metadata
        if field in _LIST_FIELDS:
            bucket.setdefault(field, []).append(value)
        else:
            bucket[field] = value
    if 'sources' in metadata:
        # One JSON literal per source (see ir_datasets.v2.sources.describe_sources);
        # triples are unordered, so each carries its own `order`.
        metadata['sources'] = sorted((json.loads(x) for x in metadata['sources']),
                                     key=lambda s: s['order'])
    if not found:
        return None
    return NodeView(store, name, type_, metadata, frozen,
                    entity=_ENTITY_BY_TYPE.get(type_))


def node_exists(store, name):
    """Whether ``name`` is a real node in the store -- ``False``, not an
    error, for a string that isn't even a well-formed node reference (e.g. a
    Benchmark's ``citation`` is plain text today, often containing spaces or
    no ``:`` at all; see ``app._node_exists``)."""
    try:
        iri = node_iri(name)
    except ValueError:
        return False
    return next(iter(store.quads_for_pattern(iri, None, None)), None) is not None


def triples_of(store, name):
    """Every edge-kind triple touching ``name``, either direction, as
    ``(subject, kind, object, other)`` -- what ``_triples_of`` used to build
    from ``graph.edges_of``/``graph.incoming_edges``."""
    self_iri = node_iri(name)
    rows = []
    for quad in store.quads_for_pattern(self_iri, None, None):
        kind = edge_kind(quad.predicate)
        if kind is None:
            continue
        target = node_name(quad.object)
        rows.append((name, kind, target, target))
    for quad in store.quads_for_pattern(None, None, self_iri):
        kind = edge_kind(quad.predicate)
        if kind is None:
            continue
        subject = node_name(quad.subject)
        rows.append((subject, kind, name, subject))
    rows.sort(key=lambda r: (r[1], r[3]))
    return rows


#: (name, type, provider) for every materialized node, sorted by name *with
#: its provider prefix ignored* (``irds:antique-docs`` sorts next to
#: ``hf:neuclir/csl``'s neighbors by "antique-docs" vs. "neuclir/csl", not
#: bucketed under "irds:"/"hf:" first) -- the prefix is real identity, not
#: noise, but a listing spanning providers reads more like "the catalog,
#: alphabetically" and less like "provider A's catalog, then provider B's"
#: this way. The full name (prefix included) breaks ties between two
#: providers' same-named node, so ordering stays deterministic. Computed
#: once per process (the store is read-only and never changes while the app
#: runs -- see build_graph_db.py/README.md's "restart after rebuild").
#: ``provider`` is the quad's own named graph (see rdf_schema.graph_for),
#: not re-derived by parsing ``name`` -- the graph *is* the provider-filter
#: partition. With ~620k CLIRMatrix-inflated nodes in the catalog,
#: re-scanning the store's rdf:type quads from scratch on every request (as
#: this used to do) cost ~0.5s each; caching the one full scan turns every
#: subsequent list/count into an in-memory filter instead.
_type_rows_cache = None


def _sort_key(row):
    name = row[0]
    return (name.split(':', 1)[-1], name)


def _all_type_rows(store):
    global _type_rows_cache
    if _type_rows_cache is None:
        _type_rows_cache = sorted(
            ((node_name(q.subject), type_name(q.object), graph_name(q.graph_name))
             for q in store.quads_for_pattern(None, RDF_TYPE, None, None)),
            key=_sort_key)
    return _type_rows_cache


def available_providers(store):
    """Every named graph (provider-filter checkbox) with at least one node --
    computed from the store, not hardcoded, so a checkbox never lags behind
    what's actually been materialized. ``irds`` sorts first (the original,
    primary provider), the rest alphabetically after it."""
    providers = {p for _, _, p in _all_type_rows(store)}
    return sorted(providers, key=lambda p: (p != 'irds', p))


def warm(store):
    """Build the type-rows cache now rather than on whichever request
    happens to be first -- with ~620k CLIRMatrix-inflated nodes in the
    catalog, that scan+sort takes ~2s; a real visitor shouldn't pay it."""
    _all_type_rows(store)


def _type_parents(store):
    return {type_name(q.subject): type_name(q.object)
            for q in store.quads_for_pattern(None, RDFS_SUBCLASS_OF, None)}


def _subtype_closure(store, qualified_type):
    """``qualified_type`` and every declared (transitive) subtype of it --
    mirrors ``vocabulary.is_subtype`` but walking the store's own
    ``subClassOf`` triples instead of the in-process vocabulary index."""
    parents = _type_parents(store)
    children = {}
    for child, parent in parents.items():
        children.setdefault(parent, []).append(child)
    out, stack = set(), [qualified_type]
    while stack:
        t = stack.pop()
        if t in out:
            continue
        out.add(t)
        stack.extend(children.get(t, []))
    return out


def list_nodes(store, type_filter=None, query=None, name_prefix=None,
              providers=None, limit=None, offset=0, regex=False):
    """``(rows, total)`` -- ``(name, type)`` pairs for materialized nodes,
    optionally restricted to a type (and its subtypes), a name substring (or,
    with ``regex=True``, a name regex -- see below), a name prefix (a
    provider's own ``"prefix:"`` namespace), and/or a set of enabled
    provider-graphs (``providers=None`` means no filtering; an empty
    set/list means nothing matches), sorted by name. ``total`` is the count
    *before* slicing by ``limit``/``offset`` -- a page's own caller needs it
    to render "N of M" / infinite-scroll state; with ~620k CLIRMatrix-
    inflated names in the catalog, a route must never hand the unsliced list
    to a template (a multi-ten-megabyte page)."""
    rows = _all_type_rows(store)
    if providers is not None:
        allowed_providers = set(providers)
        rows = [r for r in rows if r[2] in allowed_providers]
    if type_filter:
        allowed = _subtype_closure(store, type_filter)
        rows = [r for r in rows if r[1] in allowed]
    if name_prefix:
        rows = [r for r in rows if r[0].startswith(name_prefix)]
    if query:
        if regex:
            #: the query box re-searches on every keystroke (see
            #: static/browse.js), so a pattern is very often mid-edit and
            #: invalid (an unclosed group, a trailing backslash, ...) -- fall
            #: back to the same substring match as the non-regex path rather
            #: than erroring the whole listing out from under the user for
            #: what's usually just a not-yet-finished pattern.
            #: matched against the name with its "provider:" prefix stripped
            #: -- the provider is already its own filter (the Providers
            #: checkboxes / `providers=`), so a pattern like `^foo` shouldn't
            #: have to account for an arbitrary "irds:"/"hf:"/... in front of
            #: the part the user actually means to anchor against.
            try:
                pattern = re.compile(query, re.IGNORECASE)
                rows = [r for r in rows if pattern.search(r[0].split(':', 1)[-1])]
            except re.error:
                q = query.lower()
                rows = [r for r in rows if q in r[0].lower()]
        else:
            q = query.lower()
            rows = [r for r in rows if q in r[0].lower()]
    total = len(rows)
    if limit is not None:
        rows = rows[offset:offset + limit]
    return [(name, type_) for name, type_, _ in rows], total


def counts_by_type(store, categories, providers=None):
    """``{category: count}`` for each of ``categories`` (qualified types),
    each node counted under every category it's a (transitive) subtype of,
    restricted to ``providers`` the same way ``list_nodes`` is."""
    counts = {c: 0 for c in categories}
    closures = {c: _subtype_closure(store, c) for c in categories}
    allowed_providers = set(providers) if providers is not None else None
    for _, t, p in _all_type_rows(store):
        if allowed_providers is not None and p not in allowed_providers:
            continue
        for c in categories:
            if t in closures[c]:
                counts[c] += 1
    return counts
