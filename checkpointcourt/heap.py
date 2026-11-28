"""Inspect an offline heap catalog captured by a synthetic application.

The catalog models three kinds of reachability:

* ``roots`` are ordinary strong roots; every ordinary strong edge keeps its
  target alive once its source is alive.
* ``handles`` are returnable registered handles.  A handle keeps its target
  alive while it is held; returning the handle removes that seed.  Handle
  state is recorded as runtime events, never by mutating the sealed catalog.
* ``tables`` hold conditional entries.  An entry's key is *not* kept alive by
  being stored in the table.  The entry value becomes live only when the table
  is live and the key already has an independent liveness proof from the real
  root set.  Values in a reference cycle cannot bootstrap keys that no root
  reaches: liveness is the least fixed point seeded by the real roots.

Cleanup plans are simulated first (``heap-plan-propose``), authorized by an
administrator scope (``heap-plan-authorize``) and only then executed
(``heap-plan-execute``).  Execution re-derives the liveness proof against the
actual capture layout, protected handles and entry generations, so a plan
computed against an older layout is stale and frees nothing.
"""
import json

from .memorymap import capture_exists
from .store import one, rows

MAX_PLAN_ACTIONS = 8
MAX_PLAN_OBJECTS = 24

ADMIN_SCOPES = ('cache-directory', 'debug-session')

SCHEMA = '''
CREATE TABLE IF NOT EXISTS heap_catalogs (
    id TEXT PRIMARY KEY, capture TEXT, roots TEXT, handles TEXT, tables TEXT, objects TEXT
);
CREATE TABLE IF NOT EXISTS heap_handle_states (
    catalog TEXT, handle TEXT, status TEXT, at INTEGER,
    PRIMARY KEY (catalog, handle)
);
CREATE TABLE IF NOT EXISTS heap_root_protections (
    catalog TEXT, object TEXT, at INTEGER,
    PRIMARY KEY (catalog, object)
);
CREATE TABLE IF NOT EXISTS heap_entry_states (
    catalog TEXT, "table" TEXT, key TEXT, generation INTEGER, status TEXT, at INTEGER,
    PRIMARY KEY (catalog, "table", key)
);
CREATE TABLE IF NOT EXISTS heap_plans (
    id TEXT PRIMARY KEY, catalog TEXT, scope TEXT, actions TEXT, snapshot TEXT,
    projected_freed TEXT, projected_bytes INTEGER, status TEXT, admin TEXT, at INTEGER
);
CREATE TABLE IF NOT EXISTS heap_collections (
    plan TEXT, catalog TEXT, object TEXT, bytes INTEGER, generation INTEGER,
    PRIMARY KEY (plan, object)
);
'''


def valid_id(value):
    return isinstance(value, str) and bool(value)


def validate(catalog):
    if not isinstance(catalog, dict) or not set(catalog) <= {'roots', 'objects', 'handles', 'tables'}:
        return False
    if 'roots' not in catalog or 'objects' not in catalog:
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
    handles = catalog.get('handles', [])
    if not isinstance(handles, list):
        return False
    handle_ids = set()
    for handle in handles:
        if not isinstance(handle, dict) or set(handle) != {'id', 'target'}:
            return False
        if not valid_id(handle['id']) or handle['id'] in handle_ids or handle['target'] not in names:
            return False
        handle_ids.add(handle['id'])
    tables = catalog.get('tables', [])
    if not isinstance(tables, list):
        return False
    table_ids = set()
    for table in tables:
        if not isinstance(table, dict) or set(table) != {'id', 'entries'}:
            return False
        if not valid_id(table['id']) or table['id'] in table_ids or table['id'] not in names:
            return False
        table_ids.add(table['id'])
        entries = table['entries']
        if not isinstance(entries, list):
            return False
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {'key', 'value', 'generation'}:
                return False
            if entry['key'] not in names or entry['value'] not in names:
                return False
            if type(entry['generation']) is not int or entry['generation'] < 0:
                return False
    references = [*roots, *(ref for node in objects for ref in node['strong'])]
    return all(valid_id(ref) and ref in names for ref in references)


