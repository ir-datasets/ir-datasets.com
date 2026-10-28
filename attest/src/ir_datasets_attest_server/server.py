"""The attestation server: a small WSGI app over a SQLite store.

    POST /register                       create an IdentityAttestation
    POST /{hash,visual,evaluation}       create an attestation about a node
    POST /revoke                         delete an attestation (a signed request, from the identity that signed it)
    GET  /[?full=1&after=N&limit=M]      attestations (id and type; whole records with full), after seq N
    GET  /<id>,<id>,...                  those records
    GET  /<prefix:name>                  every attestation about a node

Run it with ``ir-datasets-attest-server``. Records and signatures are checked with ``ir_datasets.v2.attest``,
the same code the clients use.
"""
import json
import sqlite3
from urllib.parse import parse_qs
from wsgiref.simple_server import WSGIRequestHandler, make_server

from ir_datasets.v2.attest import crypto, records

_STATUS = {200: '200 OK', 201: '201 Created', 400: '400 Bad Request', 404: '404 Not Found',
           403: '403 Forbidden', 405: '405 Method Not Allowed', 409: '409 Conflict', 413: '413 Payload Too Large',
           422: '422 Unprocessable Entity'}


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class App:
    def __init__(self, store):
        self.store = store

    # -- WSGI ---------------------------------------------------------------

    def __call__(self, environ, start_response):
        try:
            status, body = self.dispatch(environ['REQUEST_METHOD'], environ.get('PATH_INFO', '/'), environ)
        except HttpError as e:
            status, body = e.status, {'error': str(e)}
        payload = json.dumps(body).encode('utf-8')
        start_response(_STATUS[status], [('Content-Type', 'application/json'),
                                         ('Content-Length', str(len(payload))),
                                         ('Access-Control-Allow-Origin', '*')])
        return [payload]

    def dispatch(self, method, path, environ):
        path = path.strip('/')
        if method == 'GET':
            return 200, self.read(path, parse_qs(environ.get('QUERY_STRING', '')))
        if method == 'POST':
            body = self.json_body(environ)
            if path == 'register':
                return 201, self.register(body)
            if path in records.SUBMITTABLE:
                return 201, self.submit(path, body)
            if path == 'revoke':
                return 200, self.revoke(body)
            raise HttpError(404, f'no such endpoint: POST /{path}')
        raise HttpError(405, 'only GET and POST are supported')

    @staticmethod
    def json_body(environ):
        try:
            length = int(environ.get('CONTENT_LENGTH') or 0)
        except ValueError:
            raise HttpError(400, 'bad Content-Length')
        if length > records.MAX_BODY_BYTES:
            raise HttpError(413, f'body is limited to {records.MAX_BODY_BYTES} bytes')
        try:
            body = json.loads(environ['wsgi.input'].read(length) or b'null')
        except ValueError:
            raise HttpError(400, 'body is not valid JSON')
        if not isinstance(body, dict):
            raise HttpError(400, 'body must be a JSON object')
        return body

    # -- reads --------------------------------------------------------------

    def read(self, path, query=None):
        query = query or {}
        if not path:
            try:
                after, limit = int(query.get('after', ['0'])[0]), min(int(query.get('limit', ['1000'])[0]), 5000)
            except ValueError:
                raise HttpError(400, 'after and limit must be integers')
            rows = self.store.since(after, limit)
            if 'full' not in query:
                rows = [{k: r[k] for k in ('id', 'type', 'seq', 'deleted') if k in r} for r in rows]
            return {'attestations': rows}
        if ':' in path:      # a node name; ids are bare GUIDs
            return {'subject': path, 'attestations': self.store.for_subject(path)}
        found = [self.store.get(i) for i in dict.fromkeys(filter(None, path.split(',')))]
        if len(found) == 1 and found[0] is None:
            raise HttpError(404, f'no such attestation: {path}')
        return {'attestations': [r for r in found if r], 'missing': [i for i, r in zip(path.split(','), found) if not r]}

    # -- writes -------------------------------------------------------------

    def register(self, body):
        record = self._record(records.IDENTITY, body)
        # An identity vouches for itself: signed with the key it announces.
        signature = body.get('signature')
        if not signature:
            raise HttpError(422, 'registration must be signed with the private key of public_key')
        if not crypto.verify(record['public_key'], signature, records.signing_payload(record)):
            raise HttpError(422, 'signature does not verify against public_key')
        try:
            id = self.store.add(records.IDENTITY, None, record, signature=signature, verified=True)
        except sqlite3.IntegrityError:
            raise HttpError(409, f'username {record["username"]!r} is taken')
        return {'id': id}

    def submit(self, kind, body):
        signature, signed_by = body.get('signature'), body.get('signed_by')
        record = self._record(kind, body, fill_timestamp=not signature)
        subject = record['subject']
        verified = False
        if signature or signed_by or kind in records.SIGNATURE_REQUIRED:
            if not (signature and signed_by):
                raise HttpError(422, f'a {kind} attestation must be signed (signature and signed_by)' if kind in records.SIGNATURE_REQUIRED
                                else 'signature and signed_by go together')
            identity = self.store.get(signed_by)
            if identity is None or identity['type'] != records.IDENTITY:
                raise HttpError(422, f'signed_by {signed_by!r} is not a registered identity')
            verified = crypto.verify(identity['record']['public_key'], signature, records.signing_payload(record))
            if not verified:
                raise HttpError(422, 'signature does not verify against the identity\'s public key')
        return {'id': self.store.add(kind, subject, record, signature=signature, signed_by=signed_by, verified=verified)}

    def revoke(self, body):
        """Delete an attestation, at the request of whoever signed it. The request is a signed record naming the
        attestation (``subject: attest:<id>``): it must be signed by the identity that signed that attestation, and
        verify against that identity's public key -- which is the proof that the key's owner is the one asking.
        Nothing about the request is kept; the attestation's content is erased (see ``Store.delete``). An
        anonymous attestation has no key behind it, so it cannot be deleted; nor can an identity."""
        record = self._record(records.REVOCATION, body)
        signature, signed_by = body.get('signature'), body.get('signed_by')
        if not (signature and signed_by):
            raise HttpError(422, 'a deletion must be signed (signature and signed_by)')
        target_id = record['subject'].split(':', 1)[1]
        target = self.store.get(target_id)
        if target is None:
            raise HttpError(404, f'no such attestation: {record["subject"]}')
        if target['type'] == records.IDENTITY:
            raise HttpError(422, 'an identity cannot be deleted')
        if not target['signed_by']:
            raise HttpError(422, 'that attestation is anonymous (not signed), so no key can prove it is theirs')
        if signed_by != target['signed_by']:
            raise HttpError(403, 'only the identity that signed an attestation can delete it')
        identity = self.store.get(signed_by)
        if identity is None or not crypto.verify(identity['record']['public_key'], signature, records.signing_payload(record)):
            raise HttpError(422, 'signature does not verify against the identity\'s public key')
        self.store.delete(target_id)
        return {'id': target_id, 'deleted': True}

    @staticmethod
    def _record(kind, body, fill_timestamp=False):
        record = body.get('record')
        if isinstance(record, dict) and fill_timestamp and 'attested_at' not in record:
            record = {**record, 'attested_at': records.now()}   # only unsigned: a signed record is exactly what was signed
        try:
            return records.validate(kind, record)
        except ValueError as e:
            raise HttpError(422, str(e))


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):
        pass


def make(store, host='127.0.0.1', port=8000, quiet=False):
    """A ready-to-``serve_forever`` WSGI server (port 0 picks a free one)."""
    return make_server(host, port, App(store), handler_class=_QuietHandler if quiet else WSGIRequestHandler)
