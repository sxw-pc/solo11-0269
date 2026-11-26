"""Export reproducible synthetic dirty-page round artifacts."""
import json

from . import journal
from .store import one

SCHEMA = '''
CREATE TABLE IF NOT EXISTS page_rounds (
    id TEXT PRIMARY KEY, source TEXT, tick INTEGER, base TEXT, pages TEXT
);
'''


def handle(db, event):
    if event['op'] != 'round-export':
        return {'decision': 'unknown_operation'}
    source = one(db,'SELECT * FROM page_sources WHERE id=?',(event['source'],))
    if not source or one(db,'SELECT 1 FROM page_rounds WHERE id=?',(event['id'],)):
        return {'decision': 'round_denied'}
    tick = source['head']
    base_id = event.get('base')
    old = {}
    if base_id is not None:
        base = one(db,'SELECT * FROM page_rounds WHERE id=? AND source=?',(base_id,source['id']))
        if not base:
            return {'decision': 'round_base_invalid'}
        old = journal.image_at(db,source['id'],base['tick'])
    current = journal.image_at(db,source['id'],tick)
    delta = {str(page): payload.hex() for page,payload in current.items() if payload != old.get(page)}
    db.execute('INSERT INTO page_rounds VALUES (?,?,?,?,?)',
               (event['id'],source['id'],tick,base_id,json.dumps(delta)))
    return {'decision': 'round_exported','tick':tick,'pages':delta,'actual_bytes':len(delta)*source['width']}
