"""Capture a logical memory map from sealed synthetic checkpoint bytes."""
import json

from .store import one, rows

SCHEMA = '''
CREATE TABLE IF NOT EXISTS memory_captures (
    id TEXT PRIMARY KEY, manifest TEXT, pod TEXT, uid TEXT, generation INTEGER
);
CREATE TABLE IF NOT EXISTS memory_spans (
    capture TEXT, region TEXT, offset INTEGER, payload TEXT,
    PRIMARY KEY (capture, region, offset)
);
'''


def handle(db, event):
    if event['op'] != 'capture-read':
        return {'decision': 'unknown_operation'}
    manifest = one(db, '''
        SELECT m.id,r.pod,r.uid,r.generation FROM manifests m
        JOIN checkpoint_requests r ON r.id=m.request WHERE m.id=? AND m.status='sealed'
    ''', (event['manifest'],))
    if not manifest or one(db, 'SELECT 1 FROM memory_captures WHERE id=?', (event['id'],)):
        return {'decision': 'capture_denied'}
    pieces = rows(db, 'SELECT content FROM chunks WHERE manifest=? ORDER BY part', (manifest['id'],))
    try:
        spans = json.loads(''.join(row['content'] for row in pieces))
    except ValueError:
        return {'decision': 'capture_invalid'}
    if not isinstance(spans, list) or not spans:
        return {'decision': 'capture_invalid'}
    cells=set()
    for span in spans:
        if not isinstance(span, dict) or set(span) != {'region','offset','hex'}:
            return {'decision': 'capture_invalid'}
        region, offset = span['region'], span['offset']
        if not isinstance(region,str) or not region or type(offset) is not int or offset<0:
            return {'decision': 'capture_invalid'}
        try:
            data = bytes.fromhex(span['hex'])
        except (TypeError, ValueError):
            return {'decision': 'capture_invalid'}
        addresses = {(region,i) for i in range(offset, offset+len(data))}
        if not data or cells & addresses:
            return {'decision': 'capture_overlaps'}
        cells.update(addresses)
    db.execute('INSERT INTO memory_captures VALUES (?,?,?,?,?)',
               (event['id'],manifest['id'],manifest['pod'],manifest['uid'],manifest['generation']))
    db.executemany('INSERT INTO memory_spans VALUES (?,?,?,?)',
                   ((event['id'],span['region'],span['offset'],bytes.fromhex(span['hex']).hex()) for span in spans))
    return {'decision': 'capture_read', 'byte_count': len(cells)}


def read_cells(db, capture):
    return {(span['region'], span['offset']+index):value
            for span in rows(db, 'SELECT * FROM memory_spans WHERE capture=?', (capture,))
            for index,value in enumerate(bytes.fromhex(span['payload']))}
