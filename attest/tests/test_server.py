"""End to end: the real server, in a thread, against the real client from ir_datasets.v2.attest."""
import os
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
        self.httpd = server.make(store.Store(), port=0, quiet=True)
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


if __name__ == '__main__':
    unittest.main()
