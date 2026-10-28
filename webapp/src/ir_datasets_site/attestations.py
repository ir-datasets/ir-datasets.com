"""What the site says about attestations: turning the attestation records about a node into the
summary its Verification card shows. Pure functions over plain dicts, so no Flask and no graph
store is needed to test them.

An attestation row is ``{'id', 'kind', 'subject', 'record', 'signed', 'verified', 'signer'}`` where ``kind`` is
``hash``/``visual``/``evaluation``/``identity``, ``record`` the record's JSON, ``signed`` whether it
has a signature, ``verified`` whether the server checked that signature on receipt, and ``signer``
the signing identity's username (or ``None``).
"""
import collections
import datetime
import itertools

KINDS = ('hash', 'visual', 'evaluation', 'identity')

#: Overall status, most important first. ``verified`` needs agreement among more than one
#: attestation, or one that matches the hash the node declares: a single anonymous hash proves little.
STATUSES = {
    'none': ('Not yet attested', 'bg-yellow-lt', 'Nobody has attested to this yet.'),
    'conflict': ('Hash conflict', 'bg-red-lt', 'Attestations disagree about the data, or differ from the hash the node declares.'),
    'issues': ('Issues reported', 'bg-orange-lt', 'Someone who inspected the data reported problems.'),
    'verified': ('Verified', 'bg-green-lt', 'Hash attestations agree with each other or with the declared hash.'),
    'evaluated': ('Evaluated', 'bg-azure-lt', 'Systems have reported scores on this benchmark; compare them with your own runs.'),
    'attested': ('Attested', 'bg-secondary-lt', 'Attested, but not enough to cross-check against anything.'),
}


def kind_of(type_name):
    """``'attest:HashAttestation'`` -> ``'hash'``."""
    local = type_name.split(':', 1)[-1]
    kind = local[:-len('Attestation')].lower() if local.endswith('Attestation') else local.lower()
    return kind if kind in KINDS else None


def _date(record):
    return (record.get('attested_at') or '')[:10]


def _who(row):
    """Who made an attestation: its signer's username, or ``None`` if it is anonymous."""
    return row.get('signer') if row.get('signed') else None


def ago(date, today=None):
    """``'2026-10-28'`` as ``'3 days ago'`` (``'today'`` for today; months and years once it has been a while)."""
    today = today or datetime.date.today()
    days = (today - datetime.date.fromisoformat(date)).days
    if days <= 0:
        return 'today'
    for unit, size in (('year', 365), ('month', 30), ('day', 1)):
        if days >= size:
            n = days // size
            return f"{n} {unit}{'' if n == 1 else 's'} ago"


def _parse_hash(spec):
    algo, _, digest = str(spec).partition(':')
    return algo.lower(), digest.lower()


def _hash_groups(rows, declared):
    """Group hash attestations by what they hash (a file's bytes, or a table's content at one
    ``hash_version``) and algorithm, then by digest. A (scope, algo) with several digests is a
    conflict; a digest differing from the node's declared one for that algorithm is too."""
    scopes = collections.OrderedDict()      # (scope, algo) -> digest -> [rows]
    for row in rows:
        record = row['record']
        scope = f"table v{record['hash_version']}" if 'hash_version' in record else 'file'
        for spec in record.get('hashes') or []:
            algo, digest = _parse_hash(spec)
            scopes.setdefault((scope, algo), collections.OrderedDict()).setdefault(digest, []).append(row)
    declared = dict(_parse_hash(d) for d in declared or ())
    out, conflict = [], False
    for (scope, algo), digests in sorted(scopes.items()):
        # a file's declared hash applies to the file's bytes, not to a table's content
        expected = declared.get(algo) if scope == 'file' else None
        disagree = len(digests) > 1
        for digest, hashed_by in sorted(digests.items(), key=lambda kv: -len(kv[1])):
            matches = None if expected is None else digest == expected
            conflict = conflict or disagree or matches is False
            out.append({
                'scope': scope, 'algo': algo, 'digest': digest, 'count': len(hashed_by),
                'signers': sorted({_who(r) for r in hashed_by if _who(r)}),
                'anonymous': sum(1 for r in hashed_by if not r.get('signed')),
                'sources': sorted({r['record'].get('source') for r in hashed_by if r['record'].get('source')}),
                'matches_declared': matches, 'disagrees': disagree,
            })
    return out, conflict, bool(declared) and any(g['matches_declared'] for g in out)