def load_catalog(db, event):
    if not capture_exists(db, event['capture']):
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
    db.execute('INSERT INTO heap_catalogs VALUES (?,?,?,?,?,?)',
               (event['id'], event['capture'], json.dumps(catalog['roots']),
                json.dumps(catalog.get('handles', [])), json.dumps(catalog.get('tables', [])),
                json.dumps(catalog['objects'])))
    for table in catalog.get('tables', []):
        for entry in table['entries']:
            db.execute('INSERT INTO heap_entry_states VALUES (?,?,?,?,?,?)',
                       (event['id'], table['id'], entry['key'], entry['generation'], 'live', event.get('at', 0)))
    return {'decision': 'heap_catalog_loaded', 'objects': len(catalog['objects'])}


def catalog_row(db, catalog_id):
    return one(db, 'SELECT * FROM heap_catalogs WHERE id=?', (catalog_id,))


def catalog_view(row):
    return _view(row)


def runtime_state(db, catalog_id):
    returned = {row['handle'] for row in rows(
        db, 'SELECT handle FROM heap_handle_states WHERE catalog=? AND status=?', (catalog_id, 'returned'))}
    protected = {row['object'] for row in rows(
        db, 'SELECT object FROM heap_root_protections WHERE catalog=?', (catalog_id,))}
    cleared = {(row['table'], row['key']) for row in rows(
        db, 'SELECT "table",key FROM heap_entry_states WHERE catalog=? AND status=?', (catalog_id, 'cleared'))}
    return returned, protected, cleared


def live_set(catalog, returned=(), protected=(), cleared=()):
    """Least fixed point of the real-root-seeded liveness rules.

    Conditional entries are rebuilt from the catalog topology minus runtime
    cleared entries.  Returns the live object id set.
    """
    objects = {node['id']: node for node in catalog['objects']}
    handle_targets = {handle['id']: handle['target'] for handle in catalog.get('handles', [])}
    returned, protected, cleared = set(returned), set(protected), set(cleared)
    tables = {}
    for table in catalog.get('tables', []):
        entries = {}
        for entry in table['entries']:
            if (table['id'], entry['key']) not in cleared:
                entries[entry['key']] = (entry['value'], entry['generation'])
        tables[table['id']] = entries
    live = set(catalog['roots']) | set(protected)
    for handle_id, target in handle_targets.items():
        if handle_id not in returned:
            live.add(target)
    live &= set(objects)
    while True:
        grown = set(live)
        for node_id in live:
            grown.update(objects[node_id]['strong'])
        for table_id, entries in tables.items():
            if table_id not in live:
                continue
            for key, (value, _generation) in entries.items():
                # The key must already have an independent liveness proof from
                # the real root set; being an entry key proves nothing.
                if key in live:
                    grown.add(value)
        if grown == live:
            return live
        live = grown


def snapshot_of(catalog, returned, protected, cleared):
    return json.dumps({
        'roots': list(catalog['roots']),
        'handles': [handle['id'] for handle in catalog.get('handles', [])],
        'returned': sorted(returned),
        'protected': sorted(protected),
        'cleared': sorted(table + '\x1f' + key for table, key in cleared),
    }, sort_keys=True)


def simulate_actions(catalog, actions):
    returned, protected, cleared = set(), set(), set()
    for action in actions:
        if action['action'] == 'return-handle':
            returned.add(action['handle'])
        elif action['action'] == 'clear-entry':
            cleared.add((action['table'], action['key']))
        else:
            return None
    return returned, protected, cleared


def _plan_simulation(db, catalog, actions):
    returned, _protected, cleared = simulate_actions(catalog, actions)
    handle_ids = {handle['id'] for handle in catalog.get('handles', [])}
    if not returned <= handle_ids:
        return None
    known_entries = {(table['id'], entry['key'])
                     for table in catalog.get('tables', []) for entry in table['entries']}
    if not cleared <= known_entries:
        return None
    baseline = live_set(catalog)
    after = live_set(catalog, returned=returned, cleared=cleared)
    freed = sorted(baseline - after)
    objects = {node['id']: node for node in catalog['objects']}
    return {
        'returned': returned,
        'cleared': cleared,
        'baseline': baseline,
        'after': after,
        'freed': freed,
        'freed_bytes': sum(objects[name]['bytes'] for name in freed),
    }


