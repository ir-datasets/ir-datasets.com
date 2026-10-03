"""Materialize ``ir_datasets.v2``'s graph into a pyoxigraph store, so the
webapp can browse it via fast, indexed lookups instead of walking live
Python objects on every request (see ``graph_queries.py``).

Delete-and-recreate, not incremental: ``manifest.json`` is small (hundreds of
nodes) and is the single source of truth, so a full rebuild is simplest and
needs no diff/deletion logic. Includes a live, best-effort snapshot of the
fully dynamic ``hf`` provider (see ``hf_provider.export_triples``) -- run
this again whenever you want a fresher one.

Also writes a gzipped N-Quads dump (see ``DEFAULT_DUMP_SUFFIX``) alongside
the store itself -- ``graph.db`` is a pyoxigraph/RocksDB directory, in an
internal, Oxigraph-version-specific encoding no other tool can read; the
N-Quads dump is the same quads (same named-graph-per-provider partitioning;
see rdf_schema.py) in a plain, standard RDF serialization anything else can
load. It's what both app.py's and static_site.py's download link serve --
see app.py's ``/graph.nq.gz`` route.

Run:
    ir-datasets-site build-graph-db [--providers irds hf clirmatrix] [--out graph.db] [--dump graph.nq.gz]
"""
import argparse
import gzip
import logging
import os
import shutil
import sys
from pathlib import Path

import pyoxigraph as ox

from .rdf_schema import to_quads

logger = logging.getLogger(__name__)

try:
    import ir_datasets.v2 as v2
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4] / 'ir-datasets'))
    import ir_datasets.v2 as v2

#: Every currently-installed provider, not a hardcoded list -- a provider
#: added to ir_datasets.v2 (as CLIRMatrix's own split-out ``clirmatrix:``
#: provider was) is picked up automatically instead of silently being left
#: out of the materialized store until someone remembers to update this file.
DEFAULT_PROVIDERS = tuple(sorted(v2.graph.providers))
#: Matches app.py's STORE_PATH default/override (./graph.db, or
#: $IR_DATASETS_SITE_STORE) so running build-graph-db then serve with no
#: extra flags just works.
DEFAULT_STORE_PATH = Path(os.environ.get('IR_DATASETS_SITE_STORE', 'graph.db')).resolve()


def default_dump_path(store_path):
    """Where the N-Quads dump lands for a given store path, when ``--dump``
    isn't given explicitly -- right beside it (``graph.db`` -> ``graph.nq.gz``),
    so ``serve``/``build-static`` (which only know the store path -- see
    app.py's STORE_PATH/DUMP_PATH) can find it without a second setting to
    keep in sync."""
    return Path(store_path).with_suffix('.nq.gz')


def build(providers=DEFAULT_PROVIDERS, out=DEFAULT_STORE_PATH, dump=True):
    """``dump``: ``True`` (default) writes the N-Quads dump to
    ``default_dump_path(out)``, ``False`` skips it, or an explicit path to
    write it elsewhere."""
    out = Path(out)
    if out.exists():
        shutil.rmtree(out)
    store = ox.Store(str(out))
    logger.info('fetching triples for providers=%s', list(providers))
    triples = v2.graph.export_triples(providers=list(providers))
    try:
        quads = list(to_quads(triples))
    except Exception:
        logger.error('build-graph-db failed while converting triples to quads for '
                     'providers=%s -- see the error above for which triple and why', list(providers))
        raise
    logger.info('converted %d triples to quads, writing to %s', len(quads), out)
    store.bulk_extend(quads)
    store.optimize()
    store.flush()
    if dump:
        dump_path = Path(dump) if dump is not True else default_dump_path(out)
        logger.info('writing N-Quads dump to %s', dump_path)
        with gzip.open(dump_path, 'wb') as f:
            store.dump(f, ox.RdfFormat.N_QUADS)
    return len(quads)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--providers', nargs='*', default=list(DEFAULT_PROVIDERS),
                        help=f'provider prefixes to materialize (default: {" ".join(DEFAULT_PROVIDERS)})')
    parser.add_argument('--out', default=str(DEFAULT_STORE_PATH),
                        help=f'store path (default: {DEFAULT_STORE_PATH})')
    parser.add_argument('--dump', default=None,
                        help='gzipped N-Quads dump path (default: --out, with its suffix '
                             'replaced by .nq.gz)')
    parser.add_argument('--no-dump', dest='dump_enabled', action='store_false', default=True,
                        help="don't write the N-Quads dump at all")
    args = parser.parse_args(argv)
    dump = False if not args.dump_enabled else (args.dump or True)
    n = build(providers=args.providers, out=args.out, dump=dump)
    dump_path = default_dump_path(args.out) if dump is True else dump
    print(f'wrote {args.out}: {n} triples, providers={args.providers}'
          + (f'; dump={dump_path}' if dump else ''))


if __name__ == '__main__':
    main()

