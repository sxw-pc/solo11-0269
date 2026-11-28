"""Persist synthetic sender and receiver prefixes independently."""
from .store import one, rows

SCHEMA = '''
CREATE TABLE IF NOT EXISTS mail_ports (
    id TEXT PRIMARY KEY, seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS mail_items (
    id TEXT PRIMARY KEY, sender TEXT NOT NULL, receiver TEXT NOT NULL,
    sent INTEGER NOT NULL, arrived INTEGER, body TEXT NOT NULL,
    instruction TEXT, batch TEXT, generation TEXT
);
CREATE TABLE IF NOT EXISTS mail_cuts (
    id TEXT PRIMARY KEY, port TEXT NOT NULL, seq INTEGER NOT NULL
);
'''


def port(db, key):
    return one(db, 'SELECT * FROM mail_ports WHERE id=?', (key,))


def tick(db, current):
    value = current['seq'] + 1
    db.execute('UPDATE mail_ports SET seq=? WHERE id=?', (value, current['id']))
    return value


def handle(db, event):
    operation = event['op']
    current = port(db, event['port'])
    if operation == 'port-open':
        if current:
            return {'decision': 'port_already_open'}
        db.execute('INSERT INTO mail_ports VALUES (?,0)', (event['port'],))
        return {'decision': 'port_open'}
    if not current:
        return {'decision': 'port_missing'}
    if operation == 'cut':
        if one(db, 'SELECT 1 FROM mail_cuts WHERE id=?', (event['id'],)):
            return {'decision': 'cut_exists'}
        db.execute('INSERT INTO mail_cuts VALUES (?,?,?)', (event['id'], current['id'], current['seq']))
        return {'decision': 'cut_stored', 'seq': current['seq']}
    if operation == 'ledger-export':
        cuts = rows(db, 'SELECT id, seq FROM mail_cuts WHERE port=? ORDER BY id', (current['id'],))
        items = rows(db, '''SELECT id, sent, receiver, generation, instruction, batch, body
                           FROM mail_items WHERE sender=? ORDER BY sent''', (current['id'],))
        return {'decision': 'ledger_export', 'port': current['id'], 'seq': current['seq'],
                'cuts': cuts, 'items': items}
    item = one(db, 'SELECT * FROM mail_items WHERE id=?', (event['id'],))
    if operation == 'send':
        receiver = port(db, event['receiver'])
        instruction = event.get('instruction')
        batch = event.get('batch')
        generation = event.get('generation')
        if (item or not receiver or receiver == current or not isinstance(event['body'], str)
                or (instruction is not None and not isinstance(instruction, str))
                or (batch is not None and not isinstance(batch, str))
                or (generation is not None and not isinstance(generation, str))):
            return {'decision': 'send_denied'}
        seq = tick(db, current)
        db.execute('INSERT INTO mail_items VALUES (?,?,?,?,NULL,?,?,?,?)',
                   (event['id'], current['id'], receiver['id'], seq, event['body'],
                    instruction if instruction is not None else event['body'],
                    batch, generation))
        return {'decision': 'mail_sent', 'seq': seq}
    if operation == 'arrive':
        if not item or item['receiver'] != current['id'] or item['arrived'] is not None:
            return {'decision': 'arrival_denied'}
        seq = tick(db, current)
        db.execute('UPDATE mail_items SET arrived=? WHERE id=?', (seq, item['id']))
        return {'decision': 'mail_arrived', 'seq': seq}
    return {'decision': 'unknown_operation'}
