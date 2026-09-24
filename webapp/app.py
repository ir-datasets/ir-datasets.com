"""A dynamic ir-datasets.com, built directly on ``ir_datasets.v2``.

Unlike the static generator (``../generate.py``), this renders every page
per-request from the live v2 graph. It's phase 4 of ``../PLAN_V2_SITE.md``:
"Graph assembly from a single local manifest file + dataset/benchmark pages
... No registry/fetcher yet -- one provider, manifest read from disk." In
practice that means importing ``ir_datasets.v2`` directly (our own package is
trusted; the trust-free manifest-only path is for *other* providers, added in
a later phase) and querying its ``graph`` object -- ``Graph.edges_of``,
``referrers``, ``dependents``, ``list`` -- which is exactly the traversal API
this site needs, unmodified.

Content today: ANTIQUE, the full BEIR suite (14 headline benchmarks + a
separate CQADupStack sub-suite), and NanoBEIR -- still a small slice of the
real catalog, since each family is hand-ported. The v1->v2 manifest exporter
(plan phase 3) is what gives this the full catalog without waiting on a
hand-port of every dataset.

Run:
    pip install -r requirements.txt
    python app.py                 # http://127.0.0.1:5000
"""
import json
import sys
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, url_for

try:
    import ir_datasets.v2 as v2
except ImportError:
    # Fall back to a sibling checkout if ir_datasets isn't pip-installed in
    # this interpreter (see README.md's "pip install -e" note -- this is a
    # convenience for running the app without that step, not a replacement
    # for it in a real deployment).
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / 'ir-datasets'))
    import ir_datasets.v2 as v2

app = Flask(__name__)

#: Every node type this site knows how to render specially. A type not in
#: here (a future third-party provider's own vocabulary) still gets a page --
#: see node_generic.html -- just a plainer one.
TYPE_LABELS = {
    v2.RESOURCE: 'Resource',
    v2.TABLE: 'Table',
    v2.BENCHMARK: 'Benchmark',
    v2.SUITE: 'Suite',
}

#: Same vocabulary, plural -- for a category listing (the home page's stat
#: cards: "12 Tables", not "12 Table"), never for a single node's own badge
#: (`type_label`/`TYPE_LABELS` stays singular for that).
TYPE_LABELS_PLURAL = {
    v2.RESOURCE: 'Resources',
    v2.TABLE: 'Tables',
    v2.BENCHMARK: 'Benchmarks',
    v2.SUITE: 'Suites',
}

TEMPLATE_BY_TYPE = {
    v2.RESOURCE: 'node_resource.html',
    v2.TABLE: 'node_table.html',
    v2.BENCHMARK: 'node_benchmark.html',
    v2.SUITE: 'node_suite.html',
}


def _counts_by_type():
    counts = {t: 0 for t in TYPE_LABELS}
    for name in v2.list_datasets(discover=True):
        t = v2.graph.type_of(name)
        if t in counts:
            counts[t] += 1
    return counts


def _frozen_count(table):
    """A table's record count, from the frozen manifest ONLY.

    Deliberately never falls back to ``Table.count()`` -- for an unverified
    node (no ``freeze --verify`` run yet, which is most of the catalog before
    that CI job exists) that can fall through to actually building a
    docstore, which for something like BEIR's msmarco means downloading a
    1GB+ zip on page render. A page must never trigger that as a side effect
    of being viewed.
    """
    return table._frozen().get('count')


def _pretty_samples(frozen):
    """Frozen sample rows are canonical JSON *strings*; re-parse for display."""
    out = []
    for idx, line in sorted(frozen.get('samples', {}).items(), key=lambda kv: int(kv[0])):
        try:
            out.append((idx, json.dumps(json.loads(line), indent=2, ensure_ascii=False)))
        except (TypeError, ValueError):
            out.append((idx, line))
    return out


def _python_snippet(node):
    """A minimal, accurate usage example -- only what the library actually
    supports today (no CLI/PyTerrier/XPM-IR generators yet; see
    ../PLAN_V2_SITE.md's reuse table)."""
    name = node.qualified_name
    if node.type == v2.BENCHMARK:
        singular = {'docs': 'doc', 'queries': 'query', 'qrels': 'qrel',
                    'scoreddocs': 'scoreddoc', 'docpairs': 'docpair'}
        lines = ["import ir_datasets.v2", f"ds = ir_datasets.v2.load({name!r})"]
        for entity in v2.ENTITIES:
            table = node.edge(entity)
            if table is None:
                continue
            fields = ', '.join(table.record_type._fields)
            lines.append(f"for {singular[entity]} in ds.{entity}:")
            lines.append(f"    ...  # {singular[entity]}.[{fields}]")
        return '\n'.join(lines)
    if node.type == v2.TABLE:
        return (f"import ir_datasets.v2\n"
                f"table = ir_datasets.v2.load({name!r})\n"
                f"len(table)             # record count\n"
                f"table[0]               # first record\n"
                f"for record in table: ...")
    return f"import ir_datasets.v2\nnode = ir_datasets.v2.load({name!r})"


