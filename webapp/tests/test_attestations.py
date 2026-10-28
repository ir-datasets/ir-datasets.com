import unittest

from ir_datasets_site import attestations as at

SHA_A, SHA_B = 'sha256:' + 'a' * 64, 'sha256:' + 'b' * 64


def row(kind, record, signer=None, id=None, subject='irds:x'):
    return {'id': id or f'id-{kind}-{abs(hash(repr(record))) % 1000}', 'kind': kind, 'subject': subject,
            'record': {'attested_at': '2026-10-28T12:00:00Z', **record},
            'signed': bool(signer), 'verified': bool(signer), 'signer': signer}


def hash_row(*hashes, signer=None, **kw):
    return row('hash', {'hashes': list(hashes), **kw}, signer)


class TestSummarize(unittest.TestCase):
    def test_nothing(self):
        s = at.summarize([])
        self.assertEqual(('none', 0), (s['status'], s['total']))

    def test_one_anonymous_hash_is_only_attested(self):
        s = at.summarize([hash_row(SHA_A)])
        self.assertEqual('attested', s['status'])
        self.assertEqual([('file', 'sha256', 1)], [(h['scope'], h['algo'], h['count']) for h in s['hashes']])

    def test_two_agreeing_hashes_verify(self):
        s = at.summarize([hash_row(SHA_A, signer='alice'), hash_row(SHA_A)])
        self.assertEqual('verified', s['status'])
        [h] = s['hashes']
        self.assertEqual((2, ['alice'], 1), (h['count'], h['signers'], h['anonymous']))

    def test_latest_attestation_per_kind(self):
        old, new = hash_row(SHA_A), hash_row(SHA_A)
        old['id'], new['id'] = 'old', 'new'
        old['record']['attested_at'] = '2026-01-02T00:00:00Z'
        new['record']['attested_at'] = '2026-03-04T00:00:00Z'
        s = at.summarize([new, old])
        self.assertEqual({'hash': {'id': 'new', 'date': '2026-03-04'}}, s['latest'])
        self.assertFalse(s['hash_conflict'])

    def test_disagreeing_hashes_conflict(self):
        s = at.summarize([hash_row(SHA_A), hash_row(SHA_B), hash_row(SHA_B)])
        self.assertTrue(s['hash_conflict'])
        self.assertEqual('conflict', s['status'])
        self.assertEqual([2, 1], [h['count'] for h in s['hashes']])      # the common one first
        self.assertTrue(all(h['disagrees'] for h in s['hashes']))

    def test_declared_hash(self):
        md5 = 'md5:' + 'c' * 32
        s = at.summarize([hash_row(SHA_A, md5)], declared_hashes=[md5])
        self.assertEqual(('verified', True), (s['status'], s['matches_declared']))     # one attestation, but it matches
        s = at.summarize([hash_row(SHA_A, md5), hash_row(SHA_A, 'md5:' + 'd' * 32)], declared_hashes=[md5])
        self.assertEqual('conflict', s['status'])
        s = at.summarize([hash_row(SHA_A)], declared_hashes=[md5])                       # no algorithm in common
        self.assertEqual(('attested', False), (s['status'], s['matches_declared']))

    def test_table_hashes_are_grouped_by_version_and_never_compared_with_a_file_hash(self):
        s = at.summarize([hash_row(SHA_A, hash_version=1), hash_row(SHA_A, hash_version=2),
                          hash_row(SHA_B)])
        self.assertEqual({'table v1', 'table v2', 'file'}, {h['scope'] for h in s['hashes']})
        self.assertNotEqual('conflict', s['status'])

    def test_visual(self):
        ok = row('visual', {'verdict': 'ok', 'sample': ['1', '2']}, 'bob')
        bad = row('visual', {'verdict': 'issues', 'notes': 'mojibake', 'sample': ['3']}, 'carol')
        s = at.summarize([hash_row(SHA_A), hash_row(SHA_A), ok, bad])
        self.assertEqual('issues', s['status'])
        self.assertEqual(['issues', 'ok'], [v['verdict'] for v in s['inspections']])       # problems first
        self.assertEqual(('mojibake', 1), (s['inspections'][0]['notes'], s['inspections'][0]['sample']))
        self.assertEqual('attested', at.summarize([ok])['status'])

    def test_conflict_outranks_issues(self):
        s = at.summarize([hash_row(SHA_A), hash_row(SHA_B), row('visual', {'verdict': 'issues'}, 'x')])
        self.assertEqual('conflict', s['status'])

    def test_evaluations(self):
        s = at.summarize([row('evaluation', {'system': 'BM25', 'measures': {'nDCG@10': 0.3, 'MAP': 0.2}}, 'dana'),
                          row('evaluation', {'system': 'ANCE', 'measures': {'nDCG@10': 0.4}})])
        self.assertEqual(['ANCE', 'BM25'], [e['system'] for e in s['evaluations']])
        self.assertEqual(['MAP', 'nDCG@10'], s['measures'])
        self.assertEqual([('ANCE', 1), ('BM25', 1)], [(g['system'], len(g['entries'])) for g in s['systems']])
        self.assertEqual((None, 'dana'), (s['evaluations'][0]['who'], s['evaluations'][1]['who']))
        self.assertEqual('evaluated', s['status'])
        s = at.summarize([hash_row(SHA_A), row('evaluation', {'system': 'x', 'measures': {'m': 1}})])
        self.assertEqual('attested', s['status'])      # an evaluation next to a lone hash is still just a lone hash

    def test_identities_are_ignored(self):
        self.assertEqual(0, at.summarize([row('identity', {'username': 'x'}, subject=None)])['total'])


class TestKind(unittest.TestCase):
    def test_kind_of(self):
        self.assertEqual('hash', at.kind_of('attest:HashAttestation'))
        self.assertEqual('identity', at.kind_of('attest:IdentityAttestation'))
        self.assertIsNone(at.kind_of('irds:Docs'))


class TestOverview(unittest.TestCase):
    def test_overview(self):
        rows = [row('hash', {'hashes': [SHA_A]}, 'a', subject='irds:x'), row('identity', {'username': 'a'}, 'a', subject=None),
                row('evaluation', {'system': 's', 'measures': {'m': 1}}, subject='irds:y')]
        rows[2]['record']['attested_at'] = '2026-10-29T00:00:00Z'
        o = at.overview(rows)
        self.assertEqual((3, 2, 1), (o['total'], o['nodes'], o['signed']))
        self.assertEqual({'hash': 1, 'visual': 0, 'evaluation': 1, 'identity': 1}, o['counts'])
        self.assertEqual(['evaluation', 'hash'], [r['kind'] for r in o['recent']])        # newest first, no identities


if __name__ == '__main__':
    unittest.main()
