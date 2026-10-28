"""End to end: the real server, in a thread, against the real client from ir_datasets.v2.attest."""
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from ir_datasets.v2.attest import AttestError, Client, attest_evaluation, attest_visual, crypto, records
from ir_datasets_attest_server import server, store

HASH = {'hashes': ['sha256:' + 'a' * 64], 'source': None}


class ServerCase(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        patch = mock.patch.dict(os.environ, {'IR_DATASETS_HOME': home.name})   # identities are saved here
        patch.start()
        self.addCleanup(patch.stop)
        self.store = store.Store()
        self.httpd = server.make(self.store, port=0, quiet=True)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.client = Client(f'http://127.0.0.1:{self.httpd.server_port}')
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def post(self, kind, body):
        return self.client._call('POST', kind, json=body)

    def assertRefused(self, status, call, *args, **kw):
        with self.assertRaises(AttestError) as e:
            call(*args, **kw)
        self.assertEqual(status, e.exception.status)


class TestReading(ServerCase):
    def test_anonymous_roundtrip(self):
        id = self.client.submit('hash', 'irds:antique-docs', HASH)
        self.assertEqual([id], self.client.ids())
        [row] = self.client.get(id, 'no-such-id')
        self.assertEqual(('hash', 'irds:antique-docs', None, False),
                         (row['type'], row['subject'], row['signature'], row['verified']))
        self.assertIn('attested_at', row['record'])
        self.assertEqual([id], [r['id'] for r in self.client.for_node('irds:antique-docs')])
        self.assertEqual([], self.client.for_node('irds:other'))
        self.assertRefused(404, self.client._call, 'GET', 'no-such-id')

    def test_incremental_listing(self):
        ids = [self.client.submit('hash', f'irds:d{i}', HASH) for i in range(3)]
        rows = list(self.client.records())
        self.assertEqual(ids, [r['id'] for r in rows])
        self.assertEqual(ids[2:], [r['id'] for r in self.client.records(after=rows[1]['seq'])])
        self.assertEqual([], list(self.client.records(after=rows[2]['seq'])))
        light = self.client._call('GET', '')['attestations']       # no ?full: id and type only
        self.assertEqual({'id', 'type', 'seq'}, set(light[0]))


class TestSubmitting(ServerCase):
    def test_evaluation_and_bad_input(self):
        attest_evaluation('irds:antique-test', 'BM25', {'nDCG@10': 0.3}, client=self.client)
        self.assertRefused(422, self.post, 'evaluation', {'record': {'kind': 'evaluation', 'subject': 'antique-test'}})
        self.assertRefused(404, self.post, 'bogus', {})
        self.assertRefused(422, self.post, 'hash', {})
        self.assertRefused(400, self.client._call, 'POST', 'hash', data=b'not json')

    def test_kind_must_match_endpoint(self):
        record = {'kind': 'hash', 'subject': 'irds:x', **HASH}
        self.assertRefused(422, self.post, 'evaluation', {'record': record})

    def test_identity_and_signatures(self):
        me = self.client.register('alice')
        self.assertEqual(me.identity_id, crypto.Identity.load().identity_id)
        id = self.client.submit('evaluation', 'irds:antique-test', {'system': 'BM25', 'measures': {'MAP': 0.2}}, me)
        [row] = self.client.get(id)
        self.assertTrue(row['verified'])
        self.assertEqual(me.identity_id, row['signed_by'])
        self.assertRefused(409, self.client.register, 'alice')        # username taken
        # A signature is bound to the whole record, subject included.
        record = {'kind': 'evaluation', 'subject': 'irds:antique-test', 'attested_at': records.now(),
                  'system': 'BM25', 'measures': {'MAP': 0.2}}
        signature = me.sign(records.signing_payload(record))
        for changed in ({**record, 'subject': 'irds:other'}, {**record, 'measures': {'MAP': 0.9}}):
            self.assertRefused(422, self.post, 'evaluation',
                               {'record': changed, 'signature': signature, 'signed_by': me.identity_id})
        self.assertRefused(422, self.post, 'evaluation', {'record': record, 'signature': signature, 'signed_by': 'nope'})
        self.assertRefused(422, self.post, 'evaluation', {'record': record, 'signature': signature})   # signed_by missing

    def test_registration_must_be_self_signed(self):
        private, public = crypto.generate_keypair()
        record = {'kind': 'identity', 'username': 'mallory', 'public_key': public, 'attested_at': records.now()}
        self.assertRefused(422, self.post, 'register', {'record': record})
        other = crypto.generate_keypair()[0]
        self.assertRefused(422, self.post, 'register',
                           {'record': record, 'signature': crypto.sign(other, records.signing_payload(record))})

    def test_visual_needs_signature(self):
        record = {'verdict': 'ok', 'sample': ['1'], 'notes': ''}
        self.assertRefused(422, self.client.submit, 'visual', 'irds:antique-docs', record)
        self.client.register('bob')
        id = attest_visual('irds:antique-docs', 'ok', sample=['1'], client=self.client)
        self.assertTrue(self.client.get(id)[0]['verified'])

    def test_body_limit(self):
        self.assertRefused(413, self.post, 'hash', {'record': {'pad': 'x' * records.MAX_BODY_BYTES}})


class TestDeleting(ServerCase):
    """A signer can delete what they signed; the proof is a request signed with the key."""

    def setUp(self):
        super().setUp()
        self.alice = self.client.register('alice')
        self.bob = self.client.register('bob')
        self.mine = self.client.submit('hash', 'irds:antique-docs', HASH, self.alice)
        self.theirs = self.client.submit('hash', 'irds:antique-docs', HASH, self.bob)
        self.anon = self.client.submit('hash', 'irds:antique-docs', HASH)

    def request(self, target, identity, **changes):
        """A deletion request for ``target`` signed by ``identity``, the way the client builds one."""
        record = {'kind': 'revocation', 'subject': f'attest:{target}', 'attested_at': records.now(), **changes}
        return {'record': record, 'signature': identity.sign(records.signing_payload(record)), 'signed_by': identity.identity_id}

    def test_signer_deletes(self):
        self.assertEqual(self.mine, self.client.revoke(self.mine, 'wrong file', self.alice))
        self.assertRefused(404, self.client.get, self.mine)
        self.assertRefused(404, self.client._call, 'GET', self.mine)
        self.assertEqual({self.theirs, self.anon}, {r['id'] for r in self.client.for_node('irds:antique-docs')})
        self.assertNotIn(self.mine, self.client.ids())

    def test_it_leaves_a_tombstone_that_readers_syncing_from_before_see(self):
        before = max(r['seq'] for r in self.client.records())
        self.client.revoke(self.mine, identity=self.alice)
        [gone] = list(self.client.records(after=before))
        self.assertEqual({'id': self.mine, 'type': 'hash', 'seq': gone['seq'], 'deleted': True}, gone)   # no content
        self.assertGreater(gone['seq'], before)
        rows = {r['id']: r for r in self.client.records()}
        self.assertTrue(rows[self.mine]['deleted'])
        self.assertNotIn('record', rows[self.mine])
        light = {a['id']: a for a in self.client._call('GET', '')['attestations']}
        self.assertTrue(light[self.mine]['deleted'])

    def test_the_content_is_erased_from_the_database(self):
        self.client.revoke(self.mine, identity=self.alice)
        row = self.store._db.execute('SELECT * FROM attestation WHERE id = ?', (self.mine,)).fetchone()
        self.assertEqual(('{}', None, None), (row['record'], row['signature'], row['subject']))
        self.assertIsNotNone(row['deleted_at'])

    def test_only_the_signer_may_delete(self):
        self.assertRefused(403, self.post, 'revoke', self.request(self.mine, self.bob))     # bob's key, alice's attestation
        with self.assertRaises(ValueError):                                                  # the client says so before asking
            self.client.revoke(self.mine, identity=self.bob)
        self.assertIn(self.mine, self.client.ids())

    def test_anonymous_attestations_and_identities_cannot_be_deleted(self):
        self.assertRefused(422, self.post, 'revoke', self.request(self.anon, self.alice))
        with self.assertRaises(ValueError):
            self.client.revoke(self.anon, identity=self.alice)
        self.assertRefused(422, self.post, 'revoke', self.request(self.alice.identity_id, self.alice))
        with self.assertRaises(ValueError):
            self.client.revoke(self.alice.identity_id, identity=self.alice)

    def test_the_request_must_really_be_signed_by_that_key(self):
        request = self.request(self.mine, self.alice)
        self.assertRefused(422, self.post, 'revoke', {**request, 'signature': self.bob.sign(b'whatever')})    # not alice's signature
        forged = self.request(self.mine, self.alice)
        forged['record'] = {**forged['record'], 'subject': f'attest:{self.theirs}'}                          # signature for another target
        forged['signed_by'] = self.bob.identity_id
        self.assertRefused(422, self.post, 'revoke', forged)
        self.assertRefused(422, self.post, 'revoke', {'record': request['record']})                          # unsigned
        self.assertRefused(422, self.post, 'revoke', {**request, 'record': {**request['record'], 'subject': 'irds:antique-docs'}})
        self.assertIn(self.mine, self.client.ids())

    def test_gone_means_gone(self):
        request = self.request(self.mine, self.alice)
        self.post('revoke', request)
        self.assertRefused(404, self.post, 'revoke', request)     # replaying it finds nothing
        self.assertRefused(404, self.post, 'revoke', self.request('00000000-0000-0000-0000-000000000000', self.alice))


class TestOldDatabase(unittest.TestCase):
    def test_a_database_from_before_deletion_is_upgraded(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'old.db')
            db = sqlite3.connect(path)
            db.executescript('''CREATE TABLE attestation (id TEXT PRIMARY KEY, kind TEXT NOT NULL, subject TEXT, record TEXT NOT NULL,
                signature TEXT, signed_by TEXT, verified INTEGER NOT NULL DEFAULT 0, received_at TEXT NOT NULL, seq INTEGER NOT NULL);
                INSERT INTO attestation VALUES ('a', 'hash', 'irds:x', '{}', NULL, NULL, 0, 'now', 1);''')
            db.commit()
            db.close()
            old = store.Store(path)
            self.assertEqual('a', old.get('a')['id'])
            self.assertTrue(old.delete('a'))
            self.assertIsNone(old.get('a'))


if __name__ == '__main__':
    unittest.main()
