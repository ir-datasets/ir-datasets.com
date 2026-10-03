# ir-datasets.com v2 (dynamic prototype)

A Flask app that renders every page per-request from a **materialized**
snapshot of the `ir_datasets.v2` graph -- a pyoxigraph RDF store built by
`build_graph_db.py` and read by `graph_queries.py` (the RDF mapping is in
`rdf_schema.py`) -- instead of walking `ir_datasets.v2`'s live Python objects
on every request. This is still, per `../PLAN_V2_SITE.md`'s **phase 4**,
pages generated per-request rather than baked once at build time
(`../generate.py`'s approach); it's just that "assemble the graph" now
happens once, ahead of time, instead of on every request:

> Minimal dynamic app: Graph assembly from a single local manifest file +
> dataset/benchmark pages + code-snippet generators, checked for content
> parity against today's static pages. No registry/fetcher yet — one
> provider, manifest read from disk.

`irds` (this repo's own, trusted, first-party provider) and `hf` (HuggingFace
Hub datasets, fully dynamic -- see `ir_datasets.v2.hf_provider`) are both
materialized; `hf`'s data is necessarily a point-in-time snapshot (the store
stamps each of its nodes with `snapshot_at`), since it can't be enumerated
ahead of time the way a frozen manifest can. A node the last snapshot missed
(a brand-new hf repo) still resolves -- `/n/<qualified>` falls back to a live
`ir_datasets.v2` lookup for anything not in the store.

## Run it

Installable directly from this repo (pulls in `ir_datasets.v2` too, pinned
in `pyproject.toml` to the branch that has it -- see `../.devcontainer/Dockerfile`
for the same approach):

```bash
pip install "ir-datasets-site @ git+https://github.com/<org>/ir-datasets.com.git#subdirectory=webapp"
ir-datasets-site build-graph-db          # Potentially add something like --provider irds to speed up. Builds ./graph.db from the current manifest (+ a fresh hf crawl)
ir-datasets-site serve                   # http://127.0.0.1:5000
```

Or editable, from a local checkout (e.g. inside `.devcontainer`, or to
iterate on `ir_datasets.v2` alongside this app):

```bash
cd ~/ws/ir-datasets.com/webapp
pip install -e .
pip install -e ~/ws/ir-datasets          # if you want your own ir_datasets.v2 checkout instead of pyproject.toml's pinned branch
ir-datasets-site build-graph-db
ir-datasets-site serve
```

Re-run `build-graph-db` whenever `ir_datasets/v2/manifest.json` changes or
you want a fresher `hf:` snapshot -- it's a full delete-and-recreate, not
incremental, and takes well under a second for `irds` alone (the `hf` crawl's
time depends on how many repos are tagged `ir-datasets` on the Hub; pass
`--providers irds` to skip it). The manifest itself needs to exist first
(`cd ~/ws/ir-datasets && python -m ir_datasets.v2.freeze --verify`) — counts,
hashes and sample records only render if a manifest has been frozen.

`graph.db` is written to `./graph.db` (the current working directory), not
inside the installed package -- override with `--out`/`--store` or the
`IR_DATASETS_SITE_STORE` environment variable if you want it elsewhere.

## Downloading the graph data

`graph.db` is a pyoxigraph/RocksDB directory in an internal, Oxigraph-specific
format -- not something another tool can read directly. `build-graph-db`
therefore also writes a plain, standard **gzipped N-Quads** dump of the exact
same quads (same provider-partitioned named graphs) right alongside it
(`graph.db` -> `graph.nq.gz`; override with `--dump`, or skip it with
`--no-dump`). Both the live app and a static build serve this at the same
URL, `/graph.nq.gz` (linked from every page's footer, once a dump exists):

- **Live app**: `app.py`'s `/graph.nq.gz` route (`send_file`).
- **Static build**: `static_site.py`'s `build()` copies it into the output
  directory as-is, so it's just another static file there.

## Static build

No live process required at all -- `ir-datasets-site build-static` renders
the whole site, once, to plain HTML + assets (see `static_site.py`):

```bash
ir-datasets-site build-graph-db          # (as above) graph.db must exist first
ir-datasets-site build-static --out dist
python -m http.server 8000 --directory dist   # or any other static file host
```

By default this includes every provider except `clirmatrix`/`legacy` (same
providers the live home page hides by default) -- pass `--providers irds hf
clirmatrix ...` to include more, keeping in mind each provider means one HTML
file per node (CLIRMatrix alone is ~620k nodes).

This drops the two things that genuinely need a live backend:

- **Free-text/regex search** (`/search?q=...&regex=1`) -- unbounded input,
  can't be enumerated into finitely many pages. The static build instead
  ships one pre-rendered page per type filter (`/search`,
  `/search/type/<type>`), and hides the live navbar search box (it has
  nothing to call).
- **The `hf:` live-fallback resolution** for a node missing from the
  materialized snapshot (see `node()` in `app.py`) -- a static export only
  ever knows what's in `graph.db`.

Everything else (home page, every node page, every provider page) renders
identically to the live app.

## What's here

| Route | Renders |
|---|---|
| `/` | Node-type counts, installed providers |
| `/search?type=&q=` | Every node, filterable by type / name substring (live app only -- see "Static build" above) |
| `/provider/<prefix>` | One provider's nodes, declared vocabulary, aliases |
| `/n/<qualified>` | One node — dispatches to a type-specific template (`node_benchmark.html`, `node_table.html`, `node_resource.html`, `node_suite.html`), falling back to `node_generic.html` for a type this site has no template for yet (a third-party provider's own vocabulary, once those exist) |

## Known limitations (honest, not hidden)

- **Content is thin**: ANTIQUE, MS MARCO (passage + document), BEIR (14 headline
  benchmarks folded into one suite, including all 12 CQADupStack sub-forums),
  NanoBEIR, and BRIGHT (12 domains + 8 long-document variants, one suite) are
  ported to v2 so far — still a fraction of the real catalog, since each
  family is hand-ported. This is a *content* gap (the v1→v2 manifest exporter,
  plan phase 3), not an architecture one — the app will show whatever's in the
  graph without changes once that exists.
- **Counts only show where `freeze --verify` has run.** BEIR/NanoBEIR are
  frozen metadata-only (no `--verify`, since that would download BEIR's
  multi-GB zips) — their pages show `—` for counts rather than a number.
  Page rendering **never** triggers materialization itself, deliberately (see
  `app.py`'s `_frozen_count` and the comment on `{% if table is not none %}`
  in `node_benchmark.html` — a bare `{% if table %}` on a `Table` object
  silently calls `.count()` via Python's truthiness-falls-back-to-`__len__`
  protocol, which is how a page view once triggered a live 1GB download).
- **No code-snippet generators beyond a plain Python example.** The old
  site's CLI/PyTerrier/XPM-IR generators aren't ported; showing a snippet for
  a feature that doesn't exist yet (there's no v2 CLI) would be misleading,
  so it's just omitted rather than faked.
- **`Resource` sources are unstructured** (`repr()` strings, not `{kind,
  url}`/`{kind, instructions}` — see `PLAN_V2_SITE.md`'s "Files that can't be
  automatically obtained" section). The resource page renders them as-is and
  says so. Integrity is richer than that, though: a `Resource` can declare
  multiple `algo:hexdigest` hashes (`nodes.parse_hash`), all shown on its page.
- **No download-checker, no provider registry/fetcher, no dynamic-node
  resolver sandbox** — those are later phases in `PLAN_V2_SITE.md`, not
  missing by oversight.
- **`debug=True`** in `app.py` is for local development only; drop it (and
  add a real WSGI server) before this goes anywhere near production.
