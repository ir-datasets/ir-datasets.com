"""Materialize ``ir_datasets.v2``'s graph into a pyoxigraph store, so the
webapp can browse it via fast, indexed lookups instead of walking live
Python objects on every request (see ``graph_queries.py``).

Delete-and-recreate, not incremental: ``manifest.json`` is small (hundreds of
nodes) and is the single source of truth, so a full rebuild is simplest and
needs no diff/deletion logic. Includes a live, best-effort snapshot of the
fully dynamic ``hf`` provider (see ``hf_provider.export_triples``) -- run
this again whenever you want a fresher one.

Run:
    python build_graph_db.py [--providers irds hf clirmatrix] [--out graph.db]
"""
import argparse
import shutil
import sys
from pathlib import Path

import pyoxigraph as ox

from rdf_schema import to_quads

try:
    import ir_datasets.v2 as v2
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / 'ir-datasets'))
    import ir_datasets.v2 as v2

#: Every currently-installed provider, not a hardcoded list -- a provider
#: added to ir_datasets.v2 (as CLIRMatrix's own split-out ``clirmatrix:``
#: provider was) is picked up automatically instead of silently being left
#: out of the materialized store until someone remembers to update this file.
DEFAULT_PROVIDERS = tuple(sorted(v2.graph.providers))
DEFAULT_STORE_PATH = Path(__file__).resolve().parent / 'graph.db'


def build(providers=DEFAULT_PROVIDERS, out=DEFAULT_STORE_PATH):
    out = Path(out)
    if out.exists():
        shutil.rmtree(out)
    store = ox.Store(str(out))
    triples = v2.graph.export_triples(providers=list(providers))
    quads = list(to_quads(triples))
    store.bulk_extend(quads)
    store.optimize()
    store.flush()
    return len(quads)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--providers', nargs='*', default=list(DEFAULT_PROVIDERS),
                        help=f'provider prefixes to materialize (default: {" ".join(DEFAULT_PROVIDERS)})')
    parser.add_argument('--out', default=str(DEFAULT_STORE_PATH),
                        help=f'store path (default: {DEFAULT_STORE_PATH})')
    args = parser.parse_args(argv)
    n = build(providers=args.providers, out=args.out)
    print(f'wrote {args.out}: {n} triples, providers={args.providers}')


if __name__ == '__main__':
    main()
