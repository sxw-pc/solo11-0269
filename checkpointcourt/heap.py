"""Inspect an offline heap catalog captured by a synthetic application."""
import json

from .store import one, rows

SCHEMA = '''
CREATE TABLE IF NOT EXISTS heap_catalogs (
    id TEXT PRIMARY KEY, capture TEXT, roots TEXT, objects TEXT
);
'''


def valid_id(value):
    return isinstance(value, str) and bool(value)


def validate(catalog):
    if not isinstance(catalog, dict) or set(catalog) != {'roots', 'objects'}:
        return False
    roots, objects = catalog['roots'], catalog['objects']
    if not isinstance(roots, list) or not isinstance(objects, list) or not objects:
        return False
    names = set()
    for node in objects:
        if not isinstance(node, dict) or set(node) != {'id', 'bytes', 'strong'}:
            return False
        if not valid_id(node['id']) or node['id'] in names:
            return False
        if type(node['bytes']) is not int or node['bytes'] < 0 or not isinstance(node['strong'], list):
            return False
        names.add(node['id'])
    references = [*roots, *(ref for node in objects for ref in node['strong'])]
    return all(valid_id(ref) and ref in names for ref in references)


def load_catalog(db, event):
    capture = one(db, 'SELECT 1 FROM memory_captures WHERE id=?', (event['capture'],))
    if not capture:
        return {'decision': 'heap_capture_missing'}
    if one(db, 'SELECT 1 FROM heap_catalogs WHERE id=?', (event['id'],)):
        return {'decision': 'heap_catalog_exists'}
    spans = rows(db, "SELECT offset,payload FROM memory_spans WHERE capture=? AND region='heap-catalog' ORDER BY offset",
                 (event['capture'],))
    assembled = bytearray()
    for span in spans:
        if span['offset'] != len(assembled):
            return {'decision': 'heap_catalog_gap'}
        assembled.extend(bytes.fromhex(span['payload']))
    try:
        catalog = json.loads(assembled.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        return {'decision': 'heap_catalog_invalid'}
    if not validate(catalog):
        return {'decision': 'heap_catalog_invalid'}
    db.execute('INSERT INTO heap_catalogs VALUES (?,?,?,?)',
               (event['id'], event['capture'], json.dumps(catalog['roots']), json.dumps(catalog['objects'])))
    return {'decision': 'heap_catalog_loaded', 'objects': len(catalog['objects'])}


def live_objects(roots, objects):
    found = set()
    pending = list(roots)
    while pending:
        node = pending.pop()
        if node not in found:
            found.add(node)
            pending.extend(objects[node]['strong'])
    return found


def census(db, event):
    catalog = one(db, 'SELECT * FROM heap_catalogs WHERE id=?', (event['catalog'],))
    if not catalog:
        return {'decision': 'heap_catalog_missing'}
    objects = {node['id']: node for node in json.loads(catalog['objects'])}
    live = live_objects(json.loads(catalog['roots']), objects)
    unreachable = objects.keys() - live
    return {'decision': 'heap_counted', 'live': sorted(live), 'unreachable': sorted(unreachable),
            'live_bytes': sum(objects[name]['bytes'] for name in live),
            'unreachable_bytes': sum(objects[name]['bytes'] for name in unreachable)}


def handle(db, event):
    operation = event['op']
    if operation == 'catalog-load':
        return load_catalog(db, event)
    if operation == 'heap-census':
        return census(db, event)
    return {'decision': 'unknown_operation'}
