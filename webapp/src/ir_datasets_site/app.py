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

Run (see ../README.md):
    pip install -e ..                   # or: pip install "ir-datasets-site @ git+https://github.com/<org>/ir-datasets.com.git#subdirectory=webapp"
    ir-datasets-site build-graph-db     # once, or whenever you want a fresh snapshot
    ir-datasets-site serve              # http://127.0.0.1:5000
"""
import os
import re
import sys
from pathlib import Path

import pyoxigraph as ox
from markupsafe import Markup, escape
from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, url_for

from . import attestations as attest_summary
from . import graph_queries as gq
from ir_datasets.v2 import requirements as v2_requirements
from ir_datasets.v2.vocabulary import protocol_of as vocabulary_protocol_of

try:
    import ir_datasets.v2 as v2
except ImportError:
    # Fall back to a sibling checkout if ir_datasets isn't pip-installed in
    # this interpreter (see README.md's "pip install -e" note -- this is a
    # convenience for running the app without that step, not a replacement
    # for it in a real deployment).
    sys.path.insert(0, str(Path(__file__).resolve().parents[4] / 'ir-datasets'))
    import ir_datasets.v2 as v2

app = Flask(__name__)

#: Defaults to ./graph.db (relative to wherever the process is run from),
#: not a path inside this installed package -- the package directory isn't
#: guaranteed writable (site-packages) and shouldn't hold generated data
#: anyway. Override with $IR_DATASETS_SITE_STORE or ``--store`` (see cli.py).
STORE_PATH = Path(os.environ.get('IR_DATASETS_SITE_STORE', 'graph.db')).resolve()
try:
    store = ox.Store.read_only(str(STORE_PATH))
except OSError:
    # No build_graph_db.py run yet, or an older pyoxigraph without
    # read_only() -- either way, fall back to a plain (still never written
    # to at request time) store so the app can at least start.
    store = ox.Store(str(STORE_PATH))
gq.warm(store)

#: Gzipped N-Quads dump of the whole store -- a plain, standard RDF
#: serialization of the same quads ``graph.db`` holds in Oxigraph's own
#: internal (RocksDB-backed, not independently readable) format, for anyone
#: who wants the data itself rather than just browsing it. Written by
#: build_graph_db.py's ``build()`` right alongside ``graph.db`` by default
#: (see its ``default_dump_path``) -- served from here by ``graph_dump()``,
#: and copied as-is into a static build's output by static_site.py.
DUMP_PATH = STORE_PATH.with_suffix('.nq.gz')


#: The site's public address, for a page's canonical link.
CANONICAL_BASE = os.environ.get('IR_DATASETS_SITE_BASE_URL', 'https://ir-datasets.com').rstrip('/')


@app.context_processor
def _canonical_link():
    """``canonical_url`` on an entity page: its ``/id/<name>`` address, absolute."""
    if request.endpoint == 'entity':
        return {'canonical_url': CANONICAL_BASE + request.path}
    return {}


@app.context_processor
def _inject_graph_dump_available():
    """So base.html's download link (see its footer) can show up only when
    there's actually something to download -- same ``DUMP_PATH`` check
    whether this runs for a live request or (via static_site.py's direct
    ``render_template()`` calls) a static build."""
    return {'graph_dump_available': DUMP_PATH.exists()}


@app.route('/graph.nq.gz')
def graph_dump():
    """The download behind base.html's footer link -- the whole store as
    gzipped N-Quads (see DUMP_PATH). 404s with a clear hint if build-graph-db
    hasn't been run yet (or was run with --no-dump)."""
    if not DUMP_PATH.exists():
        abort(404, f'{DUMP_PATH} not found -- run `ir-datasets-site build-graph-db` '
                    f'(without --no-dump) first')
    return send_file(DUMP_PATH, mimetype='application/gzip',
                      as_attachment=True, download_name='graph.nq.gz')

#: The top-level node categories this site knows how to render specially --
#: used for the home page's stat cards and the browse page's type filter.
#: ``irds:Table`` is itself a real (registerable) type, but every concrete
#: table node in the catalog is actually one of its five subtypes
#: (``irds:Docs``, ``irds:Qrels``, ...) -- see ``ir_datasets.v2.nodes``'s
#: ``TABLE_TYPES``. Grouping by category below is therefore always done via
#: ``graph_queries``'s stored ``subClassOf`` closure, never exact-``==``
#: against a node's own (more specific) type. A type not in here at all (a
#: future third-party provider's own vocabulary with no matching parent)
#: still gets a page -- see node_generic.html -- just a plainer one.
TYPE_LABELS = {
    v2.DATA: 'Data',
    v2.TABLE: 'Table',
    v2.SUITE: 'Suite',            # before Benchmark: a Suite is a Benchmark, and the first match wins
    v2.BENCHMARK: 'Benchmark',
    v2.BUNDLE: 'Bundle',
    'attest:Attestation': 'Attestation',
}

#: Same vocabulary, plural -- for a category listing (the home page's stat
#: cards: "12 Tables", not "12 Table"), never for a single node's own badge
#: (`type_label`/`TYPE_LABELS` stays singular for that).
TYPE_LABELS_PLURAL = {
    v2.DATA: 'Data',
    v2.TABLE: 'Tables',
    v2.BUNDLE: 'Bundles',
}

