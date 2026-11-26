"""Recover one missing archive warehouse member from sealed source parity."""
import hashlib
import json

from .store import one, rows

SCHEMA = '''
CREATE TABLE IF NOT EXISTS archive_stripes (
    id TEXT PRIMARY KEY, manifest TEXT UNIQUE, checks TEXT, parity BLOB
);
CREATE TABLE IF NOT EXISTS archive_members (
    stripe TEXT, part INTEGER, payload BLOB, origin TEXT,
    PRIMARY KEY (stripe, part)
);
'''


def xor_members(payloads, width):
    parity = bytearray(width)
    for payload in payloads:
        for index, value in enumerate(payload):
            parity[index] ^= value
    return bytes(parity)


def seal_stripe(db, event):
    manifest = one(db, "SELECT * FROM manifests WHERE id=? AND status='sealed'", (event['manifest'],))
    if not manifest:
        return {'decision': 'archive_source_unsealed'}
    if one(db, 'SELECT 1 FROM archive_stripes WHERE id=? OR manifest=?', (event['id'], event['manifest'])):
        return {'decision': 'archive_stripe_exists'}
    source = rows(db, 'SELECT content,digest FROM chunks WHERE manifest=? ORDER BY part', (manifest['id'],))
    payloads = [part['content'].encode('utf-8') for part in source]
    checks = [{'length': len(payload), 'digest': part['digest']} for payload, part in zip(payloads, source)]
    parity = xor_members(payloads, max(map(len, payloads)))
    db.execute('INSERT INTO archive_stripes VALUES (?,?,?,?)',
               (event['id'], manifest['id'], json.dumps(checks), parity))
    return {'decision': 'archive_stripe_sealed', 'parts': len(checks), 'parity_bytes': len(parity)}


def verify_payload(payload, check):
    return len(payload) == check['length'] and hashlib.sha256(payload).hexdigest() == check['digest']


def receive(db, event, stripe, checks):
    part = event['part']
    if type(part) is not int or not 0 <= part < len(checks) or not isinstance(event['content'], str):
        return {'decision': 'archive_part_denied'}
    payload = event['content'].encode('utf-8')
    if not verify_payload(payload, checks[part]):
        return {'decision': 'archive_part_mismatch'}
    cursor = db.execute('INSERT OR IGNORE INTO archive_members VALUES (?,?,?,?)',
                        (stripe['id'], part, payload, 'received'))
    return {'decision': 'archive_part_received' if cursor.rowcount else 'archive_part_already_present'}


def repair(db, stripe, checks):
    members = rows(db, 'SELECT part,payload FROM archive_members WHERE stripe=? ORDER BY part', (stripe['id'],))
    missing = sorted(set(range(len(checks))) - {member['part'] for member in members})
    if not missing:
        return {'decision': 'archive_complete'}
    if len(missing) > 1:
        return {'decision': 'archive_insufficient', 'missing': missing}
    part = missing[0]
    block = xor_members([stripe['parity'], *(member['payload'] for member in members)], len(stripe['parity']))
    payload = block[:checks[part]['length']]
    if not verify_payload(payload, checks[part]):
        return {'decision': 'archive_repair_mismatch'}
    db.execute('INSERT INTO archive_members VALUES (?,?,?,?)', (stripe['id'], part, payload, 'recovered'))
    return {'decision': 'archive_part_repaired', 'part': part, 'payload_hex': payload.hex()}


def handle(db, event):
    op = event['op']
    if op == 'stripe-seal':
        return seal_stripe(db, event)
    if op not in ('member-receive', 'stripe-repair'):
        return {'decision': 'unknown_operation'}
    stripe = one(db, 'SELECT * FROM archive_stripes WHERE id=?', (event['stripe'],))
    if not stripe:
        return {'decision': 'archive_stripe_missing'}
    checks = json.loads(stripe['checks'])
    if op == 'member-receive':
        return receive(db, event, stripe, checks)
    return repair(db, stripe, checks)
