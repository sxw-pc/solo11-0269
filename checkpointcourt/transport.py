"""Charge actual synthetic wire bytes to a local transfer window."""
from . import custody
from .store import one

SCHEMA = '''
CREATE TABLE IF NOT EXISTS windowed_plans (plan TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS wire_windows (
    id TEXT PRIMARY KEY, target TEXT, starts INTEGER, ends INTEGER, budget INTEGER, used INTEGER
);
CREATE TABLE IF NOT EXISTS wire_deliveries (
    id TEXT PRIMARY KEY, window TEXT, plan TEXT, part INTEGER, octets INTEGER, outcome TEXT
);
'''


def handle(db, event):
    operation = event['op']
    if operation == 'window-open':
        starts, ends, budget = event['starts'], event['ends'], event['budget']
        if not all(type(v) is int for v in (starts, ends, budget)) or starts >= ends or budget <= 0:
            return {'decision': 'wire_window_invalid'}
        if one(db, 'SELECT 1 FROM wire_windows WHERE id=?', (event['id'],)):
            return {'decision': 'wire_window_exists'}
        db.execute('INSERT INTO wire_windows VALUES (?,?,?,?,?,0)', (event['id'], event['target'], starts, ends, budget))
        return {'decision': 'wire_window_open'}
    if operation != 'part-send':
        return {'decision': 'unknown_operation'}
    window = one(db, 'SELECT * FROM wire_windows WHERE id=?', (event['window'],))
    if not window or event['target'] != window['target'] or not window['starts'] <= event['at'] < window['ends']:
        return {'decision': 'wire_outside_window'}
    octets = len(event['content'].encode())
    if not octets:
        return {'decision': 'wire_empty'}
    if one(db, 'SELECT 1 FROM wire_deliveries WHERE id=?', (event['id'],)):
        return {'decision': 'wire_delivery_exists'}
    if octets > window['budget'] - window['used']:
        return {'decision': 'wire_capacity_exceeded', 'remaining': window['budget'] - window['used']}
    result = custody.transfer(db, {**event, 'op': 'copy-part'})
    db.execute('UPDATE wire_windows SET used=used+? WHERE id=?', (octets, window['id']))
    db.execute('INSERT INTO wire_deliveries VALUES (?,?,?,?,?,?)', (event['id'], window['id'], event['plan'], event['part'], octets, result['decision']))
    return {'decision': 'wire_sent', 'receipt': result['decision'], 'remaining': window['budget'] - window['used'] - octets}
