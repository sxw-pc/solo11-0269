"""A synthetic pinned checkpoint medium with a hard physical page budget."""
import json

from .store import one

SCHEMA = '''
CREATE TABLE IF NOT EXISTS boot_media (
    id TEXT PRIMARY KEY, variant TEXT NOT NULL, pages TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS read_reservations (
    id TEXT PRIMARY KEY, medium TEXT NOT NULL, allowance INTEGER NOT NULL, spent INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS resident_pages (
    reservation TEXT NOT NULL, page TEXT NOT NULL, body TEXT NOT NULL,
    PRIMARY KEY (reservation,page)
);
'''


def handle(db, event):
    operation = event['op']
    if operation == 'medium-seal':
        if one(db, 'SELECT 1 FROM boot_media WHERE id=?', (event['id'],)):
            return {'decision': 'medium_exists'}
        if not isinstance(event['pages'], dict) or not event['pages'] or not all(
                isinstance(key, str) and key and isinstance(value, str)
                for key, value in event['pages'].items()):
            return {'decision': 'medium_invalid'}
        db.execute('INSERT INTO boot_media VALUES (?,?,?)',
                   (event['id'], event['variant'], json.dumps(event['pages'], sort_keys=True)))
        return {'decision': 'medium_sealed'}
    if operation == 'read-open':
        if (not one(db, 'SELECT 1 FROM boot_media WHERE id=?', (event['medium'],))
                or one(db, 'SELECT 1 FROM read_reservations WHERE id=?', (event['id'],))
                or type(event['allowance']) is not int or event['allowance'] < 0):
            return {'decision': 'read_denied'}
        db.execute('INSERT INTO read_reservations VALUES (?,?,?,0)',
                   (event['id'], event['medium'], event['allowance']))
        return {'decision': 'read_open'}
    if operation != 'read-page':
        return {'decision': 'unknown_operation'}
    reservation = one(db, 'SELECT * FROM read_reservations WHERE id=?', (event['reservation'],))
    if not reservation:
        return {'decision': 'read_denied'}
    medium = one(db, 'SELECT * FROM boot_media WHERE id=?', (reservation['medium'],))
    pages = json.loads(medium['pages'])
    page = event['page']
    if page not in pages:
        return {'decision': 'page_unavailable'}
    resident = one(db, 'SELECT body FROM resident_pages WHERE reservation=? AND page=?',
                   (reservation['id'], page))
    if resident:
        return {'decision': 'read_resident', 'body': resident['body'], 'spent': reservation['spent']}
    if reservation['spent'] >= reservation['allowance']:
        return {'decision': 'read_budget_exhausted'}
    db.execute('INSERT INTO resident_pages VALUES (?,?,?)', (reservation['id'], page, pages[page]))
    db.execute('UPDATE read_reservations SET spent=spent+1 WHERE id=?', (reservation['id'],))
    return {'decision': 'page_fetched', 'body': pages[page], 'spent': reservation['spent'] + 1}