TEMPLATE_BY_TYPE = {
    v2.DATA: 'node_resource.html',
    v2.TABLE: 'node_table.html',
    v2.SUITE: 'node_suite.html',
    v2.BENCHMARK: 'node_benchmark.html',
    v2.BUNDLE: 'node_bundle.html',
    'attest:Attestation': 'node_attestation.html',
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
    v2.DATA: 'resource',
    v2.TABLE: 'table',
    v2.BENCHMARK: 'bundle',
    v2.SUITE: 'bundle',
    v2.BUNDLE: 'bundle',
    'attest:Attestation': 'attestation',
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
        if v2.is_subclass(t, category):
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


def _frozen_of(node):
    """The facts a page shows beside a node's metadata: ``count`` (the author's
    declared ``count_hint``) and a git repo's ``commit``. A store node carries
    them as ``NodeView._frozen()``; a live node (the ``hf:`` fallback) is read
    directly, never materialized."""
    frozen_fn = getattr(node, '_frozen', None)
    if frozen_fn is not None:
        return frozen_fn()
    out = {}
    count_hint = getattr(node, 'count_hint', None)
    if count_hint is not None:
        out['count'] = count_hint
    commit = (getattr(node, 'metadata', None) or {}).get('commit')
    if commit:
        out['commit'] = commit
    return out


def _frozen_count(table):
    """A table's record count, from the declared count ONLY.

    Deliberately never falls back to ``Table.count()`` -- that can fall
    through to actually building a docstore, which for something like BEIR's
    msmarco means downloading a 1GB+ zip on page render. A page must never
    trigger that as a side effect of being viewed.
    """
    return _frozen_of(table).get('count')


def _provider_vocab(p):
    """What provider.html shows about a provider's declared vocabulary, read
    from the live provider (there is no manifest any more)."""
    return {'classes': dict(p.graph_classes), 'properties': dict(p.graph_properties), 'aliases': {}}


def _package_of(node):
    """The pip distribution a node comes from, as recorded in the graph (or
    computed for a live-fallback node)."""
    meta = node.metadata
    if meta.get('package'):
        return meta['package']
    if not hasattr(node, 'defined_in'):
        # A node read from the store that never recorded one -- a generated name (one of CLIRMatrix's
        # 150,000) has no per-node ``package`` triple; its provider's distribution stands for all.
        provider = v2.graph.providers.get(node.qualified_name.split(':', 1)[0])
        return getattr(provider, 'distribution', None)
    from ir_datasets.v2.vocabulary import row_for
    return row_for(node).get('package')


def _requires_for(node, qualified):
    """Every optional pip extra a node needs, its own and those of everything
    it is built from: from the store for a materialized node, from the live
    graph for the fallback."""
    if gq.node_exists(store, qualified):
        return gq.effective_requires(store, qualified)
    return v2.graph.requires(node)


#: Longest cell text shown in the sample-records table; the rest is elided
#: (a web document can be megabytes).
_SAMPLE_CELL_MAX = 200


def _sample_cell(value):
    import json
    if value is None:
        return ''
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if len(text) <= _SAMPLE_CELL_MAX:
        return text
    # Elided text stays in the page (hidden) so the "[N more characters]" link
    # can expand it in place -- see static/sample.js.
    head, rest = text[:_SAMPLE_CELL_MAX], text[_SAMPLE_CELL_MAX:]
    return Markup(
        f'{escape(head)}<span data-sample-rest hidden>{escape(rest)}</span>'
        f'<span data-sample-ellipsis>\u2026</span> '
        f'<a href="#" class="text-secondary text-nowrap" data-sample-expand>[+{len(rest):,} chars]</a>')


def _pretty_samples(frozen, fields=()):
    """Frozen sample rows are records (graph store) or canonical JSON *strings*
    (manifest rows, keys sorted); parse
    into ``(columns, rows)`` for a table: columns follow the table's declared
    field order (``fields``) with any extra keys after, rows are
    ``(record index, [cell text, ...])``. Where the sampled indices skip, a
    ``(None, None)`` row marks the gap. ``([], [])`` when there are none."""
    import json
    records = []
    for idx, line in sorted(frozen.get('samples', {}).items(), key=lambda kv: int(kv[0])):
        # A record object (graph store: samples_json holds the records
        # themselves) or its canonical JSON string (a manifest row).
        if isinstance(line, dict):
            record = line
        else:
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
    """A table's column names in order, from its ``irds:column`` documents (see
    ``Table.discovery_literals``), ``None`` if it had none."""
    return [c['name'] for c in node.metadata.get('column') or []] or None


def _is_benchmark(t):
    """Whether type ``t`` is a benchmark with facets: a ``Suite`` is a ``Benchmark`` too, but its
    members, not tables, are what it is made of."""
    return v2.is_subclass(t, v2.BENCHMARK) and not v2.is_subclass(t, v2.SUITE)


def _python_snippet(node):
    """A minimal, accurate usage example -- only what the library actually
    supports today (no CLI/PyTerrier/XPM-IR generators yet; see
    ../PLAN_V2_SITE.md's reuse table)."""
    name = node.qualified_name
    if _is_benchmark(node.type):
        singular = {'docs': 'doc', 'queries': 'query', 'qrels': 'qrel',
                    'scoreddocs': 'scoreddoc', 'docpairs': 'docpair', 'answers': 'answer'}
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
    if v2.is_subclass(node.type, v2.TABLE):
        return (f"import ir_datasets.v2\n"
                f"table = ir_datasets.v2.load({name!r})\n"
                f"len(table)  # record count\n"
                f"table[0]  # first record\n"
                f"for record in table: ...")
    if v2.is_subclass(node.type, v2.DATA):
        return _resource_snippet(node)
    return f"import ir_datasets.v2\nnode = ir_datasets.v2.load({name!r})"


def _snippet_variants(node, triples=()):
    """The Use It card's pills: ``[{'id', 'label', 'lang', 'code'}]``, the Python
    API example first. Every example uses only what ``ir_datasets.v2`` supports
    today -- there are no CLI/PyTerrier/XPM-IR examples because those don't
    resolve v2 names."""
    name = node.qualified_name
    variants = [{'id': 'python', 'label': 'Python API', 'lang': 'python', 'code': _python_snippet(node)}]
    if _is_benchmark(node.type):
        head = f"import ir_datasets.v2\nds = ir_datasets.v2.load({name!r})\n"
        tables = [(e, f'ds.{e}', _record_fields(node.edge(e))) for e in v2.ENTITIES
                  if node.edge(e) is not None]
    elif v2.is_subclass(node.type, v2.TABLE):
        head = f"import ir_datasets.v2\ntable = ir_datasets.v2.load({name!r})\n"
        tables = [(node.type.rpartition(':')[2].lower(), 'table', _record_fields(node))]
    else:
        return variants
    # Lookup by id (docs use the docstore; queries and answers are held in memory):
    # part of the Python API example.
    body = []
    for entity, expr, _ in tables:
        if entity == 'answers':
            body.append(f"answers = {expr}.lookup('QUERY_ID')  # that query's answers")
        elif entity in ('docs', 'queries'):
            one = 'doc' if entity == 'docs' else 'query'
            body.append(f"{one} = {expr}.lookup('ID')  # one record")
            body.append(f"{entity}_by_id = {expr}.lookup(['ID1', 'ID2'])  # a dict of records")
    if body:
        variants[0]['code'] += '\n\n# look records up by id\n' + '\n'.join(body)
    # Pandas: a table's records are named tuples, so a DataFrame takes them directly;
    # part of the Python API example (the table or ``ds`` is already loaded above).
    frames = [f"{e}_df = pd.DataFrame({expr})" + (f"  # columns: {', '.join(fields)}" if fields else '')
              for e, expr, fields in tables]
    if frames:
        variants[0]['code'] += '\n\n# as a pandas DataFrame\nimport pandas as pd\n' + '\n'.join(frames)
    # Evaluation: qrels as ir_measures input
    if any(e == 'qrels' for e, _, _ in tables):
        # a qrels table page loads the table as ``qrels``, not the generic ``table``
        qrels, evaluate_head = ('ds.qrels', head) if _is_benchmark(node.type) \
            else ('qrels', head.replace('table = ', 'qrels = ', 1))
        variants.append({'id': 'evaluate', 'label': 'Evaluate', 'lang': 'python', 'code':
            "import ir_datasets.v2\nimport ir_measures\nfrom ir_measures import nDCG, P\n"
            + evaluate_head.split('\n', 1)[1] +
            "run = ...  # your system's output: e.g., a DataFrame with query_id, doc_id, score\n"
            f"print(ir_measures.calc_aggregate([nDCG@10, P(rel=1)@10], {qrels}, run))"})
    # PyTerrier: indexing a docs table (the records are read straight from the v2 loader)
    pyterrier = _pyterrier_examples(node)
    if pyterrier:
        variants.append({'id': 'pyterrier', 'label': 'PyTerrier', 'lang': 'python', 'examples': pyterrier})
    xpm = _xpmir_snippet(node, tables, triples)
    if xpm:
        variants.append({'id': 'xpmir', 'label': 'XPM-IR', 'lang': 'python', 'code': xpm})
    return variants


#: Docs columns that are text by type but not something to search.
_NON_TEXT_COLUMNS = {'doc_id', 'marked_up_text', 'source_xml', 'msmarco_document_id'}


#: ISO 639-1 language -> the Snowball stemmer ``pt.terrier.TerrierStemmer`` has for it.
_TERRIER_STEMMERS = {'da': 'danish', 'fi': 'finnish', 'de': 'german', 'hu': 'hungarian',
                     'no': 'norwegian', 'nb': 'norwegian', 'nn': 'norwegian',
                     'pt': 'portugese', 'es': 'spanish', 'sv': 'swedish', 'tr': 'turkish'}


def _pyterrier_examples(node):
    """Ways to index a Docs table with PyTerrier -- ``[{'id', 'label', 'code'}]``, each
    reading the records straight from the v2 loader (one dict per record, its text
    columns as the indexed fields). ``None`` unless ``node`` is a Docs table."""
    if not v2.is_subclass(node.type, v2.TABLE) or node.type.rpartition(':')[2].lower() != 'docs':
        return None
    columns = node.metadata.get('column') or []
    fields = [c['name'] for c in columns
              if c.get('data_type') == 'str' and c['name'] not in _NON_TEXT_COLUMNS] or ['text']
    entries = ', '.join(["'docno': doc.doc_id"] + [f"{f!r}: doc.{f}" for f in fields])
    records = f"{{{entries}}} for doc in docs"
    lang = node.metadata.get('lang') or ''
    foreign = bool(lang) and lang != 'en'
    name = node.qualified_name.split(':', 1)[-1].replace('/', '_')
    load = ["import pyterrier as pt", "import ir_datasets.v2",
            f"docs = ir_datasets.v2.load({node.qualified_name!r})"]
    stemmer = _TERRIER_STEMMERS.get(lang.split('-')[0].lower()) if foreign else None

    # -- Terrier
    terrier = load + [f"index = pt.terrier.TerrierIndex('{name}.terrier')"]
    if foreign:
        # TerrierIndex.index() uses the English analysis; indexer() takes it as arguments
        if stemmer:
            terrier.append(f"# the text is {lang}: use the UTF tokeniser with Terrier's {stemmer.capitalize()} "
                           "stemmer (its stopword list is English, so none)")
        else:
            terrier.append(f"# the text is {lang}, which Terrier has no stemmer for: use the UTF "
                           "tokeniser, no stemming, no stopwords")
        terrier += ["indexer = index.indexer(",
                    f"    text_attrs={fields!r},",
                    "    tokeniser=pt.terrier.TerrierTokeniser.utf,",
                    f"    stemmer=pt.terrier.TerrierStemmer.{stemmer or 'none'},",
                    "    stopwords=pt.terrier.TerrierStopwords.none)",
                    f"indexer.index({records})"]
    else:
        terrier.append(f"index.index({records})")
    terrier.append("# afterwards, index.bm25() is a BM25 retriever over it")

    # -- PISA
    pisa = ["from pyterrier_pisa import PisaIndex", "import ir_datasets.v2",
            f"docs = ir_datasets.v2.load({node.qualified_name!r})"]
    options = []   # every str field is indexed by default
    if foreign:
        pisa.append(f"# the text is {lang}: PISA's stemmers and stopword list are English, so use neither. You may want to do additional pre-processing.")
        options += ["stemmer='none'", "stops='none'"]
    pisa += [f"index = PisaIndex({', '.join([repr(name + '.pisa')] + options)})",
             f"index.index({records})",
             "# afterwards, index.bm25() is a BM25 retriever over it"]
    return [{'id': 'terrier', 'label': 'Build a Terrier index', 'code': '\n'.join(terrier)},
            {'id': 'pisa', 'label': 'Build a PISA index', 'code': '\n'.join(pisa)}]


def _v1_id(name, triples):
    """The v1 id of a benchmark: the ``legacy:`` node that is an alias of it."""
    for subject, kind, obj, other in triples:
        if kind == 'irds:aliasOf' and obj == name and subject.startswith('legacy:'):
            return subject.partition(':')[2]
    return None


def _xpmir_snippet(node, tables, triples):
    """experimaestro-ir (datamaestro) addresses a dataset by its v1 id with dots
    (``irds.antique.test``), so only a benchmark that still has one gets an example."""
    if not _is_benchmark(node.type):
        return None
    v1 = _v1_id(node.qualified_name, triples)
    if v1 is None:
        return None
    dm = 'irds.' + v1.replace('/', '.')
    lines = ["# requires experimaestro-ir: pip install experimaestro-ir",
             "from datamaestro import prepare_dataset"]
    for entity, _, _ in tables:
        if entity == 'docs':
            lines += [f"documents = prepare_dataset('{dm}')",
                      "for doc in documents.iter_documents(): ...  # an AdhocDocumentStore"]
        elif entity == 'queries':
            lines += [f"topics = prepare_dataset('{dm}.queries')  # AdhocTopics",
                      "for topic in topics.iter(): ..."]
        elif entity == 'qrels':
            lines += [f"qrels = prepare_dataset('{dm}.qrels')  # AdhocAssessments",
                      "for topic_qrels in qrels.iter(): ..."]
        elif entity == 'scoreddocs':
            lines += [f"run = prepare_dataset('{dm}.scoreddocs')  # AdhocRun"]
        elif entity == 'docpairs':
            lines += [f"docpairs = prepare_dataset('{dm}.docpairs')  # TrainingTriplets",
                      "next(docpairs.iter())  # the first triplet"]
    return '\n'.join(lines) if len(lines) > 2 else None


def _snippet_config(node):
    """What the Use It card's dropdowns need to rebuild a benchmark's snippet
    client-side (``static/use_it.js``): per facet that has alternatives, the
    default table and each alternative with its record fields. ``None`` when
    the node isn't a benchmark or nothing has alternatives."""
    if not _is_benchmark(node.type):
        return None
    singular = {'docs': 'doc', 'queries': 'query', 'qrels': 'qrel',
                'scoreddocs': 'scoreddoc', 'docpairs': 'docpair', 'answers': 'answer'}
    facets, any_alts = [], False
    for entity in v2.ENTITIES:
        table = node.edge(entity)
        if table is None:
            continue
        alts = _alternatives(table.qualified_name)
        any_alts |= bool(alts)
        options = [{'name': table.qualified_name, 'fields': _record_fields(table) or [], 'note': None}]
        for alt in alts:
            view = gq.node_data(store, alt)
            options.append({'name': alt, 'fields': (_record_fields(view) if view else None) or [],
                            'note': _alt_note(alt)})
        facets.append({'entity': entity, 'singular': singular[entity], 'options': options})
    return {'name': node.qualified_name, 'facets': facets} if any_alts else None


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
    sources = meta.get('source') or []
    manual = [x for x in sources if x.get('type') == 'manual']
    head = f"import ir_datasets.v2\nres = ir_datasets.v2.load({name!r})\n"
    if meta.get('repo'):
        commit = _frozen_of(node).get('commit')
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
    return url_for('entity', name=name)


def _type_label(t):
    if t is None:
        return 'unresolved'
    if t in TYPE_LABELS:
        return TYPE_LABELS[t]
    # A more specific type than the four categories above (e.g.
    # ``irds:Qrels``, or a third-party ``ext:MyTable``) -- its own bare
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


_RST_CODE = re.compile(r'``(.+?)``')


def _inline_code(text):
    """Escape ``text`` and turn reStructuredText ``literals`` (as docstrings
    write them) into ``<code>``."""
    return Markup(_RST_CODE.sub(lambda m: f'<code>{m.group(1)}</code>', str(escape(text or ''))))


app.jinja_env.filters['inline_code'] = _inline_code


def _node_types(node):
    """Every type of a node (a live ``Node``'s or a store ``NodeView``'s):
    ``node.types`` when it has several, else just ``[node.type]``."""
    return list(getattr(node, 'types', None) or [node.type])


app.jinja_env.globals['node_types'] = _node_types


def _host(url):
    from urllib.parse import urlparse
    try:
        return (urlparse(url or '').hostname or '').lower()
    except ValueError:
        return ''


def _is_google_drive(url):
    """Whether a source URL is on Google Drive -- told from the host, not a source type."""
    return _host(url) == 'drive.google.com'


def _is_irds_mirror(url):
    """Whether a source URL is on the ir-datasets community mirror -- told from the host."""
    from ir_datasets.v2.sources import IRDS_MIRROR
    return bool(_host(url)) and _host(url) == _host(IRDS_MIRROR)


app.jinja_env.globals['is_google_drive'] = _is_google_drive
app.jinja_env.globals['is_irds_mirror'] = _is_irds_mirror
app.jinja_env.globals['type_badge_class'] = _type_badge_class


def _node_badge(name):
    """A small inline type badge for a node, by qualified name (its real type, e.g. ``File``)."""
    d = gq.node_data(store, name)
    t = (d or v2.graph[name]).type
    return Markup(f'<span class="badge badge-xs align-middle {_type_badge_class(t)}" style="padding-bottom: calc(.15em + 1px); position: relative; top: -1px">{escape(_type_label(t))}</span>')


app.jinja_env.globals['node_badge'] = _node_badge
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
    """Rows ``(score, meaning, count)`` for the Relevance Labels table: every
    score the table defines (``defs``) or that occurs in its frozen
    ``score_counts``, ordered numerically. ``meaning``/``count`` are None where
    the table doesn't define the score / hasn't been verified yet."""
    defs = {str(d['label']): d['meaning'] for d in node.metadata.get('relevanceLabel') or []}
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
    counts = gq.counts_by_type(store, list(TYPE_LABELS_PLURAL), providers=_selected_providers())
    return render_template(
        'index.html', counts=counts, has_sidebar=False,
        # irds sorts first (the original, primary provider), same as
        # graph_queries.available_providers.
        providers=sorted(v2.graph.providers, key=lambda p: (p != 'irds', p)),
        type_labels=TYPE_LABELS_PLURAL)


def _type_args():
    """The selected type filter(s): repeated ``?type=`` params (also accepting
    comma-separated values), in order, no duplicates."""
    out = []
    for value in request.args.getlist('type'):
        for t in value.split(','):
            if t and t not in out:
                out.append(t)
    return out


def _ordered_types(parents, children):
    """Every known type, depth-first: the core roots first (Resource, Table,
    Bundle), then every other root, each followed by its subtypes."""
    first = [v2.DATA, v2.TABLE, v2.BUNDLE]
    roots = [t for t in first if t in parents] + sorted(
        t for t, parent in parents.items() if parent is None and t not in first)
    out = []

    def walk(t):
        out.append(t)
        for c in children.get(t, []):
            walk(c)
    for t in roots:
        walk(t)
    return out


@app.route('/search')
def search():
    """The filterable, infinite-scrolling catalog listing (see
    static/browse.js) -- reached by typing into the navbar search box from
    anywhere (a fake, history-API navigation -- see static/nav.js) or by a
    real link (a stat card, a provider page). Unlike the home page, this one
    has the Type/Provider filter sidebar."""
    selected = _selected_providers()
    selected_types = _type_args()
    type_filter = selected_types[0] if selected_types else ''
    query = request.args.get('q') or ''
    regex = request.args.get('regex') in ('1', 'true')
    parents, children, _ = _type_graph()
    # Every known type, as options for the Type tags input (value, label, badge classes).
    type_options = [{'value': t, 'label': _type_label(t), 'badge_class': _type_badge_class(t),
                     'desc': _type_description(t) or ''}
                    for t in _ordered_types(parents, children)]
    # Server-rendered so the Filters card's own count shows on first paint,
    # not just after static/browse.js's first /api/browse response lands
    # (limit=0: this route only wants the total, not any rows).
    _, total = gq.list_nodes(store, type_filter=selected_types or None, query=query or None,
                             providers=selected, limit=0, regex=regex)
    return render_template(
        'search.html', has_sidebar=True,
        type_filter_labels=TYPE_LABELS,
        type_filter=type_filter, selected_types=selected_types, type_options=type_options,
        query=query,
        regex=regex,
        available_providers=gq.available_providers(store),
        selected_providers=selected,
        total=total,
        # Always None for a live request -- only static_site.py's build()
        # passes real rows (pre-rendered inline, no JS/AJAX; see
        # search.html's "{% if rows is not none %}" branch).
        rows=None, static_search_url=None)


def _row_dict(name, types):
    """One /api/browse row's JSON shape -- also reused, unchanged, by
    static_site.py's build() to pre-render the same rows inline (no live
    /api/browse call possible from a static export)."""
    types = list(types)
    return {'name': name, 'type_label': _type_label(types[0]), 'type': types[0],
            'badge_class': _type_badge_class(types[0]),
            # Every type the node has (usually one) -- one badge each.
            'badges': [{'label': _type_label(t), 'badge_class': _type_badge_class(t)}
                       for t in types],
            'url': _node_url(name), 'provider': name.split(':', 1)[0],
            'provider_url': url_for('provider', prefix=name.split(':', 1)[0])}


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
    type_filter = _type_args() or None
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
        'rows': [_row_dict(name, types) for name, types in rows],
        'total': total, 'offset': offset, 'limit': limit,
    })


