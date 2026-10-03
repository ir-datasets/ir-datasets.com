"""Console entry point installed as ``ir-datasets-site`` (see
``pyproject.toml``'s ``[project.scripts]``): wraps ``build_graph_db.py`` and
``app.py`` so both are reachable after a plain ``pip install`` with no need
to locate/run those files directly.

    ir-datasets-site build-graph-db [--providers irds hf clirmatrix] [--out graph.db] [--dump graph.nq.gz] [--no-dump]
    ir-datasets-site serve [--host 127.0.0.1] [--port 5000] [--store graph.db]
    ir-datasets-site build-static [--out dist] [--providers irds hf] [--store graph.db]
"""
import argparse
import os


def main(argv=None):
    parser = argparse.ArgumentParser(prog='ir-datasets-site', description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)

    from . import build_graph_db as bgd

    build_parser = subparsers.add_parser(
        'build-graph-db',
        help="materialize ir_datasets.v2's graph into a pyoxigraph store")
    build_parser.add_argument(
        '--providers', nargs='*', default=list(bgd.DEFAULT_PROVIDERS),
        help=f'provider prefixes to materialize (default: {" ".join(bgd.DEFAULT_PROVIDERS)})')
    build_parser.add_argument(
        '--out', default=str(bgd.DEFAULT_STORE_PATH),
        help=f'store path (default: {bgd.DEFAULT_STORE_PATH})')
    build_parser.add_argument(
        '--dump', default=None,
        help='gzipped N-Quads dump path -- a plain, standard-format export of the same '
             'quads, downloadable via app.py\'s /graph.nq.gz route or static_site.py\'s '
             'copy of it (default: --out, with its suffix replaced by .nq.gz)')
    build_parser.add_argument(
        '--no-dump', dest='dump_enabled', action='store_false', default=True,
        help="don't write the N-Quads dump at all")

    serve_parser = subparsers.add_parser('serve', help='run the Flask dev server')
    serve_parser.add_argument('--host', default='127.0.0.1')
    serve_parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', 5000)))
    serve_parser.add_argument(
        '--store', default=None,
        help='graph.db path (default: $IR_DATASETS_SITE_STORE, or ./graph.db)')
    serve_parser.add_argument('--debug', dest='debug', action='store_true', default=True)
    serve_parser.add_argument('--no-debug', dest='debug', action='store_false')

    static_parser = subparsers.add_parser(
        'build-static',
        help="render the whole site to plain static HTML (see static_site.py -- "
             "drops free-text/regex search and the hf: live-fallback for a "
             "missing node; everything else is pre-rendered as-is)")
    static_parser.add_argument('--out', default='dist', help='output directory (default: dist)')
    static_parser.add_argument(
        '--providers', nargs='*', default=None,
        help='provider prefixes to include (default: every registered provider '
             "except clirmatrix/legacy -- see static_site.DEFAULT_STATIC_PROVIDERS)")
    static_parser.add_argument(
        '--store', default=None,
        help='graph.db path (default: $IR_DATASETS_SITE_STORE, or ./graph.db)')

    args = parser.parse_args(argv)

    if args.command == 'build-graph-db':
        dump = False if not args.dump_enabled else (args.dump or True)
        n = bgd.build(providers=args.providers, out=args.out, dump=dump)
        dump_path = bgd.default_dump_path(args.out) if dump is True else dump
        print(f'wrote {args.out}: {n} triples, providers={args.providers}'
              + (f'; dump={dump_path}' if dump else ''))
    elif args.command == 'serve':
        if args.store:
            # Must be set before app.py is imported -- it reads this env var
            # at import time to build its (module-level) Store object.
            os.environ['IR_DATASETS_SITE_STORE'] = args.store
        from .app import app
        app.run(host=args.host, port=args.port, debug=args.debug)
    elif args.command == 'build-static':
        if args.store:
            os.environ['IR_DATASETS_SITE_STORE'] = args.store
        from . import static_site
        out_dir = static_site.build(out=args.out, providers=args.providers)
        print(f'wrote static site to {out_dir}')


if __name__ == '__main__':
    main()
