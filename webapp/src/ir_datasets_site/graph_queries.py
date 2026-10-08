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

from .rdf_schema import (
    FROZEN_FIELDS, RDF_TYPE, RDFS_SUBCLASS_OF, edge_iri, edge_kind, graph_name,
    meta_field, node_iri, node_name, type_iri, type_name,
)

#: Fields that were emitted once per list element (see registry.row_triples)
#: -- collect repeated (name, field) triples back into a list instead of
#: letting the last one silently win.
_LIST_FIELDS = {'sources', 'metrics', 'citation', 'license'}
#: Fields whose single literal is a JSON blob of a dict, decoded back to one
#: (`samples_json` -> `samples`, `defs_json` -> `defs`).
_JSON_SUFFIX = '_json'


class NodeView:
    """A stand-in for a live ``Node`` object, built entirely from the store --
    exposes the same attributes the templates read (``qualified_name``,
    ``type``, ``metadata``, ``entity``, ``_frozen()``) without
    ever importing/resolving the real node."""

    def __init__(self, store, name, types, metadata, frozen, entity=None):
        self.qualified_name = name
        # A node may have several types (e.g. a benchmark that is both an
        # AdhocBenchmark and a QaBenchmark); `type` is the first.
        self.types = list(types)
        self.type = self.types[0] if self.types else None
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
    'irds:Docs': 'docs', 'irds:Queries': 'queries',
    'irds:Qrels': 'qrels', 'irds:AdhocRun': 'scoreddocs',
    'irds:DocPairs': 'docpairs', 'irds:Answers': 'answers',
}


def node_data(store, name):
    """A ``NodeView`` built from every literal/type triple this node has, or
    ``None`` if it isn't in the store."""
    types = []
    metadata = {}
    frozen = {}
    found = False
    for quad in store.quads_for_pattern(node_iri(name), None, None):
        found = True
        if quad.predicate == RDF_TYPE:
            types.append(type_name(quad.object))
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
    if 'citation' in metadata:
        # A single literal may hold several ';'-separated citations
        # (e.g. 'dblp:conf/clef/X; dblp:conf/eacl/Y'): split them apart.
        metadata['citation'] = [c.strip() for raw in metadata['citation']
                                for c in str(raw).split(';') if c.strip()]
    if 'sources' in metadata:
        # One JSON literal per source (see ir_datasets.v2.sources.describe_sources);
        # triples are unordered, so each carries its own `order`.
        metadata['sources'] = sorted((json.loads(x) for x in metadata['sources']),
                                     key=lambda s: s['order'])
    if not found:
        return None
    types.sort()  # triples are unordered; keep the order deterministic
    return NodeView(store, name, types, metadata, frozen,
                    entity=next((_ENTITY_BY_TYPE[t] for t in types if t in _ENTITY_BY_TYPE), None))


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


def raw_triples_of(store, name):
    """Every triple with ``name`` as subject (type, literals, edges alike) plus
    every edge pointing at it, as ``(subject, predicate, object, object_kind, graph)``
    where ``object_kind`` is ``'node'`` (a link), ``'type'`` or ``'literal'``."""
    self_iri = node_iri(name)
    rows = []
    for quad in store.quads_for_pattern(self_iri, None, None):
        if quad.predicate == RDF_TYPE:
            rows.append((name, 'rdf:type', type_name(quad.object), 'type', graph_name(quad.graph_name)))
        elif (kind := edge_kind(quad.predicate)) is not None:
            rows.append((name, kind, node_name(quad.object), 'node', graph_name(quad.graph_name)))
        elif (field := meta_field(quad.predicate)) is not None:
            rows.append((name, field, quad.object.value, 'literal', graph_name(quad.graph_name)))
    for quad in store.quads_for_pattern(None, None, self_iri):
        kind = edge_kind(quad.predicate)
        if kind is not None:
            rows.append((node_name(quad.subject), kind, name, 'node', graph_name(quad.graph_name)))
    # This node's own triples first, and among them rdf:type before everything else.
    rows.sort(key=lambda r: (r[0] != name, r[1] != 'rdf:type', r[1], r[2]))
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
        # One row per node, carrying every one of its types (a node may have
        # several): (name, sorted tuple of types, provider).
        grouped = {}
        for q in store.quads_for_pattern(None, RDF_TYPE, None, None):
            key = (node_name(q.subject), graph_name(q.graph_name))
            grouped.setdefault(key, set()).add(type_name(q.object))
        _type_rows_cache = sorted(
            ((name, tuple(sorted(types)), provider)
             for (name, provider), types in grouped.items()),
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
    """``(rows, total)`` -- ``(name, types)`` pairs (``types`` a tuple) for materialized nodes,
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
        # One type, or several (a node matches if it has any of them).
        wanted = [type_filter] if isinstance(type_filter, str) else list(type_filter)
        allowed = set().union(*(_subtype_closure(store, t) for t in wanted))
        rows = [r for r in rows if allowed.intersection(r[1])]
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
    return [(name, types) for name, types, _ in rows], total


def type_hierarchy(store, declared=None):
    """``(parents, children, all_types)`` over every type the store knows --
    its ``subClassOf`` triples, every ``rdf:type`` object -- plus ``declared``
    (``{type: parent or None}``, e.g. the in-process vocabulary, which also
    knows root types and types of providers whose nodes aren't materialized).
    A type with no parent is a root."""
    parents = {}
    for t, parent in (declared or {}).items():
        parents[t] = parent
    for t, parent in _type_parents(store).items():
        parents[t] = parent
    for _, types, _ in _all_type_rows(store):
        for t in types:
            parents.setdefault(t, None)
    for parent in list(parents.values()):
        if parent is not None:
            parents.setdefault(parent, None)
    children = {}
    for t, parent in parents.items():
        if parent is not None:
            children.setdefault(parent, []).append(t)
    for kids in children.values():
        kids.sort()
    return parents, children, set(parents)


def counts_per_type(store, parents, providers=None):
    """``{type: node count}`` for *every* type, each node counted under each
    of its types and every ancestor of them (so ``irds:Table``'s count is all
    tables), once per node."""
    allowed = set(providers) if providers is not None else None
    counts = {t: 0 for t in parents}
    for _, types, p in _all_type_rows(store):
        if allowed is not None and p not in allowed:
            continue
        seen = set()
        for t in types:
            while t is not None and t not in seen:
                seen.add(t)
                t = parents.get(t)
        for t in seen:
            counts[t] = counts.get(t, 0) + 1
    return counts


def counts_by_type(store, categories, providers=None):
    """``{category: count}`` for each of ``categories`` (qualified types),
    each node counted under every category it's a (transitive) subtype of,
    restricted to ``providers`` the same way ``list_nodes`` is."""
    counts = {c: 0 for c in categories}
    closures = {c: _subtype_closure(store, c) for c in categories}
    allowed_providers = set(providers) if providers is not None else None
    for _, types, p in _all_type_rows(store):
        if allowed_providers is not None and p not in allowed_providers:
            continue
        for c in categories:
            if closures[c].intersection(types):
                counts[c] += 1
    return counts