def _triples_of(name):
    """Every triple involving `name`, either as subject or object: ``(subject,
    kind, object, other)`` where `other` is whichever end isn't `name`, for
    linking. Merges what used to be two separate sections (outgoing "Edges"
    and incoming "Referenced by") into one table, since both are just triples
    from `name`'s point of view."""
    rows = []
    for kind, targets in v2.graph.edges_of(name).items():
        for target in targets:
            rows.append((name, kind, target, target))
    for subject, kind in v2.graph.incoming_edges(name):
        rows.append((subject, kind, name, subject))
    rows.sort(key=lambda r: (r[1], r[3]))
    return rows


def _search_names(query, limit=20):
    """Names containing `query`, cheapest match first (earliest in the name,
    then shortest name) -- substring, case-insensitive, same rule ``/browse``
    filters by. ``discover=False``: a live-search box firing on every
    keystroke can't afford hf's live Hub lookup on each one."""
    query = query.lower()
    names = [n for n in v2.list_datasets(discover=False) if query in n.lower()]
    names.sort(key=lambda n: (n.lower().index(query), len(n)))
    return names[:limit]


def _node_url(name):
    """A link to a node by qualified name -- resolvable whether or not it's
    been registered/imported yet (the route resolves lazily on request)."""
    return url_for('node', qualified=name)


def _type_label(t):
    if t is None:
        # A discover=True/known_names() entry: known to exist, not yet
        # resolved, so its type isn't known without resolving it.
        return 'unresolved'
    return TYPE_LABELS.get(t, t)


def _node_exists(name):
    """Whether a (colon-containing) string actually resolves in the graph --
    e.g. a Benchmark's ``citation`` is plain text today (see PLAN_V2_SITE.md's
    "Deferred to a later paper"), often a DBLP key with a colon in it, but not
    a graph node; don't link it as if it were one."""
    try:
        return name in v2.graph
    except KeyError:
        return False


app.jinja_env.globals['node_url'] = _node_url
app.jinja_env.globals['node_exists'] = _node_exists
app.jinja_env.globals['type_label'] = _type_label
app.jinja_env.globals['provider_of'] = lambda name: name.split(':', 1)[0]
app.jinja_env.globals['frozen_count'] = _frozen_count


@app.route('/')
def index():
    return render_template(
        'index.html', counts=_counts_by_type(),
        providers=sorted(v2.graph.providers), type_labels=TYPE_LABELS_PLURAL)


@app.route('/api/search')
def api_search():
    """JSON backing the home page's live search box. Cheap and synchronous
    (no ``discover``), so it's safe to call on every keystroke."""
    query = (request.args.get('q') or '').strip()
    if not query:
        return jsonify([])
    return jsonify([
        {'name': name, 'type': _type_label(v2.graph.type_of(name)), 'url': _node_url(name)}
        for name in _search_names(query)
    ])


@app.route('/browse')
def browse():
    type_filter = request.args.get('type') or None
    query = (request.args.get('q') or '').strip().lower()
    # discover=True also asks dynamic providers (e.g. hf) for their known-but-
    # unresolved names -- a live lookup, so it only applies when there's no
    # type filter (an unresolved name's type isn't known without resolving it).
    names = v2.list_datasets(type=type_filter, discover=type_filter is None)
    if query:
        names = [n for n in names if query in n.lower()]
    rows = [(name, v2.graph.type_of(name)) for name in names]
    return render_template(
        'browse.html', rows=rows, type_labels=TYPE_LABELS,
        type_filter=type_filter, query=query)


@app.route('/provider/<prefix>')
def provider(prefix):
    p = v2.graph.providers.get(prefix)
    if p is None:
        abort(404, f'no provider registered under prefix {prefix!r} (installed: '
                    f'{", ".join(sorted(v2.graph.providers)) or "none"})')
    manifest = p.manifest()
    # known_names() adds a dynamic provider's (e.g. hf's) known-but-unresolved
    # names -- a live lookup; its entries have no type yet (see type_of below)
    # since resolving each one just to list it would defeat the point.
    rows = [(name, p.type_of(name)) for name in sorted(p.names() | p.known_names())]
    return render_template(
        'provider.html', provider=p, prefix=prefix, rows=rows,
        manifest=manifest, type_labels=TYPE_LABELS)


@app.route('/n/<path:qualified>')
def node(qualified):
    try:
        n = v2.graph[qualified]
    except KeyError as e:
        abort(404, str(e))
    template = TEMPLATE_BY_TYPE.get(n.type, 'node_generic.html')
    frozen = n._frozen()
    triples = _triples_of(n.qualified_name)
    return render_template(
        template, node=n, frozen=frozen, triples=triples,
        type_labels=TYPE_LABELS, samples=_pretty_samples(frozen),
        snippet=_python_snippet(n))


@app.errorhandler(404)
def not_found(e):
    return render_template('404.html', message=getattr(e, 'description', 'not found')), 404


if __name__ == '__main__':
    import os
    app.run(debug=True, port=int(os.environ.get('PORT', 5000)))