def _type_description(t):
    """The ``desc`` a provider declared for node type ``t`` (``None`` if none, or
    its provider isn't installed) -- not stored in the graph, so read from the
    in-process vocabulary."""
    return (v2.graph_classes().get(t) or {}).get('desc')


def _type_graph():
    """``(parents, children, counts)`` for every known type -- shared by
    the overview and each type's own page."""
    declared = {t: info.get('parent') for t, info in v2.graph_classes().items()}
    parents, children, _ = gq.type_hierarchy(store, declared)
    return parents, children, gq.counts_per_type(store, parents, providers=_selected_providers())


def _type_entry(t, parents, children, counts):
    return {'name': t, 'label': _type_label(t), 'badge_class': _type_badge_class(t), 'color': _type_color(t),
            'desc': _type_description(t), 'count': counts.get(t, 0),
            'url': _node_url(t),
            'children': [_type_entry(c, parents, children, counts) for c in children.get(t, [])]}


@app.route('/classes')
def classes():
    """Every graph class, as a tree (a type under its parent), each with its
    short description and how many nodes have it (counting subtypes)."""
    parents, children, counts = _type_graph()
    roots = [t for t in _ordered_types(parents, children) if parents.get(t) is None]
    return render_template('classes.html', roots=[_type_entry(t, parents, children, counts) for t in roots])


