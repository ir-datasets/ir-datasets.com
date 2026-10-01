"""A dynamic ir-datasets.com, reading a materialized snapshot of the
``ir_datasets.v2`` graph instead of walking its live Python objects.

Renders every page per-request, but the "assemble the graph" cost is paid
once, ahead of time, by ``build_graph_db.py`` (into ``graph.db``, a
pyoxigraph store) -- not on every request. That's what removes the two slow
paths the live-graph version had: a home-page/`/browse` visit no longer
triggers a live crawl of every ``ir-datasets``-tagged HuggingFace repo
(``discover=True``), and a node page no longer risks importing a Python
module just to answer "does this exist". See ``graph_queries.py`` for the
read path and ``rdf_schema.py`` for the RDF mapping ``build_graph_db.py``
writes and this reads back.

A node not in the store at all (freshest hf: repos the last snapshot missed)
falls back to a live ``ir_datasets.v2`` resolution -- see ``node()`` -- so
browsing an arbitrary hf repo still works, just not from the fast path.

Run:
    pip install -r requirements.txt
    python build_graph_db.py      # once, or whenever you want a fresh snapshot
    python app.py                 # http://127.0.0.1:5000
"""
import re
import sys
from pathlib import Path

import pyoxigraph as ox
from markupsafe import Markup, escape
from flask import Flask, abort, jsonify, render_template, request, url_for

import graph_queries as gq

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

STORE_PATH = Path(__file__).resolve().parent / 'graph.db'
try:
    store = ox.Store.read_only(str(STORE_PATH))
except OSError:
    # No build_graph_db.py run yet, or an older pyoxigraph without
    # read_only() -- either way, fall back to a plain (still never written
    # to at request time) store so the app can at least start.
    store = ox.Store(str(STORE_PATH))
gq.warm(store)

#: The top-level node categories this site knows how to render specially --
#: used for the home page's stat cards and the browse page's type filter.
#: ``irds:Table`` is itself a real (registerable) type, but every concrete
#: table node in the catalog is actually one of its five subtypes
#: (``irds:DocTable``, ``irds:QrelTable``, ...) -- see ``ir_datasets.v2.nodes``'s
#: ``TABLE_TYPES``. Grouping by category below is therefore always done via
#: ``graph_queries``'s stored ``subClassOf`` closure, never exact-``==``
#: against a node's own (more specific) type. A type not in here at all (a
#: future third-party provider's own vocabulary with no matching parent)
#: still gets a page -- see node_generic.html -- just a plainer one.
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

#: Exact-type template overrides, checked before the category-based
#: ``TEMPLATE_BY_TYPE`` lookup above -- for a type that isn't part of that
#: four-category hierarchy at all (``legacy:V1Dataset`` is its own axis, not
#: a Resource/Table/Benchmark/Suite) but still deserves its own page rather
#: than node_generic.html's raw metadata dump.
TEMPLATE_BY_EXACT_TYPE = {
    'legacy:V1Dataset': 'node_legacy.html',
}

#: One custom color per top-level category (a specific brand palette, not
#: one of Tabler's named colors), used everywhere a type shows up with its
#: own color: a badge (a node page's header, /search's and
#: /provider/<prefix>'s result rows) or the home page's stat cards -- never
#: blue, since that's already the site's link color and a blue badge next to
#: a blue link reads as one blurred-together thing. Maps to a `cat-<slug>`
#: CSS class family defined in static/style.css (Tabler has no utility
#: classes for arbitrary hex colors).
TYPE_COLOR_SLUGS = {
    v2.RESOURCE: 'resource',
    v2.TABLE: 'table',
    v2.BENCHMARK: 'benchmark',
    v2.SUITE: 'suite',
}
#: An unrecognized (future third-party) type still gets a color, just a
#: neutral one -- see _category_of.
DEFAULT_COLOR_SLUG = 'default'

_SCHEMA_FIELDS_RE = re.compile(r'\(([^)]*)\)')