def propose_plan(db, event):
    if one(db, 'SELECT 1 FROM heap_plans WHERE id=?', (event['id'],)):
        return {'decision': 'heap_plan_exists'}
    row = catalog_row(db, event['catalog'])
    if not row or not capture_exists(db, row['capture']):
        return {'decision': 'heap_capture_missing'}
    catalog = _view(row)
    if event.get('scope') not in ADMIN_SCOPES:
        return {'decision': 'heap_plan_scope_invalid'}
    actions = event.get('actions', [])
    if not isinstance(actions, list) or not actions or len(actions) > MAX_PLAN_ACTIONS:
        return {'decision': 'heap_plan_limits_exceeded'}
    touched = set()
    for action in actions:
        if not isinstance(action, dict):
            return {'decision': 'heap_plan_invalid'}
        if action.get('action') == 'return-handle':
            if set(action) != {'action', 'handle'}:
                return {'decision': 'heap_plan_invalid'}
            touched.add(('handle', action['handle']))
        elif action.get('action') == 'clear-entry':
            if set(action) != {'action', 'table', 'key'}:
                return {'decision': 'heap_plan_invalid'}
            touched.add(('entry', action['table'], action['key']))
        else:
            return {'decision': 'heap_plan_invalid'}
    simulation = _plan_simulation(db, catalog, actions)
    if simulation is None:
        return {'decision': 'heap_plan_invalid'}
    if len(simulation['freed']) > MAX_PLAN_OBJECTS or len(touched) > MAX_PLAN_ACTIONS:
        return {'decision': 'heap_plan_limits_exceeded'}
    returned_now, protected_now, cleared_now = runtime_state(db, event['catalog'])
    snapshot = snapshot_of(catalog, returned_now | simulation['returned'],
                           protected_now, cleared_now | simulation['cleared'])
    db.execute('INSERT INTO heap_plans VALUES (?,?,?,?,?,?,?,?,?,?)',
               (event['id'], event['catalog'], event['scope'], json.dumps(actions), snapshot,
                json.dumps(simulation['freed']), simulation['freed_bytes'], 'proposed', None, event.get('at', 0)))
    return {'decision': 'heap_plan_proposed', 'plan': event['id'],
            'projected_freed': simulation['freed'], 'projected_bytes': simulation['freed_bytes']}


def _view(row):
    return {
        'roots': json.loads(row['roots']),
        'handles': json.loads(row['handles']),
        'tables': json.loads(row['tables']),
        'objects': json.loads(row['objects']),
    }


def authorize_plan(db, event):
    plan = one(db, 'SELECT * FROM heap_plans WHERE id=?', (event['plan'],))
    if not plan:
        return {'decision': 'heap_plan_missing'}
    if plan['status'] != 'proposed':
        return {'decision': 'heap_plan_not_open'}
    if event.get('scope') not in ADMIN_SCOPES or event['scope'] != plan['scope']:
        return {'decision': 'heap_plan_scope_denied'}
    db.execute('UPDATE heap_plans SET status=?,admin=?,at=? WHERE id=?',
               ('authorized', event['admin'], event.get('at', 0), plan['id']))
    return {'decision': 'heap_plan_authorized', 'plan': plan['id'], 'scope': plan['scope']}


def _entry_generations(catalog):
    return {(table['id'], entry['key']): entry['generation']
            for table in catalog.get('tables', []) for entry in table['entries']}