def _property_entries():
    """Every declared graph property, as listing rows: ``{'name', 'owner', 'local',
    'range', 'structural', 'desc', 'count', 'url'}``. Declarations come
    from the live ``ir_datasets.v2`` vocabulary; counts from the store."""
    declared = v2.graph_properties()
    counts = gq.property_counts(store, declared, providers=_selected_providers())
    return [{'name': q, 'owner': e['owner'], 'local': q.split(':', 1)[1], 'range': e['range'],
             'structural': e['structural'], 'desc': e['desc'], 'parents': list(e.get('parents', ())),
             'domain': e.get('domain'), 'range_type': e.get('range_type'),
             'count': counts.get(q, 0), 'url': _node_url(q)}
            for q, e in sorted(declared.items())]


def _property_tree(entries):
    """The properties as a hierarchy: each under its ``rdfs:subPropertyOf`` parent(s) -- one
    that has two parents is listed under both. Roots are those with no declared parent;
    the standard RDF/RDFS ones come last."""
    by_name = {e['name']: e for e in entries}
    children = {}
    for e in entries:
        for parent in e['parents']:
            if parent in by_name:
                children.setdefault(parent, []).append(e['name'])

    def build(name, trail=()):
        return dict(by_name[name], children=[build(c, (*trail, name)) for c in children.get(name, [])
                                             if c not in trail])
    roots = [e['name'] for e in entries if not any(p in by_name for p in e['parents'])]
    roots.sort(key=lambda n: (by_name[n]['owner'] in ('rdf', 'rdfs'), 0 if n in children else 1, n))
    return [build(n) for n in roots]


