"""``ir-datasets-attest-server``: run the attestation server."""
import argparse
import sys

from . import server, store


def main(argv=None):
    p = argparse.ArgumentParser(prog='ir-datasets-attest-server', description=__doc__)
    p.add_argument('--db', default='attest.db', help='SQLite file (created if missing)')
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=8000)
    args = p.parse_args(argv)
    httpd = server.make(store.Store(args.db), args.host, args.port)
    print(f'serving {args.db} on http://{args.host}:{httpd.server_port}', file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
