"""SQLite storage for the attestation server. One table; a record is stored whole, as submitted."""
import json
import sqlite3
import threading

from ir_datasets.v2.attest import records

_SCHEMA = '''
CREATE TABLE IF NOT EXISTS attestation (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    subject     TEXT,                 -- qualified node name; NULL for an identity
    record      TEXT NOT NULL,        -- canonical JSON
    signature   TEXT,
    signed_by   TEXT REFERENCES attestation(id),
    verified    INTEGER NOT NULL DEFAULT 0,   -- the signature checked out when it was received
    received_at TEXT NOT NULL,
    seq         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS attestation_subject ON attestation(subject);
CREATE UNIQUE INDEX IF NOT EXISTS identity_username ON attestation(json_extract(record, '$.username')) WHERE kind = 'identity';
'''


class Store:
    def __init__(self, path=':memory:'):
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(_SCHEMA)

    @staticmethod
    def _out(row):
        return {'id': row['id'], 'type': row['kind'], 'subject': row['subject'],
                'record': json.loads(row['record']), 'signature': row['signature'],
                'signed_by': row['signed_by'], 'verified': bool(row['verified']),
                'received_at': row['received_at'], 'seq': row['seq']}

    def add(self, kind, subject, record, signature=None, signed_by=None, verified=False, id=None):
        """Insert one attestation and return its id. Raises ``sqlite3.IntegrityError`` on a taken username."""
        id = id or records.new_id()
        with self._lock, self._db:
            seq = self._db.execute('SELECT COALESCE(MAX(seq), 0) + 1 FROM attestation').fetchone()[0]
            self._db.execute(
                'INSERT INTO attestation VALUES (?,?,?,?,?,?,?,?,?)',
                (id, kind, subject, records.canonical_json(record), signature,
                 signed_by or (id if kind == records.IDENTITY else None), int(verified),
                 records.now(), seq))
        return id

    def get(self, id):
        with self._lock:
            row = self._db.execute('SELECT * FROM attestation WHERE id = ?', (id,)).fetchone()
        return self._out(row) if row else None

    def since(self, after=0, limit=None):
        """Full records with ``seq`` greater than ``after``, oldest first (records never change, so a
        reader can keep the highest ``seq`` it has seen and ask only for what is newer)."""
        with self._lock:
            rows = self._db.execute('SELECT * FROM attestation WHERE seq > ? ORDER BY seq LIMIT ?',
                                    (after, limit if limit else -1)).fetchall()
        return [self._out(r) for r in rows]

    def for_subject(self, subject):
        with self._lock:
            rows = self._db.execute('SELECT * FROM attestation WHERE subject = ? ORDER BY seq', (subject,)).fetchall()
        return [self._out(r) for r in rows]

    def identity_by_username(self, username):
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM attestation WHERE kind = 'identity' AND json_extract(record, '$.username') = ?",
                (username,)).fetchone()
        return self._out(row) if row else None
