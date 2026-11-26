"""Record byte differences between captures of one process identity."""
import json

from .memorymap import read_cells
from .store import one

SCHEMA = '''
CREATE TABLE IF NOT EXISTS capture_comparisons (
    id TEXT PRIMARY KEY, older TEXT, newer TEXT, differences TEXT
);
'''


def handle(db, event):
    if event['op'] != 'capture-compare':
        return {'decision': 'unknown_operation'}
    older = one(db, 'SELECT * FROM memory_captures WHERE id=?', (event['older'],))
    newer = one(db, 'SELECT * FROM memory_captures WHERE id=?', (event['newer'],))
    if not older or not newer or (older['pod'],older['uid']) != (newer['pod'],newer['uid']) or older['generation'] >= newer['generation']:
        return {'decision': 'comparison_denied'}
    if one(db, 'SELECT 1 FROM capture_comparisons WHERE id=?', (event['id'],)):
        return {'decision': 'comparison_exists'}
    before, after = read_cells(db, older['id']), read_cells(db, newer['id'])
    changes = [{'region': region,'offset':offset,'before':before.get((region,offset)),'after':after.get((region,offset))}
               for region,offset in sorted(before.keys() | after.keys()) if before.get((region,offset)) != after.get((region,offset))]
    db.execute('INSERT INTO capture_comparisons VALUES (?,?,?,?)', (event['id'], older['id'], newer['id'], json.dumps(changes)))
    return {'decision': 'comparison_recorded', 'differences': changes}
