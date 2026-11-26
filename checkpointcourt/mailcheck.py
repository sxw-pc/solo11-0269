"""Materialize the pending inbox for one explicitly chosen pair of cuts."""
from .store import one, rows

SCHEMA = '''
CREATE TABLE IF NOT EXISTS inbox_packages (
    id TEXT PRIMARY KEY, sender_cut TEXT NOT NULL, receiver_cut TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbox_pending (
    package TEXT NOT NULL, delivery TEXT NOT NULL, position INTEGER NOT NULL,
    body TEXT NOT NULL, consumed INTEGER NOT NULL,
    PRIMARY KEY(package, delivery)
);
'''


def recover(db, event):
    sender = one(db, 'SELECT * FROM mail_cuts WHERE id=?', (event['sender_cut'],))
    receiver = one(db, 'SELECT * FROM mail_cuts WHERE id=?', (event['receiver_cut'],))
    if not sender or not receiver or sender['port'] == receiver['port']:
        return {'decision': 'cuts_invalid'}
    if one(db, 'SELECT 1 FROM inbox_packages WHERE id=?', (event['id'],)):
        return {'decision': 'inbox_exists'}
    items = rows(db, 'SELECT * FROM mail_items WHERE sender=? AND receiver=? ORDER BY sent',
                 (sender['port'], receiver['port']))
    pending = []
    for item in items:
        sent = item['sent'] <= sender['seq']
        received = item['arrived'] is not None and item['arrived'] <= receiver['seq']
        if received and not sent:
            return {'decision': 'inbox_inconsistent', 'delivery': item['id']}
        if sent and not received:
            pending.append(item)
    db.execute('INSERT INTO inbox_packages VALUES (?,?,?)', (event['id'], sender['id'], receiver['id']))
    db.executemany('INSERT INTO inbox_pending VALUES (?,?,?,?,0)',
                   ((event['id'], item['id'], n, item['body']) for n, item in enumerate(pending)))
    return {'decision': 'inbox_ready', 'pending': [item['id'] for item in pending]}


def deliver(db, event):
    if not one(db, 'SELECT 1 FROM inbox_packages WHERE id=?', (event['id'],)):
        return {'decision': 'inbox_missing'}
    item = one(db, '''SELECT * FROM inbox_pending WHERE package=? AND consumed=0
                     ORDER BY position LIMIT 1''', (event['id'],))
    if not item:
        return {'decision': 'inbox_empty'}
    db.execute('UPDATE inbox_pending SET consumed=1 WHERE package=? AND delivery=?',
               (event['id'], item['delivery']))
    return {'decision': 'mail_delivered', 'delivery': item['delivery'], 'body': item['body']}


def handle(db, event):
    operation = event['op']
    if operation == 'inbox-recover':
        return recover(db, event)
    if operation == 'inbox-deliver':
        return deliver(db, event)
    return {'decision': 'unknown_operation'}