@app.route('/properties')
def properties():
    """Every declared graph property -- edges between nodes and literal-valued
    properties -- as a hierarchy (a property under the ``rdfs:subPropertyOf`` it
    specializes), each with its description and how many triples use it."""
    return render_template('properties.html', roots=_property_tree(_property_entries()))


#: How many of a graph property's triples its own page lists inline.
PROPERTY_PAGE_ROWS = 50


def property_page(qualified):
    entries = {e['name']: e for e in _property_entries()}
    entry = entries.get(qualified)
    if entry is None:
        abort(404, f'{qualified!r} is not a declared graph property')
    rows = gq.property_examples(store, qualified, entry['range'], providers=_selected_providers(),
                                 limit=PROPERTY_PAGE_ROWS)
    subs = sorted(n for n, e in entries.items() if qualified in e['parents'])
    return render_template('property.html', entry=entry, rows=rows, shown=len(rows), subs=subs)


def _protocol_info(t):
    """What node type ``t`` promises, for its type page: the protocol declared on
    it or its closest ancestor (``irds.node_type(..., protocol=...)``), as
    ``{'ref', 'name', 'declared_on', 'inherited', 'source', 'bases'}``, or
    ``None`` if none is declared. ``source`` is the protocol's full class
    definition -- name, base protocols, docstrings and comments included -- and
    ``bases`` lists the protocols it extends (nearest first), each with its own
    ``source``, since their members are part of the promise too. A protocol that
    can't be imported here (a third-party type's, not installed) still reports
    its reference, with no source."""
    import importlib
    import inspect
    import typing
    ref, declared_on = vocabulary_protocol_of(t)
    if ref is None:
        return None
    module, _, qualname = ref.partition(':')
    info = {'ref': ref, 'name': qualname, 'module': module, 'declared_on': declared_on,
            'inherited': declared_on != t, 'source': None, 'bases': []}
    try:
        cls = getattr(importlib.import_module(module), qualname)
        info['source'] = inspect.getsource(cls)
    except (ImportError, AttributeError, OSError, TypeError):
        return info
    for base in cls.__mro__[1:]:
        if base is typing.Protocol or not getattr(base, '_is_protocol', False):
            continue
        try:
            info['bases'].append({'name': base.__name__, 'source': inspect.getsource(base)})
        except (OSError, TypeError):
            pass
    return info


#: How many of a type's nodes its own page lists inline (the rest are one
#: click away in the filterable /search listing).
TYPE_PAGE_ROWS = 100


def class_page(qualified):
    parents, children, counts = _type_graph()
    if qualified not in parents:
        abort(404, f'{qualified!r} is not a known node type')
    ancestors, cur = [], parents.get(qualified)
    while cur is not None and cur not in ancestors:
        ancestors.append(cur)
        cur = parents.get(cur)
    rows, total = gq.list_nodes(store, type_filter=qualified, providers=_selected_providers(),
                                limit=TYPE_PAGE_ROWS)
    return render_template(
        'class.html', entry=_type_entry(qualified, parents, children, counts),
        protocol=_protocol_info(qualified),
        ancestors=[{'name': a, 'label': _type_label(a), 'url': _node_url(a)}
                   for a in reversed(ancestors)],
        rows=[_row_dict(name, ts) for name, ts in rows], total=total, shown=len(rows))


def _attestation_view(node, qualified):
    """What an attestation node's page shows: its row, how its hashes compare with the other attestations about the
    same node, and (for an identity) what it has signed. ``None`` for any other node."""
    if not v2.is_subclass(node.type, 'attest:Attestation') or not isinstance(node, gq.NodeView):
        return None
    row = gq.attestation_row(store, qualified)
    if row is None:
        return None
    view = dict(row, environment=row['record'].get('environment') or {}, comparison=[], signed_rows=[])
    if row['kind'] == 'hash' and row['subject']:
        view['comparison'] = attest_summary.compare_hash(row, gq.attestations_of(store, row['subject']))
    if row['kind'] == 'identity':
        view['signed_rows'] = gq.signed_by(store, qualified)
    return view


def _attestable(node):
    """Which kinds of attestation can be made about a node: hashes for a file or a table, a visual check
    for a table, scores for a benchmark. Empty for everything else (a directory, a suite, a bundle...)."""
    types = getattr(node, 'types', None) or [node.type]
    is_table = any(v2.is_subclass(t, v2.TABLE) for t in types)
    return (['hash'] if is_table or any(v2.is_subclass(t, v2.FILE) for t in types) else []) \
        + (['visual'] if is_table else []) \
        + (['evaluation'] if any(v2.is_subclass(t, v2.BENCHMARK) and not v2.is_subclass(t, v2.SUITE) for t in types) else [])


def _verification(node, qualified):
    """The Verification card's content for a node -- shown whenever something can be attested about it, even
    if nothing has been yet -- or ``None`` for a node it doesn't apply to."""
    can_attest = _attestable(node)
    if not can_attest:
        return None
    rows = gq.attestations_of(store, qualified) if isinstance(node, gq.NodeView) else []
    summary = attest_summary.summarize(rows, node.metadata.get('fileHash') or ())
    summary['can_attest'] = can_attest
    return summary


