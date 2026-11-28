"""Synthetic local allocator that consumes reclaimed heap intervals.

Reclaimed bytes are published by ``heap-plan-execute`` through :func:`release`.
The allocator only knows intervals backed by an executed, non-void cleanup plan
whose capture is still on record.  Every allocation bumps a monotonic cursor so
the same pending interval can never be handed out twice, and a claim against an
expired (never executed) plan is rejected.  Reopening the database re-verifies
every honored claim from the event ledger.
"""
from .memorymap import capture_exists
from .store import one, rows

SCHEMA = '''
CREATE TABLE IF NOT EXISTS alloc_arenas (
    catalog TEXT PRIMARY KEY, capacity INTEGER, used INTEGER
);
CREATE TABLE IF NOT EXISTS alloc_claims (
    id TEXT PRIMARY KEY, catalog TEXT, plan TEXT, size INTEGER,
    "offset" INTEGER, at INTEGER, status TEXT
);
'''


def release(db, plan_id, catalog_id, objects, sizes):
    """Publish the interval freed by one executed plan.

    Objects that were already unreachable before the plan never appear here:
    the heap module computes the freed set from the live-set delta, so only
    newly collected bytes grow the arena.
    """
    arena = one(db, 'SELECT * FROM alloc_arenas WHERE catalog=?', (catalog_id,))
    total = sum(sizes)
    if arena:
        db.execute('UPDATE alloc_arenas SET capacity=capacity+? WHERE catalog=?', (total, catalog_id))
    else:
        db.execute('INSERT INTO alloc_arenas VALUES (?,?,?)', (catalog_id, total, 0))
    return total


def _backing_plan(db, catalog_id, plan_id):
    plan = one(db, 'SELECT * FROM heap_plans WHERE id=? AND catalog=?', (plan_id, catalog_id))
    if not plan or plan['status'] != 'executed':
        return None
    catalog = one(db, 'SELECT capture FROM heap_catalogs WHERE id=?', (catalog_id,))
    if not catalog or not capture_exists(db, catalog['capture']):
        return None
    return plan


def handle(db, event):
    op = event['op']
    if op != 'alloc-claim':
        return {'decision': 'unknown_operation'}
    if one(db, 'SELECT 1 FROM alloc_claims WHERE id=?', (event['id'],)):
        claim = one(db, 'SELECT * FROM alloc_claims WHERE id=?', (event['id'],))
        return {'decision': 'alloc_claim_exists', 'catalog': claim['catalog'], 'plan': claim['plan'],
                'size': claim['size'], 'offset': claim['offset'], 'remaining':
                one(db, 'SELECT capacity-used AS remaining FROM alloc_arenas WHERE catalog=?',
                    (claim['catalog'],))['remaining']}
    size = event.get('size')
    if type(size) is not int or size <= 0:
        return {'decision': 'alloc_request_invalid'}
    catalog_id, plan_id = event['catalog'], event['plan']
    arena = one(db, 'SELECT * FROM alloc_arenas WHERE catalog=?', (catalog_id,))
    if not arena:
        return {'decision': 'alloc_no_space', 'remaining': 0}
    if not _backing_plan(db, catalog_id, plan_id):
        # Expired, superseded or never-executed plans cannot back a claim.
        return {'decision': 'alloc_plan_expired', 'remaining': arena['capacity'] - arena['used']}
    remaining = arena['capacity'] - arena['used']
    if size > remaining:
        return {'decision': 'alloc_capacity_exceeded', 'remaining': remaining}
    offset = arena['used']
    db.execute('UPDATE alloc_arenas SET used=used+? WHERE catalog=?', (size, catalog_id))
    db.execute('INSERT INTO alloc_claims VALUES (?,?,?,?,?,?,?)',
               (event['id'], catalog_id, plan_id, size, offset, event.get('at', 0), 'granted'))
    return {'decision': 'alloc_granted', 'catalog': catalog_id, 'plan': plan_id,
            'size': size, 'offset': offset, 'remaining': remaining - size}


def status(db, event):
    if event['op'] != 'alloc-status':
        return {'decision': 'unknown_operation'}
    arena = one(db, 'SELECT * FROM alloc_arenas WHERE catalog=?', (event['catalog'],))
    if not arena:
        return {'decision': 'alloc_arena_missing'}
    claims = rows(db, 'SELECT id,plan,size,"offset",status FROM alloc_claims WHERE catalog=? ORDER BY "offset"',
                  (event['catalog'],))
    return {'decision': 'alloc_status', 'catalog': event['catalog'], 'capacity': arena['capacity'],
            'used': arena['used'], 'remaining': arena['capacity'] - arena['used'], 'claims': claims}