#: Default/max chunk size for /api/browse's infinite scroll -- CLIRMatrix
#: alone contributes ~155k enumerated names to the catalog; sending (or
#: rendering) an unpaginated listing is a multi-ten-megabyte response, so
#: every listing is chunked, never all-at-once, however large the catalog
#: gets. Default is generous ("more things displayed by default") since a
#: JSON chunk this size is still fast; MAX bounds a crafted ?limit=.
PAGE_SIZE = 200
MAX_PAGE_SIZE = 1000

#: Provider-filter checkboxes default to every available provider-graph
#: (see rdf_schema.graph_for) EXCEPT these -- CLIRMatrix alone is ~620k of
#: the ~623k nodes in the catalog, so showing it by default would swamp
#: every other, much smaller family a first-time visitor is more likely to
#: actually want; ``legacy`` (every dataset's old v1 id -- see
#: rdf_schema.graph_for) is one alias per real node, so showing both by
#: default would double up half the catalog under a second, redundant name.
DEFAULT_HIDDEN_PROVIDERS = {'clirmatrix', 'legacy'}


def _selected_providers(scope=None):
    """The enabled provider-graphs for this request: the ``providers=``
    query param (comma-separated; may be empty, meaning none selected) if
    given at all, else a default. Distinguishing "not given" from "given but
    empty" is why this checks ``request.args`` for the key at all rather than
    just defaulting a falsy value -- a user unchecking every box is a real,
    distinct state from a fresh page load.

    ``scope``, when given, means "this is one provider's own page" (e.g.
    /provider/clirmatrix's listing) rather than the global, cross-provider
    one (the home page): it restricts which graphs even count as
    "available", and -- since visiting a provider's own page is itself an
    explicit request to see it -- its default is that whole scope, not
    ``DEFAULT_HIDDEN_PROVIDERS`` (which exists only to keep CLIRMatrix from
    swamping the *global* listing by default)."""
    available = gq.available_providers(store)
    if scope is not None:
        available = [p for p in available if p in scope]
    if 'providers' not in request.args:
        if scope is not None:
            return available
        return [p for p in available if p not in DEFAULT_HIDDEN_PROVIDERS]
    raw = request.args.get('providers') or ''
    selected = {p for p in raw.split(',') if p}
    return [p for p in available if p in selected]


def _category_of(t):
    """Which of the four top-level categories (``TYPE_LABELS``' keys) a
    node's own type falls under -- ``None`` if it isn't a (transitive)
    subtype of any of them (an unknown third-party type)."""
    for category in TYPE_LABELS:
        if v2.is_subtype(t, category):
            return category
    return None


def _type_color(t):
    """The ``cat-<slug>`` color slug for a type -- see TYPE_COLOR_SLUGS. Used
    to build a badge's ``badge-cat-*`` class and, on the home page, a stat
    card's ``text-cat-*``/``border-cat-*`` classes (all defined in
    static/style.css)."""
    return TYPE_COLOR_SLUGS.get(_category_of(t), DEFAULT_COLOR_SLUG)


def _type_badge_class(t):
    """The ``badge-cat-*`` class for a type's badge -- see _type_color."""
    return f'badge-cat-{_type_color(t)}'


def _frozen_count(table):
    """A table's record count, from the frozen row ONLY.

    Deliberately never falls back to ``Table.count()`` -- for an unverified
    node (no ``freeze --verify`` run yet) that can fall through to actually
    building a docstore, which for something like BEIR's msmarco means
    downloading a 1GB+ zip on page render. A page must never trigger that as
    a side effect of being viewed.
    """
    return table._frozen().get('count')


#: Longest cell text shown in the sample-records table; the rest is elided
#: (a web document can be megabytes).
_SAMPLE_CELL_MAX = 500


def _sample_cell(value):
    import json
    if value is None:
        return ''
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if len(text) <= _SAMPLE_CELL_MAX:
        return text
    return f'{text[:_SAMPLE_CELL_MAX]}\u2026 [{len(text) - _SAMPLE_CELL_MAX:,} more characters]'


