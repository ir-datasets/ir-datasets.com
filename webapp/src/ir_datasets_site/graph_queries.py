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

from . import attestations

from .rdf_schema import (
    FROZEN_FIELDS, RDF_JSON, RDF_PROPERTY, RDF_TYPE, RDFS_COMMENT, RDFS_DOMAIN, RDFS_RANGE, RDFS_SUBCLASS_OF,
    RDFS_SUBPROPERTY_OF, edge_iri, edge_kind, graph_name,
    graph_iri, meta_field, meta_iri, node_iri, node_name, type_iri, type_name,
)

#: Properties a page shows as a list. Any property can have several values in the
#: graph (RDF has no single-valued properties); every other one a page shows as its
#: first value in a stable order, and ``NodeView.values`` keeps all of them.
_LIST_FIELDS = {'citation', 'license', 'metric', 'source', 'fileHash', 'requiresDependency',
                'column', 'relevanceLabel'}


def _value_sort_key(value):
    """A stable order for a property's values. A JSON document goes by its ``order``
    (a source, a column), else its ``label`` (a relevance label), else its contents;
    anything else sorts by its text."""
    if isinstance(value, dict):
        text = json.dumps(value, sort_keys=True)
        for tier, field in enumerate(('order', 'label')):
            if field in value:
                position = value[field]
                # numbers before text, so a mixed set never compares across types
                rank = (0, position, '') if isinstance(position, (int, float)) else (1, 0, str(position))
                return (tier, rank, text)
        return (2, (0, 0, ''), text)
    return (3, (0, 0, ''), str(value))


def _local(field):
    """``'irds:description'`` -> ``'description'``: a node's metadata is keyed by
    local name, as the templates read it."""
    if field == 'documentation:source':
        # not `source`: that is a Resource's `irds:source` (where its bytes come from)
        return 'documentationSource'
    return field.split(':', 1)[1] if ':' in field else field


class NodeView:
    """A stand-in for a live ``Node`` object, built entirely from the store --
    exposes the same attributes the templates read (``qualified_name``,
    ``type``, ``metadata``, ``entity``, ``_frozen()``) without
    ever importing/resolving the real node."""

    def __init__(self, store, name, types, metadata, frozen, entity=None, values=None):
        self.qualified_name = name
        # A node may have several types (e.g. a benchmark that is both an
        # AdhocBenchmark and a QaBenchmark); `type` is the first.
        self.types = list(types)
        self.type = self.types[0] if self.types else None
        self.metadata = metadata
        #: Every value of every property (lists, sorted); ``metadata`` is the page
        #: view of them (a list for ``_LIST_FIELDS``, else the first value).
        self.values = values if values is not None else {}
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
    values = {}
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
        field = _local(field)
        if getattr(quad.object, 'datatype', None) == RDF_JSON:
            value = json.loads(value)
        elif field in ('count', 'fileSize'):
            value = int(value)
        values.setdefault(field, []).append(value)
    if not found:
        return None
    if 'citation' in values:
        # A single literal may hold several ';'-separated citations
        # (e.g. 'dblp:conf/clef/X; dblp:conf/eacl/Y'): split them apart.
        # A BibTeX record ('@...') is kept whole.
        values['citation'] = [c.strip() for raw in values['citation']
                              for c in ([str(raw)] if str(raw).lstrip().startswith('@')
                                        else str(raw).split(';')) if c.strip()]
    # Triples are unordered: give every property's values a stable order (sources and
    # columns carry their own `order`).
    values = {field: sorted(vs, key=_value_sort_key) for field, vs in values.items()}
    metadata, frozen = {}, {}
    for field, vs in values.items():
        bucket = frozen if field in FROZEN_FIELDS else metadata
        bucket[field] = vs if field in _LIST_FIELDS else vs[0]
    types.sort()  # triples are unordered; keep the order deterministic
    return NodeView(store, name, types, metadata, frozen,
                    entity=next((_ENTITY_BY_TYPE[t] for t in types if t in _ENTITY_BY_TYPE), None),
                    values=values)


def effective_requires(store, name):
    """Every optional pip extra ``name`` needs: its own (``irds:requiresDependency`` literals)
    plus those of everything it is built from, found by walking structural
    edges (``derived_from``, a benchmark's facets, suite membership, ...) --
    the same answer ``Graph.requires`` gives from the live graph, as a tuple in
    first-seen order. ``()`` for a node that needs nothing, or isn't in the store."""
    from ir_datasets.v2.vocabulary import structural_kinds
    structural = structural_kinds()
    out, seen, stack = [], {name}, [name]
    requires_iri = meta_iri('irds:requiresDependency')
    while stack:
        current = stack.pop()
        iri = node_iri(current)
        for quad in store.quads_for_pattern(iri, requires_iri, None):
            if quad.object.value not in out:
                out.append(quad.object.value)
        for quad in store.quads_for_pattern(iri, None, None):
            if edge_kind(quad.predicate) in structural:
                target = node_name(quad.object)
                if target is not None and target not in seen:
                    seen.add(target)
                    stack.append(target)
    return tuple(out)


def property_term(qualified, range):
    """The store's IRI for a declared graph property: the standard RDF ones as
    themselves, an edge under the edge namespace, a property under the meta one."""
    if qualified == 'rdf:type':
        return RDF_TYPE
    if qualified == 'rdfs:subClassOf':
        return RDFS_SUBCLASS_OF
    if qualified == 'rdfs:range':
        return RDFS_RANGE
    if qualified == 'rdfs:domain':
        return RDFS_DOMAIN
    if qualified == 'rdfs:comment':
        return RDFS_COMMENT
    if qualified == 'rdfs:subPropertyOf':
        return RDFS_SUBPROPERTY_OF
    return edge_iri(qualified) if range == 'node' else meta_iri(qualified)