@app.route('/attestations')
def attestations():
    """What attestations are, the four kinds and their record shapes, how to submit one, and what has
    been attested so far."""
    rows = gq.all_attestations(store)
    return render_template('attestations.html', overview=attest_summary.overview(rows),
                           statuses=attest_summary.STATUSES)


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
    return render_template(
        'provider.html', provider=p, prefix=prefix, manifest=_provider_vocab(p),
        type_labels=TYPE_LABELS,
        # See search()'s matching comment -- only static_site.py's build()
        # passes real rows.
        rows=None)


def _alternatives(name):
    """Every table linked to ``name`` by ``irds:alternativeOf`` in either
    direction, transitively (``v2.alternatives`` over the materialized store
    rather than the live graph), excluding ``name`` itself."""
    seen, stack = {name}, [name]
    while stack:
        current = stack.pop()
        for subject, kind, obj, other in gq.triples_of(store, current):
            if kind == 'irds:alternativeOf' and other not in seen:
                seen.add(other)
                stack.append(other)
    return sorted(seen - {name})


def _alt_note(name):
    """The short ``alternative_note`` a table declares about how it differs
    from the table it is an alternative of, or ``None``."""
    view = gq.node_data(store, name)
    return view.metadata.get('alternativeNote') if view is not None else None


def _is_overview(name):
    view = gq.node_data(store, name)
    return view is not None and v2.is_subclass(view.type, v2.OVERVIEW)


def _overviews_of(name, limit=300):
    """The Overviews that describe ``name``: any Overview that contains it, directly (a
    ``bundleItem`` edge) or through what it is part of -- a table is described by the overview
    of a benchmark that uses it, a file by that of the table parsed from it. Found by walking
    structural edges *up* from the node, stopping at the first Overview on each path, so
    a bundle need list only its main parts, not every table beneath them."""
    structural = set(v2.STRUCTURAL_EDGES) | {'irds:bundleItem'}      # membership is informational but counts here
    found, seen, frontier = [], {name}, [name]
    while frontier and len(seen) < limit:
        nxt = []
        for current in frontier:
            for subject, kind, obj, _other in gq.triples_of(store, current):
                if obj != current or kind not in structural or subject in seen:
                    continue
                seen.add(subject)
                if _is_overview(subject):
                    found.append(subject)
                else:
                    nxt.append(subject)
        frontier = nxt
    return sorted(found)


def _related(n, triples):
    """What a node's page links to, all read off its own edges. For a Table:
    the Resource(s) it's parsed from, any Table it's derived from (plus what
    filters it), and the Benchmarks that use it. For a Benchmark: the Suites
    it belongs to. For a Suite: its member Benchmarks and nested Suites."""
    name = n.qualified_name
    out = {'parts': [], 'bundles': [], 'overviews': [], 'sources': [], 'derived_from': [], 'filtered_by': [], 'used_by': [],
           'suites': [], 'member_benchmarks': [], 'member_suites': [], 'members': [],
           'alternatives': [], 'alternative_of': [], 'alternative_note': None,
           'facet_alternatives': {}}
    if v2.is_subclass(n.type, v2.TABLE):
        # Direct edges only: what this table is an alternative of (with its
        # own note on how it differs), and the tables that are alternatives of it.
        out['alternative_of'] = [obj for subject, kind, obj, other in triples
                                 if subject == name and kind == 'irds:alternativeOf']
        out['alternative_note'] = _alt_note(name)
        out['alternatives'] = [(subject, _alt_note(subject)) for subject, kind, obj, other in triples
                               if obj == name and kind == 'irds:alternativeOf']
    elif _is_benchmark(n.type):
        for entity in v2.ENTITIES:
            table = n.edge(entity)
            if table is not None and (alts := _alternatives(table.qualified_name)):
                out['facet_alternatives'][entity] = [(a, _alt_note(a)) for a in alts]
    facet_kinds = {f'irds:{e}' for e in v2.ENTITIES}
    for subject, kind, obj, other in triples:
        if subject == name and kind == 'irds:derivedFrom':
            target = gq.node_data(store, obj)
            is_resource = target is not None and v2.is_subclass(target.type, v2.DATA)
            out['sources' if is_resource else 'derived_from'].append(obj)
        elif subject == name and kind == 'irds:filtered_by':
            out['filtered_by'].append(obj)
        elif obj == name and kind in facet_kinds:
            out['used_by'].append((subject, kind.split(':', 1)[1]))
        elif subject == name and kind == 'irds:bundleItem':
            out['parts'].append(obj)
        elif obj == name and kind == 'irds:bundleItem':
            out['bundles'].append(subject)
        elif obj == name and kind == 'irds:member':
            out['suites'].append(subject)
        elif subject == name and kind == 'irds:member':
            target = gq.node_data(store, obj)
            is_suite = target is not None and v2.is_subclass(target.type, v2.SUITE)
            out['member_suites' if is_suite else 'member_benchmarks'].append(obj)
            out['members'].append({'name': obj, 'type': target.type if target else None})
    out['overviews'] = _overviews_of(name)
    # Bundles that are not an overview themselves (those are listed above, by the overview row).
    out['bundles'] = [b for b in out['bundles'] if b not in out['overviews']
                      and not _is_overview(b)]
    # A table's sources are auto-downloadable unless any needs a manual download.
    out['sources_manual'] = any(
        x.get('type') == 'manual'
        for src in out['sources'] for x in (getattr(gq.node_data(store, src), 'metadata', None) or {}).get('source') or [])
    return out


#: What a bundle's parts are called when counted, by kind (the first that fits wins).
_BUNDLE_KINDS = (
    (v2.SUITE, 'suite'), (v2.BENCHMARK, 'benchmark'), (v2.BUNDLE, 'bundle'),
    ('irds:Docs', 'document set'), ('irds:Queries', 'query set'), ('irds:Qrels', 'qrels'),
)


#: The most raw triples a node page lists (a big bundle has one per part).
RAW_TRIPLE_ROWS = 100

#: How many of a bundle's parts get a row on its page (and a lookup each). A bundle can have
#: 150,000 (CLIRMatrix); the page then shows a sample and the counts for all of them.
BUNDLE_ROWS = 300


def _bundle_overview(parts, limit=BUNDLE_ROWS):
    """What a bundle page derives from its parts rather than has written: one entry per part
    (name, type, and the facts shown after it -- its description, and for a table its language
    and size), the language/license/citation across them, and a count by kind. Nothing here is
    authored. Entries are ordered by name, so related parts (a corpus and its splits, which
    share a prefix) sit together."""
    entries, counts = [], {}
    languages, licenses, citations = [], [], []
    everything = sorted(parts)
    shown = everything[:limit]
    if len(everything) > limit:
        # counts for all of them from the cached type listing -- no lookup per part
        types_by_name = {name: types for name, types, _p in gq._all_type_rows(store)}
        for name in everything:
            types = types_by_name.get(name)
            if types:
                kind = next((label for category, label in _BUNDLE_KINDS if v2.is_subclass(types[0], category)),
                            'other')
                counts[kind] = counts.get(kind, 0) + 1

    def note(into, values):
        for v in values if isinstance(values, (list, tuple)) else [values]:
            if v and v not in into:
                into.append(v)

    for name in shown:
        view = gq.node_data(store, name)
        if view is None:
            entries.append({'name': name, 'type': None, 'description': None, 'lang': [], 'count': None})
            continue
        entry = {'name': name, 'type': view.type,
                 'description': view.metadata.get('description'),
                 'lang': view.values.get('lang') or [],
                 'count': _frozen_of(view).get('count')}
        entries.append(entry)
        note(languages, entry['lang'])
        note(licenses, view.values.get('license') or [])
        note(citations, view.values.get('citation') or [])
        if len(everything) <= limit:
            kind = next((label for category, label in _BUNDLE_KINDS if v2.is_subclass(view.type, category)),
                        'other')
            counts[kind] = counts.get(kind, 0) + 1
    return {'entries': entries, 'counts': list(counts.items()), 'languages': languages,
            'licenses': licenses, 'citations': citations, 'count': len(everything),
            'shown': len(shown)}