def _pretty_samples(frozen, fields=()):
    """Frozen sample rows are canonical JSON *strings* (keys sorted); re-parse
    into ``(columns, rows)`` for a table: columns follow the table's declared
    field order (``fields``) with any extra keys after, rows are
    ``(record index, [cell text, ...])``. Where the sampled indices skip, a
    ``(None, None)`` row marks the gap. ``([], [])`` when there are none."""
    import json
    records = []
    for idx, line in sorted(frozen.get('samples', {}).items(), key=lambda kv: int(kv[0])):
        try:
            record = json.loads(line)
        except (TypeError, ValueError):
            record = None
        records.append((idx, record if isinstance(record, dict) else {'record': line}))
    present = {k for _, r in records for k in r}
    columns = [f for f in fields if f in present]
    columns += sorted(present - set(columns))
    rows, prev = [], None
    for idx, r in records:
        if prev is not None and int(idx) - prev > 1:
            rows.append((None, None))
        rows.append((idx, [_sample_cell(r.get(c)) for c in columns]))
        prev = int(idx)
    return columns, rows


def _record_fields(node):
    """A table's column names, as reported at discovery (``columns`` on the
    node -- see ``Table.discovery_literals``), ``None`` if it had none."""
    return node.metadata.get('columns') or None


def _python_snippet(node):
    """A minimal, accurate usage example -- only what the library actually
    supports today (no CLI/PyTerrier/XPM-IR generators yet; see
    ../PLAN_V2_SITE.md's reuse table)."""
    name = node.qualified_name
    if v2.is_subtype(node.type, v2.BENCHMARK):
        singular = {'docs': 'doc', 'queries': 'query', 'qrels': 'qrel',
                    'scoreddocs': 'scoreddoc', 'docpairs': 'docpair'}
        lines = ["import ir_datasets.v2", f"ds = ir_datasets.v2.load({name!r})"]
        for entity in v2.ENTITIES:
            table = node.edge(entity)
            if table is None:
                continue
            fields = _record_fields(table)
            lines.append(f"for {singular[entity]} in ds.{entity}:")
            comment = f"  # {singular[entity]}.[{', '.join(fields)}]" if fields else ''
            lines.append(f"    ...{comment}")
        return '\n'.join(lines)
    if v2.is_subtype(node.type, v2.TABLE):
        return (f"import ir_datasets.v2\n"
                f"table = ir_datasets.v2.load({name!r})\n"
                f"len(table)  # record count\n"
                f"table[0]  # first record\n"
                f"for record in table: ...")
    if v2.is_subtype(node.type, v2.RESOURCE):
        return _resource_snippet(node)
    return f"import ir_datasets.v2\nnode = ir_datasets.v2.load({name!r})"


def _resource_kind(node):
    """``'git'``, ``'directory'`` or ``'file'``. The store has no explicit
    flag: a repo is identified by its ``repo`` field, a directory by a
    ``directory_manifest`` validation or the ``.dir`` naming convention
    (irds:gov.dir)."""
    meta = node.metadata
    if meta.get('repo'):
        return 'git'
    if ((meta.get('validation') or {}).get('type') == 'directory_manifest'
            or node.qualified_name.endswith('.dir')):
        return 'directory'
    return 'file'


def _resource_snippet(node):
    """``Resource.path()``/``.stream()`` usage, varied by how the resource is
    acquired: git repo / directory tree (no ``stream()``), manual download,
    authenticated download, or a plain downloadable file."""
    name = node.qualified_name
    meta = node.metadata
    sources = meta.get('sources') or []
    manual = [x for x in sources if x.get('kind') == 'manual']
    head = f"import ir_datasets.v2\nres = ir_datasets.v2.load({name!r})\n"
    if meta.get('repo'):
        commit = node._frozen().get('commit')
        return (head +
                f"root = res.path()  # clones/fetches the repo"
                f"{f' at commit {commit[:10]}' if commit else ''}, returns the local directory\n"
                "# a git repo is a directory tree: there is no res.stream()")
    is_dir = _resource_kind(node) == 'directory'
    manual_note = ''
    if manual:
        manual_note = "# Note: This resource cannot be downloaded automatically (see above)\n"
    if is_dir:
        return (head + manual_note +
                "root = res.path()  # root of the directory tree")
    if manual:
        return (head + manual_note +
                "res.path()  # validated on first access\n"
                "with res.stream() as f:\n"
                "    ...  # f is a file-like object")
    auth = any(x.get('auth') for x in sources)
    return (head +
            ("# requires authentication: see the source above for the credentials it expects\n"
             if auth else '') +
            "res.path()  # downloads (and validates) if needed, returns the local path\n"
            "with res.stream() as f:  # read or download\n"
            "    ...  # f is a file-like object")