def property_counts(store, declared, providers=None):
    """``{qualified graph property: triple count}`` for each declared graph property
    (``{qualified: {'range': ...}}``), over the named graphs of ``providers``
    (every provider's if ``None``)."""
    graphs = None if providers is None else [graph_iri(p) for p in providers]
    counts = {}
    for qualified, entry in declared.items():
        term = property_term(qualified, entry['range'])
        if graphs is None:
            counts[qualified] = sum(1 for _ in store.quads_for_pattern(None, term, None))
            defined = sum(1 for _ in store.quads_for_pattern(None, term, RDF_PROPERTY)) if term == RDF_TYPE else 0
        else:
            counts[qualified] = sum(1 for g in graphs
                                    for _ in store.quads_for_pattern(None, term, None, g))
            defined = sum(1 for g in graphs for _ in store.quads_for_pattern(None, term, RDF_PROPERTY, g)
                          ) if term == RDF_TYPE else 0
        counts[qualified] -= defined          # a property describing itself is not a use of rdf:type
    return counts


def property_examples(store, qualified, range, providers=None, limit=50):
    """Up to ``limit`` ``(subject, object, object_kind)`` rows using a graph property
    -- ``object_kind`` is ``'node'``, ``'type'`` or ``'literal'`` -- in store order
    (a sample, not a sorted listing), over the named graphs of ``providers``."""
    term = property_term(qualified, range)
    graphs = [None] if providers is None else [graph_iri(p) for p in providers]
    rows = []
    for g in graphs:
        args = (None, term, None) if g is None else (None, term, None, g)
        for quad in store.quads_for_pattern(*args):
            subject = node_name(quad.subject)
            if subject is None:                      # a type's own subClassOf row
                subject = type_name(quad.subject)
            if subject is None:                      # a graph property's own description
                continue
            if quad.predicate == RDF_TYPE or quad.predicate == RDFS_SUBCLASS_OF:
                rows.append((subject, type_name(quad.object), 'type'))
            elif range == 'node':
                rows.append((subject, node_name(quad.object), 'node'))
            else:
                rows.append((subject, quad.object.value, 'literal'))
            if len(rows) >= limit:
                return rows
    return rows


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
    # A property's JSON documents come in their own ``order`` (sources, columns) or
    # ``label`` (relevance labels), not alphabetically by their text.
    rows.sort(key=lambda r: (r[0] != name, r[1] != 'rdf:type', r[1], _object_sort_key(r)))
    return rows


def _object_sort_key(row):
    """Order among one predicate's objects: JSON documents by ``_value_sort_key``,
    anything else by its text."""
    _, _, obj, kind, _ = row
    if kind == 'literal' and isinstance(obj, str) and obj.startswith('{'):
        try:
            return _value_sort_key(json.loads(obj))
        except ValueError:
            pass
    return _value_sort_key(str(obj))


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
            if node_name(q.subject) is None:         # a graph property describing itself, not a node
                continue
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
    mirrors ``vocabulary.is_subclass`` but walking the store's own
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


# -- attestations ------------------------------------------------------------

_ATTESTATION = 'attest:attestation'
_ATTESTATION_TYPE = 'attest:Attestation'
_SIGNED_BY = 'attest:signedBy'
#: More attestations than this and the site-wide overview stops loading every one of them.
MAX_OVERVIEW_ATTESTATIONS = 20000


def _identity_name(store, identity):
    data = node_data(store, identity)
    record = (data.values.get('record') or [{}])[0] if data else {}
    return record.get('username') or identity.split(':', 1)[-1][:8]


def _attestation_row(store, name):
    """One attestation node as ``attestations``' row shape, or ``None`` if it isn't one."""
    data = node_data(store, name)
    kind = next((k for k in map(attestations.kind_of, data.types) if k), None) if data else None
    if kind is None:
        return None
    record = (data.values.get('record') or [{}])[0]
    signer = None
    for quad in store.quads_for_pattern(node_iri(name), edge_iri(_SIGNED_BY), None):
        signer = _identity_name(store, node_name(quad.object))
    if kind == 'identity':
        signer = record.get('username')
    return {'id': name, 'kind': kind, 'subject': record.get('subject'), 'record': record,
            'signed': 'signature' in data.values, 'signer': signer,
            'verified': (data.values.get('verified') or ['false'])[0] == 'true'}


def attestations_of(store, name):
    """The attestations about node ``name``, as ``attestations`` rows (empty if none or if the
    store has no ``attest`` provider)."""
    rows = []
    for quad in store.quads_for_pattern(node_iri(name), edge_iri(_ATTESTATION), None):
        row = _attestation_row(store, node_name(quad.object))
        if row:
            rows.append(row)
    return rows


_all_attestations_cache = None


def all_attestations(store):
    """Every attestation in the store (identities too), as rows -- cached for the process, like
    the type rows (the store never changes while the app runs)."""
    global _all_attestations_cache
    if _all_attestations_cache is None:
        names, _ = list_nodes(store, type_filter=_ATTESTATION_TYPE, limit=MAX_OVERVIEW_ATTESTATIONS)
        _all_attestations_cache = [r for r in (_attestation_row(store, n) for n, _ in names) if r]
    return _all_attestations_cache

