"""Record which synthetic first request actually ran from resident pages."""
import json

from .store import one, rows

SCHEMA = '''
CREATE TABLE IF NOT EXISTS first_request_profiles (
    id TEXT PRIMARY KEY, variant TEXT NOT NULL, pages TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS warmup_receipts (
    id TEXT PRIMARY KEY, reservation TEXT NOT NULL, profile TEXT NOT NULL, reply TEXT NOT NULL
);
'''


def handle(db, event):
    operation = event['op']
    if operation == 'profile-publish':
        if one(db, 'SELECT 1 FROM first_request_profiles WHERE id=?', (event['id'],)):
            return {'decision': 'profile_exists'}
        requested = event['pages']
        if not isinstance(requested, list) or not requested or not all(isinstance(p,str) and p for p in requested):
            return {'decision': 'profile_invalid'}
        if len(set(requested)) != len(requested):
            return {'decision': 'profile_invalid'}
        db.execute('INSERT INTO first_request_profiles VALUES (?,?,?)',
                   (event['id'], event['variant'], json.dumps(requested)))
        return {'decision': 'profile_published'}
    if operation != 'serve-first':
        return {'decision': 'unknown_operation'}
    reader = one(db, '''SELECT r.*,b.variant FROM read_reservations r
                       JOIN boot_media b ON b.id=r.medium WHERE r.id=?''', (event['reservation'],))
    profile = one(db, 'SELECT * FROM first_request_profiles WHERE id=?', (event['profile'],))
    if (not reader or not profile or reader['variant'] != profile['variant']
            or one(db, 'SELECT 1 FROM warmup_receipts WHERE id=? OR reservation=?', (event['id'], event['reservation']))):
        return {'decision': 'serve_denied'}
    resident = {row['page']:row['body'] for row in rows(
        db, 'SELECT page,body FROM resident_pages WHERE reservation=?', (reader['id'],))}
    requested = json.loads(profile['pages'])
    missing = [key for key in requested if key not in resident]
    if missing:
        return {'decision': 'first_miss', 'missing': missing}
    reply = [resident[key] for key in requested]
    db.execute('INSERT INTO warmup_receipts VALUES (?,?,?,?)',
               (event['id'], reader['id'], profile['id'], json.dumps(reply)))
    return {'decision': 'first_served', 'reply': reply, 'reads': reader['spent']}