def execute_plan(db, event):
    from . import allocator
    plan = one(db, 'SELECT * FROM heap_plans WHERE id=?', (event['plan'],))
    if not plan:
        return {'decision': 'heap_plan_missing'}
    if plan['status'] == 'executed':
        return {'decision': 'heap_plan_already_executed', 'plan': plan['id']}
    if plan['status'] != 'authorized':
        return {'decision': 'heap_plan_not_authorized'}
    row = catalog_row(db, plan['catalog'])
    if not row or not capture_exists(db, row['capture']):
        return {'decision': 'heap_capture_missing'}
    catalog = _view(row)
    actions = json.loads(plan['actions'])
    returned_now, protected_now, cleared_now = runtime_state(db, plan['catalog'])
    generations = _entry_generations(catalog)
    for action in actions:
        if action['action'] == 'return-handle' and action['handle'] in returned_now:
            return {'decision': 'heap_plan_stale', 'reason': 'handle-already-returned'}
        if action['action'] == 'clear-entry':
            identity = (action['table'], action['key'])
            if identity in cleared_now:
                return {'decision': 'heap_plan_stale', 'reason': 'entry-already-cleared'}
            state = one(db, 'SELECT generation FROM heap_entry_states WHERE catalog=? AND "table"=? AND key=?',
                        (plan['catalog'], action['table'], action['key']))
            if not state or state['generation'] != generations.get(identity):
                return {'decision': 'heap_plan_stale', 'reason': 'entry-generation'}
    simulation = _plan_simulation(db, catalog, actions)
    expected_snapshot = snapshot_of(catalog, returned_now | simulation['returned'],
                                    protected_now, cleared_now | simulation['cleared'])
    if expected_snapshot != plan['snapshot']:
        return {'decision': 'heap_plan_stale', 'reason': 'layout-changed'}
    at = event.get('at', 0)
    for action in actions:
        if action['action'] == 'return-handle':
            db.execute('INSERT INTO heap_handle_states VALUES (?,?,?,?)',
                       (plan['catalog'], action['handle'], 'returned', at))
        else:
            db.execute('UPDATE heap_entry_states SET status=?,at=? WHERE catalog=? AND "table"=? AND key=?',
                       ('cleared', at, plan['catalog'], action['table'], action['key']))
    returned_after, protected_after, cleared_after = runtime_state(db, plan['catalog'])
    actual_live = live_set(catalog, returned=returned_after, protected=protected_after,
                           cleared=cleared_after)[0]
    objects = {node['id']: node for node in catalog['objects']}
    collected = sorted(simulation['baseline'] - actual_live)
    freed_bytes = sum(objects[name]['bytes'] for name in collected)
    # A plan whose recheck no longer frees its projected bytes is stale: the
    # allocator must never consume an expired reservation.
    if collected != json.loads(plan['projected_freed']) or freed_bytes != plan['projected_bytes']:
        return {'decision': 'heap_plan_stale', 'reason': 'liveness-changed'}
    for name in collected:
        db.execute('INSERT INTO heap_collections VALUES (?,?,?,?)',
                   (plan['id'], plan['catalog'], name, objects[name]['bytes'],
                    row_capture_generation(db, row['capture'])))
    allocator.release(db, plan['id'], plan['catalog'], collected,
                      [objects[name]['bytes'] for name in collected])
    db.execute('UPDATE heap_plans SET status=?,at=? WHERE id=?', ('executed', at, plan['id']))
    return {'decision': 'heap_plan_executed', 'plan': plan['id'], 'scope': plan['scope'],
            'live': sorted(actual_live),
            'live_bytes': sum(objects[name]['bytes'] for name in actual_live),
            'collected': collected, 'freed_bytes': freed_bytes}


def row_capture_generation(db, capture_id):
    capture = one(db, 'SELECT generation FROM memory_captures WHERE id=?', (capture_id,))
    return capture['generation'] if capture else 0


def protect_root(db, event):
    row = catalog_row(db, event['catalog'])
    if not row or not capture_exists(db, row['capture']):
        return {'decision': 'heap_capture_missing'}
    catalog = _view(row)
    names = {node['id'] for node in catalog['objects']}
    if event['object'] not in names:
        return {'decision': 'heap_object_unknown'}
    if one(db, 'SELECT 1 FROM heap_collections WHERE catalog=? AND object=?',
           (event['catalog'], event['object'])):
        return {'decision': 'heap_object_collected'}
    if one(db, 'SELECT 1 FROM heap_root_protections WHERE catalog=? AND object=?',
           (event['catalog'], event['object'])):
        return {'decision': 'heap_protection_exists'}
    db.execute('INSERT INTO heap_root_protections VALUES (?,?,?)',
               (event['catalog'], event['object'], event.get('at', 0)))
    return {'decision': 'heap_root_protected', 'object': event['object']}