_ALLOWED_HREF = re.compile(r'^(https?://|mailto:|/|#)', re.I)
#: ``[[node]]`` or ``[[node|label]]`` in a summary: a link to a node.
_NODE_REF = re.compile(r'\[\[([^\[\]|]+?)(?:\|([^\[\]]+?))?\]\]')


def _resolve_ref(ref, bundle_name):
    """The qualified node a ``[[ref]]`` in ``bundle_name``'s summary means, or ``None``. A
    qualified name (``irds:msmarco-passage``) is itself; a bare one is a sibling fragment of the
    bundle (``par-par-test`` in ``hf:owner/repo`` is ``hf:owner/repo/par-par-test``), else a
    node in the bundle's own provider (``msmarco-passage`` in ``irds:msmarco``)."""
    ref = ref.strip()
    candidates = [ref] if ':' in ref else []
    if ':' not in ref and bundle_name:
        candidates.append(f'{bundle_name}/{ref}')
        candidates.append(f'{bundle_name.split(":", 1)[0]}:{ref}')
    return next((c for c in candidates if _node_exists(c)), None)


#: Where a citation reference (``[[dblp:conf/emnlp/SunD20]]``, ``[[doi:10.x/y]]``) points -- the
#: same targets the Citation row links to.
_CITATION_URLS = {'dblp:': 'https://dblp.org/rec/{}.html?view=bibtex', 'doi:': 'https://doi.org/{}'}


def _link_node_refs(text, bundle_name):
    """Turn ``[[...]]`` references into Markdown links: a node (``[[node]]``, code-styled), or a
    citation (``[[dblp:key]]`` / ``[[doi:...]]``, with ``|label`` to say what to call it). A
    node that doesn't exist is shown as plain code, so a stale reference is visible and not a
    dead link."""
    def link(match):
        ref, label = match.group(1).strip(), (match.group(2) or '').strip()
        for prefix, url in _CITATION_URLS.items():
            if ref.startswith(prefix):
                return f'[{label or f"`{ref}`"}]({url.format(ref[len(prefix):])})'
        target = _resolve_ref(ref, bundle_name)
        shown = label or f'`{ref}`'
        return f'[{shown}]({_node_url(target)})' if target else (label or f'`{ref}`')
    return _NODE_REF.sub(link, text)


#: ``[text](irds:node)`` in a documentation page: a standard Markdown link whose target is a
#: fully qualified node (or a ``dblp:``/``doi:`` citation). ``http(s)``/``mailto`` targets are left to
#: Markdown.
_QUALIFIED_LINK = re.compile(r'(?<!!)\[((?:[^\[\]\\]|\\.)*)\]\(\s*<?((?!https?:|mailto:)[A-Za-z][A-Za-z0-9_-]*:[^)\s>]+)>?\s*\)')


def _link_qualified(text):
    """Turn ``[text](irds:node)`` into a link to the node page, ``[text](dblp:key)`` into the
    citation's page. A target that is not a node in the graph is shown as the plain text, so a
    stale link in an open-edit page reads as text and never as a dead link."""
    def link(match):
        label, target = match.group(1), match.group(2)
        for prefix, url in _CITATION_URLS.items():
            if target.startswith(prefix):
                return f'[{label}]({url.format(target[len(prefix):])})'
        return f'[{label}]({_node_url(target)})' if _node_exists(target) else label
    return _QUALIFIED_LINK.sub(link, text)


#: Fenced code blocks and inline code spans -- where ``<`` is left for Markdown to escape itself.
_CODE = re.compile(r'(```.*?```|`[^`\n]+`)', re.S)


def _escape_html_outside_code(text):
    """Raw HTML in a summary is text, not markup: escape ``<`` everywhere except inside code,
    where Markdown already escapes it (escaping it first would show a literal ``&lt;``)."""
    parts = _CODE.split(text)
    return ''.join(part if i % 2 else part.replace('<', '&lt;') for i, part in enumerate(parts))


@app.template_filter('markdown')
def markdown_filter(text, node=None):
    """Render a node's documentation (``documentation:about``) or card summary. ``[text](irds:node)``
    links to nodes (legacy ``[[node]]`` / ``[[node|label]]`` still work, resolved relative to ``node``). Raw HTML in the
    source is escaped, and a link is kept only if it is http(s), mailto, or a local path."""
    import markdown
    source = _link_node_refs(_link_qualified(str(text or '')), getattr(node, 'qualified_name', None))
    html = markdown.markdown(_escape_html_outside_code(source),
                             extensions=['tables', 'sane_lists', 'fenced_code'])
    html = re.sub(r'href="([^"]*)"',
                  lambda m: m.group(0) if _ALLOWED_HREF.match(m.group(1)) else 'href="#"', html)
    return Markup(html)


_INDEXED = None


def _providers_with_an_index():
    """Provider prefixes whose documentation repository has a ``<prefix>/_index.yaml`` -- read off the
    sources the snapshot recorded, so a link never points at a file that is not there."""
    global _INDEXED
    if _INDEXED is None:
        from . import rdf_schema
        sources = {q.object.value for q in store.quads_for_pattern(
            None, rdf_schema.meta_iri('documentation:source'), None)}
        _INDEXED = {path.split('/', 1)[0] for path in sources if path.endswith('/_index.yaml')}
    return _INDEXED


def _doc_links(node, parts=()):
    """Where to send someone who wants to improve a node's documentation, from the one file its
    documentation was read from (``documentation:source``: its page if it has one, else its entry in
    the provider's index). ``about`` edits the page, or writes one (a new file with the name and a
    skeleton filled in); ``description`` edits the source file, or, with no documentation at all,
    adds one: a line in the provider's index, copied to the clipboard because GitHub cannot pre-fill
    it, or a new page where there is no index. Text from elsewhere has no source here, so it only
    offers to write a page."""
    from ir_datasets.v2 import documentation_provider as docs
    meta = node.metadata
    name = node.qualified_name
    if name.startswith('legacy:'):      # a v1 id is an alias of the node that replaced it: document that
        return {'about': None, 'description': None}
    source = meta.get('documentationSource')
    is_page = bool(source) and source.endswith('.md')

    def new():
        return docs.new_page_url(name, description=meta.get('description') or None, parts=list(parts))

    if is_page:
        about = {'label': 'edit this section' if meta.get('about') else 'add "About" section',
                 'url': docs.edit_url(source), 'exists': bool(meta.get('about'))}
    else:
        about = {'label': 'write a page' if meta.get('description') else 'add documentation',
                 'url': new(), 'exists': False}
    if source:
        description = {'label': 'edit', 'url': docs.edit_url(source)}
    elif name.split(':', 1)[0] in _providers_with_an_index():
        description = {'label': 'add a description', 'url': docs.edit_url(docs.index_path(name)),
                       'clipboard': docs.index_line(name), 'title': 'Copies the line to add; paste it into the file'}
    else:
        description = {'label': 'add a description', 'url': new()}
    return {'about': about, 'description': description}