def _node_url(name):
    """A link to a node by qualified name -- resolvable whether or not it's
    in the materialized store (the route falls back to a live resolution)."""
    return url_for('node', qualified=name)


def _type_label(t):
    if t is None:
        return 'unresolved'
    if t in TYPE_LABELS:
        return TYPE_LABELS[t]
    # A more specific type than the four categories above (e.g.
    # ``irds:QrelTable``, or a third-party ``ext:MyTable``) -- its own bare
    # name is already a readable label (types are named like their Python
    # class), so strip the provider prefix rather than showing the raw
    # qualified string or collapsing it to its category's generic label.
    return t.split(':', 1)[-1]


def _node_exists(name):
    """Whether a (colon-containing) string actually resolves to a node --
    e.g. a Benchmark's ``citation`` is plain text today, often a DBLP key
    with a colon in it, but not a graph node; don't link it as if it were
    one."""
    return gq.node_exists(store, name)


app.jinja_env.globals['node_url'] = _node_url
app.jinja_env.globals['node_exists'] = _node_exists
app.jinja_env.globals['type_label'] = _type_label
app.jinja_env.globals['type_badge_class'] = _type_badge_class
app.jinja_env.globals['type_color'] = _type_color
def _node_code(name, cls=''):
    """A node's qualified name as ``<code>``, its ``provider:`` prefix in
    light gray (matching the node page header and the search/provider rows).
    Never wraps internally (``text-nowrap``): a name is one unit, so a line
    break falls between names, not inside one."""
    cls = f'{cls} text-nowrap'.strip()
    prefix, sep, rest = str(name).partition(':')
    if not sep:
        prefix, rest = '', str(name)
    attr = f' class="{escape(cls)}"' if cls else ''
    gray = f'<span class="text-secondary">{escape(prefix)}:</span>' if sep else ''
    return Markup(f'<code{attr}>{gray}{escape(rest)}</code>')


app.jinja_env.globals['node_code'] = _node_code
app.jinja_env.globals['provider_of'] = lambda name: name.split(':', 1)[0]
def _score_rows(node, frozen):
    """Rows ``(score, meaning, count)`` for the Relevance Levels table: every
    score the table defines (``defs``) or that occurs in its frozen
    ``score_counts``, ordered numerically. ``meaning``/``count`` are None where
    the table doesn't define the score / hasn't been verified yet."""
    defs = dict(node.metadata.get('defs') or {})
    counts = dict((frozen or {}).get('score_counts') or {})

    def order(score):
        try:
            return (0, float(score), score)
        except ValueError:
            return (1, 0.0, score)
    return [(score, defs.get(score), counts.get(score))
            for score in sorted(set(defs) | set(counts), key=order)]


app.jinja_env.globals['score_rows'] = _score_rows
app.jinja_env.globals['frozen_count'] = _frozen_count


def _human_size(n):
    """Decimal byte size, e.g. 1_500_000 -> '1.5 MB'."""
    n = float(n)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1000:
            return f'{n:.0f} B' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1000
    return f'{n:.1f} TB'


app.jinja_env.filters['human_size'] = _human_size