def compare_hash(row, others):
    """How each hash in the hash attestation ``row`` stands against the ``others`` about the same node: for
    every (what it hashes, algorithm) how many others gave the same digest and how many a different one."""
    out = []
    for spec in row['record'].get('hashes') or []:
        algo, digest = _parse_hash(spec)
        scope = f"table v{row['record']['hash_version']}" if 'hash_version' in row['record'] else 'file'
        agree = differ = 0
        for other in others:
            if other['kind'] != 'hash' or other['id'] == row['id']:
                continue
            record = other['record']
            if (f"table v{record['hash_version']}" if 'hash_version' in record else 'file') != scope:
                continue
            for o_spec in record.get('hashes') or []:
                o_algo, o_digest = _parse_hash(o_spec)
                if o_algo == algo:
                    agree, differ = agree + (o_digest == digest), differ + (o_digest != digest)
        out.append({'scope': scope, 'algo': algo, 'digest': digest, 'agree': agree, 'differ': differ})
    return out


def summarize(rows, declared_hashes=(), today=None):
    """The Verification card's content for the attestations ``rows`` about one node (``declared_hashes``:
    the node's own ``algo:hex`` hashes, if it declares any)."""
    rows = [r for r in rows if r['kind'] in ('hash', 'visual', 'evaluation')]
    by_kind = {k: [r for r in rows if r['kind'] == k] for k in ('hash', 'visual', 'evaluation')}
    hashes, conflict, matches_declared = _hash_groups(by_kind['hash'], declared_hashes)
    inspections = sorted(({'who': _who(r), 'verdict': r['record'].get('verdict'),
                           'notes': r['record'].get('notes') or '', 'sample': len(r['record'].get('sample') or []),
                           'date': _date(r['record']), 'id': r['id']} for r in by_kind['visual']),
                         key=lambda v: (v['verdict'] != 'issues', v['date']))
    entries = sorted(({'system': r['record'].get('system'), 'measures': r['record'].get('measures') or {},
                           'who': _who(r), 'date': _date(r['record']), 'id': r['id']} for r in by_kind['evaluation']),
                         key=lambda e: (e['system'] or '', e['date']))
    evaluations = entries
    systems = [{'system': system, 'entries': list(group)}
               for system, group in itertools.groupby(entries, key=lambda e: e['system'])]
    issues = [v for v in inspections if v['verdict'] == 'issues']
    latest = {}
    for kind, kind_rows in by_kind.items():
        if kind_rows:
            newest = max(kind_rows, key=lambda r: r['record'].get('attested_at') or '')
            latest[kind] = {'id': newest['id'], 'date': _date(newest['record']), 'ago': ago(_date(newest['record']), today)}
    # distinct agreeing attestations (a record is one attestation however many algorithms it carries)
    agreeing = len(by_kind['hash'])
    if conflict:
        status = 'conflict'
    elif issues:
        status = 'issues'
    elif by_kind['hash'] and (agreeing >= 2 or matches_declared):
        status = 'verified'
    elif evaluations and not by_kind['hash'] and not by_kind['visual']:
        status = 'evaluated'
    elif not rows:
        status = 'none'
    else:
        status = 'attested'
    # per kind: what this kind of attestation says about the node (hashes and inspections can find issues; scores cannot)
    kind_status = {
        'hash': 'none' if not by_kind['hash'] else 'issues' if conflict else 'ok',
        'visual': 'none' if not by_kind['visual'] else 'issues' if issues else 'ok',
        'evaluation': 'none' if not by_kind['evaluation'] else 'attested',
    }
    label, badge, explanation = STATUSES[status]
    return {
        'total': len(rows), 'counts': {k: len(v) for k, v in by_kind.items()},
        'signed': sum(1 for r in rows if r.get('signed')),
        'status': status, 'label': label, 'badge': badge, 'explanation': explanation,
        'kind_status': kind_status, 'hashes': hashes, 'hash_conflict': conflict, 'latest': latest, 'matches_declared': matches_declared, 'declared': bool(declared_hashes),
        'inspections': inspections, 'evaluations': evaluations, 'systems': systems,
        'measures': sorted({m for e in evaluations for m in e['measures']}),
    }


def overview(rows, recent=12):
    """Site-wide numbers for the attestations page: ``rows`` is every attestation (identities included)."""
    counts = collections.Counter(r['kind'] for r in rows)
    subjects = {r['subject'] for r in rows if r.get('subject')}
    ordered = sorted((r for r in rows if r['kind'] != 'identity'), key=lambda r: r['record'].get('attested_at') or '', reverse=True)
    return {'counts': {k: counts.get(k, 0) for k in KINDS}, 'total': sum(counts.values()),
            'nodes': len(subjects), 'signed': sum(1 for r in rows if r.get('signed') and r['kind'] != 'identity'),
            'recent': ordered[:recent]}
