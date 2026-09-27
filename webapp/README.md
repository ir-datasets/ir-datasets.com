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

```bash
cd ~/ws/ir-datasets.com/webapp
pip install -r requirements.txt
pip install -e ~/ws/ir-datasets          # if ir_datasets.v2 isn't already installed
python build_graph_db.py                 # builds graph.db from the current manifest (+ a fresh hf crawl)
python app.py                            # http://127.0.0.1:5000
```

Re-run `build_graph_db.py` whenever `ir_datasets/v2/manifest.json` changes or
you want a fresher `hf:` snapshot -- it's a full delete-and-recreate, not
incremental, and takes well under a second for `irds` alone (the `hf` crawl's
time depends on how many repos are tagged `ir-datasets` on the Hub; pass
`--providers irds` to skip it). The manifest itself needs to exist first
(`cd ~/ws/ir-datasets && python -m ir_datasets.v2.freeze --verify`) — counts,
hashes and sample records only render if a manifest has been frozen.

## What's here

| Route | Renders |
|---|---|
| `/` | Node-type counts, installed providers |
| `/browse?type=&q=` | Every node, filterable by type / name substring |
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
