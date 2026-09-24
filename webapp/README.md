# ir-datasets.com v2 (dynamic prototype)

A Flask app that renders every page per-request from the live
`ir_datasets.v2` graph, instead of `../generate.py`'s batch-import-everything-
and-bake-static-HTML approach. This is **phase 4** of `../PLAN_V2_SITE.md`:

> Minimal dynamic app: Graph assembly from a single local manifest file +
> dataset/benchmark pages + code-snippet generators, checked for content
> parity against today's static pages. No registry/fetcher yet — one
> provider, manifest read from disk.

In practice that means `app.py` does `import ir_datasets.v2 as v2` and reads
straight from `v2.graph` — the trust-free, manifest-only fetch path
(`PLAN_V2_SITE.md`'s "key insight") is for *other* providers, added in a
later phase; `irds` is this repo's own, trusted, first-party provider, so
importing it directly is the right call for now, not a shortcut around the
design.

## Run it

```bash
cd ~/ws/ir-datasets.com/webapp
pip install -r requirements.txt
pip install -e ~/ws/ir-datasets          # if ir_datasets.v2 isn't already installed
python app.py                            # http://127.0.0.1:5000
```

The manifest needs to exist first (`cd ~/ws/ir-datasets && python -m
ir_datasets.v2.freeze --verify`) — the app reads live nodes, but counts,
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