def _with_about(sections, node):
    """``sections`` with an About entry right after Overview, where its card is -- if the node has
    anything written to show."""
    sections = list(sections)
    if not (node.metadata.get('about') or node.metadata.get('summary')):
        return sections
    at = next((i + 1 for i, (key, _) in enumerate(sections) if key == 'overview'), 0)
    return sections[:at] + [('about', 'About')] + sections[at:]


app.jinja_env.globals['doc_links'] = _doc_links
app.jinja_env.globals['with_about'] = _with_about


@app.route('/triples/<path:name>')
def triples_page(name):
    """Every raw triple of a node on one page -- what a node's own page cuts off at
    ``RAW_TRIPLE_ROWS``."""
    try:
        rows = gq.raw_triples_of(store, name) if gq.node_exists(store, name) else None
    except ValueError:
        rows = None
    if rows is None:
        abort(404, f'{name!r} is not a node in the graph')
    return render_template('triples.html', name=name, raw_triples=rows)


@app.route('/id/<path:name>')
def entity(name):
    """The page for anything identified here -- ``http://ir-datasets.com/id/<name>`` is the
    canonical URL of a node, a declared graph property or a graph class, and serves its page
    directly: a declared graph property's, else a graph class's, else the node's (which
    resolves it live if the snapshot lacks it, or is a 404)."""
    if any(e['name'] == name for e in _property_entries()):
        return property_page(name)
    if name in _type_graph()[0]:
        return class_page(name)
    return node(name)


# ── ir_datasets v1 addresses ─────────────────────────────────────────────────────────
# The previous site was one static page per dataset family, with each dataset a URL *fragment*
# on it (``antique.html#antique/test``). A fragment never reaches a server, so the page itself
# is a tiny script that reads it and goes to ``/legacy/<v1 id>``; that is a real server-side
# redirect (``legacy:<id>`` aliases the node that replaced it).

def _legacy_alias(v1_id):
    """The node that replaced the v1 dataset id ``v1_id``, or ``None`` (no such id, or not replaced)."""
    name = f'legacy:{v1_id}'
    if not gq.node_exists(store, name):
        return None
    return next((obj for subject, kind, obj, _other in gq.triples_of(store, name)
                 if subject == name and kind == 'irds:aliasOf'), None)


_family_main_cache = {}


def _legacy_family_main(family):
    """The node a v1 family (``antique``, ``msmarco-passage``) stands at: that of its first
    replaced id, in id order -- where a family's own page lands when no dataset is named, or
    when the named id was never replaced. ``None`` if nothing in the family was."""
    if family not in _family_main_cache:
        from .rdf_schema import edge_iri, graph_iri, node_name
        best = None
        for quad in store.quads_for_pattern(None, edge_iri('irds:aliasOf'), None, graph_iri('legacy')):
            v1_id = (node_name(quad.subject) or '')[len('legacy:'):]
            if (v1_id == family or v1_id.startswith(family + '/')) and (best is None or v1_id < best[0]):
                best = (v1_id, node_name(quad.object))
        _family_main_cache[family] = best[1] if best else None
    return _family_main_cache[family]


@app.route('/legacy/<path:v1_id>')
def legacy_id(v1_id):
    """A v1 dataset id's current page, as a permanent redirect. An id that was never replaced
    (or one this site has never heard of, under a known family) falls back to its nearest
    replaced ancestor, then to its family's main dataset."""
    parts = v1_id.split('/')
    target = next((t for t in (_legacy_alias('/'.join(parts[:n])) for n in range(len(parts), 0, -1)) if t),
                  None) or _legacy_family_main(parts[0])
    if target is None:
        abort(404, f'{v1_id!r} is not a dataset id this site knows')
    return redirect(url_for('entity', name=target), code=301)


@app.route('/<family>.html')
def legacy_page(family):
    """An old family page (``/antique.html``). The dataset is in the URL's fragment, which only
    the browser sees, so this is a page of script that sends it on to ``/legacy/<id>``."""
    if _legacy_family_main(family) is None and not gq.node_exists(store, f'legacy:{family}'):
        abort(404, f'{family}.html is not a page this site has')
    return render_template('legacy_page.html', family=family)


@app.route('/index.html')
def legacy_index():
    return redirect(url_for('index'), code=301)


@app.route('/datasets.html')
@app.route('/all.html')
def legacy_catalog():
    return redirect(url_for('search'), code=301)


def node(qualified):
    try:
        n = gq.node_data(store, qualified)
        triples = gq.triples_of(store, qualified) if n is not None else []
        raw_triples = gq.raw_triples_of(store, qualified) if n is not None else []
        raw_total, raw_triples = len(raw_triples), raw_triples[:RAW_TRIPLE_ROWS]
    except ValueError:
        # `qualified` (straight from the URL) isn't a syntactically valid
        # IRI component -- e.g. a raw space, which some providers' own node
        # names have turned up (see rdf_schema.py's to_quads() for the
        # build-time version of the same problem). No such node can ever
        # exist in the store either way (building its IRI to look it up
        # fails identically), so this is a 404, not an unhandled 500.
        abort(404, f'{qualified!r} is not a valid node name')
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
    attestation = _attestation_view(n, qualified)
    if template == 'node_attestation.html' and attestation is None:
        template = 'node_generic.html'      # an attestation record this site cannot read
    frozen = _frozen_of(n)
    requires = _requires_for(n, qualified)
    package = _package_of(n)
    derived = []
    for subject, kind, obj, other in triples:
        if kind == 'irds:derivedFrom' and obj == qualified:
            o = gq.node_data(store, subject)
            derived.append({'name': subject, 'type': o.type if o else None})
    related = _related(n, triples)
    return render_template(
        template, node=n, verification=_verification(n, qualified), attestation=attestation, frozen=frozen, triples=triples, raw_triples=raw_triples, raw_total=raw_total,
        requires=requires, install_hint=v2_requirements.install_hint(requires) if requires else None,
        package=package,
        derived=derived,
        type_labels=TYPE_LABELS, sample_table=_pretty_samples(frozen, _record_fields(n) or ()),
        snippet=_python_snippet(n), snippet_variants=_snippet_variants(n, triples), snippet_config=_snippet_config(n), record_fields=_record_fields(n), related=related, bundle=_bundle_overview(related['parts']) if category == v2.BUNDLE else None,
        resource_kind=_resource_kind(n) if v2.is_subclass(n.type, v2.DATA) else None)


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
    # Also reachable via the installed console script (`ir-datasets-site
    # serve`, see cli.py); this remains useful for `python -m
    # ir_datasets_site.app` during local development.
    app.run(debug=True, port=int(os.environ.get('PORT', 5000)))