@app.route('/')
def index():
    """A quiet landing page -- stat cards + provider list only, no filter
    bar and no results list (see search()). Clicking a stat card links to
    /search with that type pre-filtered; typing into the navbar search box
    fake-navigates there too (see static/nav.js). Stat-card counts respect
    the provider-filter default (everything except CLIRMatrix -- there are
    no checkboxes here to override it, unlike /search)."""
    counts = gq.counts_by_type(store, list(TYPE_LABELS), providers=_selected_providers())
    return render_template(
        'index.html', counts=counts, has_sidebar=False,
        # irds sorts first (the original, primary provider), same as
        # graph_queries.available_providers.
        providers=sorted(v2.graph.providers, key=lambda p: (p != 'irds', p)),
        type_labels=TYPE_LABELS_PLURAL)


@app.route('/search')
def search():
    """The filterable, infinite-scrolling catalog listing (see
    static/browse.js) -- reached by typing into the navbar search box from
    anywhere (a fake, history-API navigation -- see static/nav.js) or by a
    real link (a stat card, a provider page). Unlike the home page, this one
    has the Type/Provider filter sidebar."""
    selected = _selected_providers()
    type_filter = request.args.get('type') or ''
    query = request.args.get('q') or ''
    regex = request.args.get('regex') in ('1', 'true')
    # Server-rendered so the Filters card's own count shows on first paint,
    # not just after static/browse.js's first /api/browse response lands
    # (limit=0: this route only wants the total, not any rows).
    _, total = gq.list_nodes(store, type_filter=type_filter or None, query=query or None,
                             providers=selected, limit=0, regex=regex)
    return render_template(
        'search.html', has_sidebar=True,
        type_filter_labels=TYPE_LABELS,
        type_filter=type_filter,
        query=query,
        regex=regex,
        available_providers=gq.available_providers(store),
        selected_providers=selected,
        total=total)


@app.route('/api/browse')
def api_browse():
    """JSON backing both the home page's and /provider/<prefix>'s live,
    infinite-scrolling listings -- one endpoint, one query shape (``q``/
    ``regex``/``type``/``name_prefix``/``providers``/``offset``/``limit``),
    so there is exactly one place that knows how to page through a catalog CLIRMatrix
    alone has made ~620k rows large. ``limit`` is capped, not just defaulted,
    so a crafted request can't force one huge response. ``providers``
    (comma-separated) follows ``_selected_providers``'s own "key absent ->
    default" rule -- but when a ``prefix`` (a provider page's own listing) is
    given and ``providers`` isn't, the default scopes to that one provider
    rather than the global "everything except CLIRMatrix" default, so
    /provider/clirmatrix's own page isn't empty by default just because
    CLIRMatrix is hidden everywhere else."""
    query = (request.args.get('q') or '').strip() or None
    regex = request.args.get('regex') in ('1', 'true')
    type_filter = request.args.get('type') or None
    name_prefix = request.args.get('prefix') or None
    try:
        offset = max(0, int(request.args.get('offset', 0)))
    except ValueError:
        offset = 0
    try:
        limit = min(MAX_PAGE_SIZE, max(1, int(request.args.get('limit', PAGE_SIZE))))
    except ValueError:
        limit = PAGE_SIZE
    scope = {name_prefix.rstrip(':')} if name_prefix else None
    rows, total = gq.list_nodes(store, type_filter=type_filter, query=query,
                                name_prefix=name_prefix, providers=_selected_providers(scope=scope),
                                limit=limit, offset=offset, regex=regex)
    return jsonify({
        'rows': [{'name': name, 'type_label': _type_label(type_), 'type': type_,
                 'badge_class': _type_badge_class(type_),
                 'url': _node_url(name), 'provider': name.split(':', 1)[0],
                 'provider_url': url_for('provider', prefix=name.split(':', 1)[0])}
                for name, type_ in rows],
        'total': total, 'offset': offset, 'limit': limit,
    })


@app.route('/provider/<prefix>')
def provider(prefix):
    """No provider-filter checkboxes here (unlike the home page) -- each
    provider is now its own graph (see rdf_schema.py), so this page's own
    listing (via /api/browse's ``prefix=`` scoping) always shows exactly
    this provider's nodes regardless of the home page's global default."""
    p = v2.graph.providers.get(prefix)
    if p is None:
        abort(404, f'no provider registered under prefix {prefix!r} (installed: '
                    f'{", ".join(sorted(v2.graph.providers)) or "none"})')
    manifest = p.manifest()
    return render_template(
        'provider.html', provider=p, prefix=prefix, manifest=manifest,
        type_labels=TYPE_LABELS)


