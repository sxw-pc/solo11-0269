"""Local target-side copy custody for checkpoint restore."""
from hashlib import sha256

from .store import one, rows

SCHEMA = '''
CREATE TABLE IF NOT EXISTS target_copies (
    plan TEXT PRIMARY KEY, manifest TEXT, target TEXT, status TEXT, proved_at INTEGER
);
CREATE TABLE IF NOT EXISTS target_parts (
    plan TEXT, part INTEGER, content TEXT, digest TEXT, received_at INTEGER,
    PRIMARY KEY (plan, part)
);
'''


def transfer(db, event):
    plan = one(db, 'SELECT * FROM transfer_plans WHERE id=?', (event['plan'],))
    if not plan or plan['status'] not in ('reserved', 'frozen') or event['target'] != plan['target']:
        return {'decision': 'copy_denied'}
    reservation = one(db, "SELECT * FROM target_reservations WHERE plan=? AND status='active'", (plan['id'],))
    if not reservation or event['at'] >= reservation['expires']:
        return {'decision': 'copy_expired'}
    copy = one(db, 'SELECT * FROM target_copies WHERE plan=?', (plan['id'],))
    if event['op'] == 'copy-open':
        manifest = one(db, "SELECT * FROM manifests WHERE id=? AND request=? AND status='sealed'", (event['manifest'], plan['request']))
        if copy or not manifest:
            return {'decision': 'copy_denied'}
        db.execute('INSERT INTO target_copies VALUES (?,?,?,?,NULL)', (plan['id'], manifest['id'], plan['target'], 'open'))
        return {'decision': 'copy_open'}
    if not copy or copy['status'] != 'open':
        return {'decision': 'copy_denied'}
    manifest = one(db, 'SELECT * FROM manifests WHERE id=?', (copy['manifest'],))
    if event['op'] == 'copy-part':
        part = event['part']
        expected = one(db, 'SELECT digest FROM chunks WHERE manifest=? AND part=?', (manifest['id'], part))
        actual = sha256(event['content'].encode()).hexdigest()
        if not expected or expected['digest'] != actual:
            return {'decision': 'copy_part_mismatch'}
        prior = one(db, 'SELECT * FROM target_parts WHERE plan=? AND part=?', (plan['id'], part))
        if prior:
            return {'decision': 'copy_part_same'}
        db.execute('INSERT INTO target_parts VALUES (?,?,?,?,?)', (plan['id'], part, event['content'], actual, event['at']))
        return {'decision': 'copy_part_stored'}
    parts = rows(db, 'SELECT * FROM target_parts WHERE plan=? ORDER BY part', (plan['id'],))
    if [row['part'] for row in parts] != list(range(manifest['part_count'])):
        return {'decision': 'copy_incomplete'}
    actual = sha256(''.join(row['content'] for row in parts).encode()).hexdigest()
    if actual != manifest['digest'] or any(row['received_at'] > event['at'] for row in parts):
        return {'decision': 'copy_unproven'}
    db.execute("UPDATE target_copies SET status='verified',proved_at=? WHERE plan=?", (event['at'], plan['id']))
    return {'decision': 'copy_verified', 'bytes': sum(len(row['content'].encode()) for row in parts)}


def authorizes_restore(db, plan, manifest, at):
    copy = one(db, 'SELECT * FROM target_copies WHERE plan=?', (plan['id'],))
    return copy is None or (
        copy['manifest'] == manifest['id'] and copy['target'] == plan['target']
        and copy['status'] == 'verified' and copy['proved_at'] <= at
    )