def census(db, event):
    row = catalog_row(db, event['catalog'])
    if not row:
        return {'decision': 'heap_catalog_missing'}
    catalog = _view(row)
    returned, protected, cleared = runtime_state(db, event['catalog'])
    live = live_set(catalog, returned=returned, protected=protected, cleared=cleared)
    objects = {node['id']: node for node in catalog['objects']}
    unreachable = objects.keys() - live
    return {'decision': 'heap_counted', 'live': sorted(live), 'unreachable': sorted(unreachable),
            'live_bytes': sum(objects[name]['bytes'] for name in live),
            'unreachable_bytes': sum(objects[name]['bytes'] for name in unreachable)}


def handle_read(db, event):
    row = catalog_row(db, event['catalog'])
    if not row:
        return {'decision': 'heap_catalog_missing'}
    catalog = _view(row)
    handle = next((entry for entry in catalog.get('handles', []) if entry['id'] == event['handle']), None)
    if not handle:
        return {'decision': 'heap_handle_unknown'}
    if one(db, 'SELECT 1 FROM heap_handle_states WHERE catalog=? AND handle=? AND status=?',
           (event['catalog'], event['handle'], 'returned')):
        return {'decision': 'heap_handle_returned', 'handle': event['handle']}
    if one(db, 'SELECT 1 FROM heap_collections WHERE catalog=? AND object=?',
           (event['catalog'], handle['target'])):
        return {'decision': 'heap_object_collected', 'object': handle['target']}
    return {'decision': 'heap_handle_read', 'handle': event['handle'], 'object': handle['target']}


def entry_read(db, event):
    row = catalog_row(db, event['catalog'])
    if not row:
        return {'decision': 'heap_catalog_missing'}
    catalog = _view(row)
    entry = None
    for table in catalog.get('tables', []):
        if table['id'] != event['table']:
            continue
        entry = next((candidate for candidate in table['entries'] if candidate['key'] == event['key']), None)
    if entry is None:
        return {'decision': 'heap_entry_unknown'}
    state = one(db, 'SELECT status,generation FROM heap_entry_states WHERE catalog=? AND "table"=? AND key=?',
                (event['catalog'], event['table'], event['key']))
    if not state or state['generation'] != entry['generation']:
        return {'decision': 'heap_entry_generation'}
    if state['status'] == 'cleared':
        return {'decision': 'heap_entry_cleared', 'table': event['table'], 'key': event['key']}
    if one(db, 'SELECT 1 FROM heap_collections WHERE catalog=? AND object=?',
           (event['catalog'], entry['value'])):
        return {'decision': 'heap_object_collected', 'object': entry['value']}
    return {'decision': 'heap_entry_read', 'table': event['table'], 'key': event['key'],
            'value': entry['value'], 'generation': entry['generation']}


def table_read(db, event):
    row = catalog_row(db, event['catalog'])
    if not row:
        return {'decision': 'heap_catalog_missing'}
    catalog = _view(row)
    table = next((candidate for candidate in catalog.get('tables', []) if candidate['id'] == event['table']), None)
    if not table:
        return {'decision': 'heap_table_unknown'}
    entries = []
    for entry in table['entries']:
        state = one(db, 'SELECT status FROM heap_entry_states WHERE catalog=? AND "table"=? AND key=?',
                    (event['catalog'], event['table'], entry['key']))
        entries.append({'key': entry['key'], 'value': entry['value'], 'generation': entry['generation'],
                        'status': state['status'] if state else 'unknown'})
    # The table configuration and identity stay readable even when every
    # conditional entry's value has been collected.
    return {'decision': 'heap_table_read', 'table': event['table'], 'entries': entries}


def handle(db, event):
    operation = event['op']
    if operation == 'catalog-load':
        return load_catalog(db, event)
    if operation == 'heap-census':
        return census(db, event)
    if operation == 'heap-plan-propose':
        return propose_plan(db, event)
    if operation == 'heap-plan-authorize':
        return authorize_plan(db, event)
    if operation == 'heap-plan-execute':
        return execute_plan(db, event)
    if operation == 'heap-root-protect':
        return protect_root(db, event)
    if operation == 'heap-handle-read':
        return handle_read(db, event)
    if operation == 'heap-entry-read':
        return entry_read(db, event)
    if operation == 'heap-table-read':
        return table_read(db, event)
    return {'decision': 'unknown_operation'}