def _related(n, triples):
    """What a node's page links to, all read off its own edges. For a Table:
    the Resource(s) it's parsed from, any Table it's derived from (plus what
    filters it), and the Benchmarks that use it. For a Benchmark: the Suites
    it belongs to. For a Suite: its member Benchmarks and nested Suites."""
    name = n.qualified_name
    out = {'sources': [], 'derived_from': [], 'filtered_by': [], 'used_by': [],
           'suites': [], 'member_benchmarks': [], 'member_suites': [], 'members': []}
    facet_kinds = {f'irds:{e}' for e in v2.ENTITIES}
    for subject, kind, obj, other in triples:
        if subject == name and kind == 'irds:derived_from':
            target = gq.node_data(store, obj)
            is_resource = target is not None and v2.is_subtype(target.type, v2.RESOURCE)
            out['sources' if is_resource else 'derived_from'].append(obj)
        elif subject == name and kind == 'irds:filtered_by':
            out['filtered_by'].append(obj)
        elif obj == name and kind in facet_kinds:
            out['used_by'].append((subject, kind.split(':', 1)[1]))
        elif obj == name and kind == 'irds:member':
            out['suites'].append(subject)
        elif subject == name and kind == 'irds:member':
            target = gq.node_data(store, obj)
            is_suite = target is not None and v2.is_subtype(target.type, v2.SUITE)
            out['member_suites' if is_suite else 'member_benchmarks'].append(obj)
            out['members'].append({'name': obj, 'type': target.type if target else None})
    # A table's sources are auto-downloadable unless any needs a manual download.
    out['sources_manual'] = any(
        x.get('kind') == 'manual'
        for src in out['sources'] for x in (getattr(gq.node_data(store, src), 'metadata', None) or {}).get('sources') or [])
    return out


@app.route('/n/<path:qualified>')
def node(qualified):
    n = gq.node_data(store, qualified)
    triples = gq.triples_of(store, qualified) if n is not None else []
    raw_triples = gq.raw_triples_of(store, qualified) if n is not None else []
    if n is None:
        # Not in the materialized snapshot (most likely a fresh hf: repo the
        # last build's crawl missed, or predates it) -- resolve it live, same
        # as the fully-dynamic version of this app always did.
        try:
            n = v2.graph[qualified]
        except KeyError as e:
            abort(404, str(e))
    category = _category_of(n.type)
    template = (TEMPLATE_BY_EXACT_TYPE.get(n.type)
                or TEMPLATE_BY_TYPE.get(category, 'node_generic.html'))
    frozen = n._frozen()
    derived = []
    for subject, kind, obj, other in triples:
        if kind == 'irds:derived_from' and obj == qualified:
            o = gq.node_data(store, subject)
            derived.append({'name': subject, 'type': o.type if o else None})
    return render_template(
        template, node=n, frozen=frozen, triples=triples, raw_triples=raw_triples,
        derived=derived,
        type_labels=TYPE_LABELS, sample_table=_pretty_samples(frozen, _record_fields(n) or ()),
        snippet=_python_snippet(n), record_fields=_record_fields(n), related=_related(n, triples),
        resource_kind=_resource_kind(n) if v2.is_subtype(n.type, v2.RESOURCE) else None)


@app.after_request
def _no_sniff(response):
    # Node names can end in data extensions (``.jsonl``, ``.json``, ``.tsv``);
    # without this, Firefox picks its JSON viewer from the URL's extension
    # even though the response is text/html.
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@app.errorhandler(404)
def not_found(e):
    return render_template('404.html', message=getattr(e, 'description', 'not found')), 404


if __name__ == '__main__':
    import os
    app.run(debug=True, port=int(os.environ.get('PORT', 5000)))
