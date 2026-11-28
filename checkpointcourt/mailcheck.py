"""Materialize the pending inbox for one explicitly chosen pair of cuts."""
import json
from hashlib import sha256

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
CREATE TABLE IF NOT EXISTS masked_packages (
    id TEXT PRIMARY KEY, contract TEXT NOT NULL, generation TEXT NOT NULL,
    sender_cut INTEGER NOT NULL, arrivals INTEGER NOT NULL,
    slip_digest TEXT NOT NULL, kept INTEGER NOT NULL, planned INTEGER NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS masked_pending (
    package TEXT NOT NULL, position INTEGER NOT NULL,
    kind TEXT NOT NULL, ref TEXT NOT NULL, instruction TEXT, body TEXT,
    consumed INTEGER NOT NULL,
    PRIMARY KEY(package, position)
);
CREATE TABLE IF NOT EXISTS masked_receipts (
    package TEXT NOT NULL, position INTEGER NOT NULL,
    kind TEXT NOT NULL, ref TEXT NOT NULL, instruction TEXT, seqno INTEGER NOT NULL,
    PRIMARY KEY(package, position)
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


def _augment(cands, pair_sj, pair_js, start):
    """One alternating-path search from a free job vertex; flips edges on success."""
    seen = set()
    came_from = {}
    stack = [(start, iter(cands[start]))]
    while stack:
        job, edges = stack[-1]
        advanced = False
        for slip in edges:
            if slip in seen:
                continue
            seen.add(slip)
            came_from[slip] = job
            if slip not in pair_sj:
                s, j = slip, job
                while True:
                    prev = pair_js.get(j)
                    pair_sj[s] = j
                    pair_js[j] = s
                    if prev is None:
                        break
                    j = came_from[prev]
                    s = prev
                return True
            stack.append((pair_sj[slip], iter(cands[pair_sj[slip]])))
            advanced = True
            break
        if not advanced:
            stack.pop()
    return False


def _kept_intervals(count, arrivals, cands):
    """Analyze one slip group against the ordered send manifest.

    cands[pos] lists the slips accepting the job at manifest position pos.
    Returns (feasible, intervals): feasible is True when every slip can be
    matched to a distinct job (a legal history exists); intervals maps each
    job position that may stay undelivered to the inclusive range of kept
    queue positions it can occupy over all legal histories.  Jobs absent
    from intervals are delivered in every legal history.
    """
    pre = [0] * (count + 1)
    pair_sj, pair_js = {}, {}
    for pos in range(count):
        _augment(cands, pair_sj, pair_js, pos)
        pre[pos + 1] = len(pair_sj)
    if pre[count] != arrivals:
        return False, {}
    suf = [0] * (count + 1)
    back_sj, back_js = {}, {}
    for pos in range(count - 1, -1, -1):
        _augment(cands, back_sj, back_js, pos)
        suf[pos] = len(back_sj)
    # Jobs reachable from unmatched jobs via alternating paths can leave the
    # delivered set; matched jobs outside that region are always delivered.
    reached = set()
    queue = [pos for pos in range(count) if pos not in pair_js and cands[pos]]
    seen_slips = set()
    while queue:
        job = queue.pop()
        if job in reached:
            continue
        reached.add(job)
        for slip in cands[job]:
            if pair_sj.get(slip) == job or slip in seen_slips:
                continue
            seen_slips.add(slip)
            if slip in pair_sj:
                queue.append(pair_sj[slip])
    intervals = {}
    for pos in range(count):
        if pos in pair_js and pos not in reached:
            continue
        first = pos - pre[pos]
        last = pos - arrivals + suf[pos + 1]
        intervals[pos] = (first, last)
    return True, intervals


def _common_prefix(kept, intervals, tokens):
    """Longest prefix of the kept queue shared by every legal history.

    tokens[pos] projects a job into the contract's view; the prefix is the
    longest run of kept positions whose possible jobs all share one token.
    """
    opens = {}
    closes = {}
    for pos, (first, last) in intervals.items():
        opens.setdefault(first, []).append(pos)
        closes.setdefault(last, []).append(pos)
    active = {}
    plan = []
    for slot in range(kept):
        for pos in opens.get(slot, ()):
            token = tokens[pos]
            active[token] = active.get(token, 0) + 1
        if len(active) != 1:
            break
        plan.append(next(iter(active)))
        for pos in closes.get(slot, ()):
            token = tokens[pos]
            active[token] -= 1
            if not active[token]:
                del active[token]
    return plan


def _manifest_scope(manifest):
    """Validate the offline send manifest and return its in-scope items."""
    if not isinstance(manifest, dict):
        return None
    sender = manifest.get('sender')
    receiver = manifest.get('receiver')
    generation = manifest.get('generation')
    sender_cut = manifest.get('sender_cut')
    arrivals = manifest.get('receiver_arrivals')
    items = manifest.get('items')
    if (not isinstance(sender, str) or not sender
            or not isinstance(receiver, str) or not receiver or sender == receiver
            or not isinstance(generation, str) or not generation
            or type(sender_cut) is not int or sender_cut < 0
            or type(arrivals) is not int or arrivals < 0
            or not isinstance(items, list)):
        return None
    by_id = {}
    for item in items:
        if not isinstance(item, dict):
            return None
        ident = item.get('id')
        sent = item.get('sent')
        batch = item.get('batch')
        interchangeable = item.get('interchangeable', False)
        if (not isinstance(ident, str) or not ident or ident in by_id
                or type(sent) is not int or sent < 1
                or not isinstance(item.get('receiver'), str)
                or not isinstance(item.get('generation'), str)
                or not isinstance(item.get('body'), str)
                or not isinstance(item.get('instruction'), str)
                or (batch is not None and not isinstance(batch, str))
                or not isinstance(interchangeable, bool)):
            return None
        by_id[ident] = item
    scoped = [item for item in items
              if item['receiver'] == receiver and item['generation'] == generation
              and item['sent'] <= sender_cut]
    scoped.sort(key=lambda item: item['sent'])
    if len({item['sent'] for item in scoped}) != len(scoped):
        return None
    return {'sender': sender, 'receiver': receiver, 'generation': generation,
            'sender_cut': sender_cut, 'arrivals': arrivals,
            'by_id': by_id, 'scoped': scoped}


def _read_slips(slips):
    if not isinstance(slips, list):
        return None
    parsed = []
    seen = set()
    for slip in slips:
        if not isinstance(slip, dict):
            return None
        ident = slip.get('id')
        candidates = slip.get('candidates')
        if (not isinstance(ident, str) or not ident or ident in seen
                or not isinstance(candidates, list) or not candidates
                or any(not isinstance(candidate, str) for candidate in candidates)
                or len(set(candidates)) != len(candidates)):
            return None
        seen.add(ident)
        parsed.append({'id': ident, 'candidates': list(candidates)})
    return parsed


def _token_view(item, authorized):
    if item.get('interchangeable', False) and item.get('batch') in authorized:
        return ('batch', item['batch'], item['instruction'])
    return ('job', item['id'])


def recover_masked(db, event):
    """Recover a pending queue from offline masked-slip evidence only.

    The evidence is self-contained: an ordered send manifest, the receiver
    cut position as an arrival count, and the fully collected masked slips
    with their candidate job ids.  No live mail_items, arrival rows or event
    log are consulted.  Only the longest queue prefix shared by every legal
    slip assignment is persisted as deliverable.
    """
    package = event.get('id')
    contract = event.get('contract')
    if not isinstance(package, str) or not package:
        return {'decision': 'manifest_invalid'}
    if contract not in ('named', 'process'):
        return {'decision': 'contract_invalid'}
    authorized = event.get('authorized_batches', [])
    if contract == 'process' and (not isinstance(authorized, list)
                                  or any(not isinstance(batch, str) for batch in authorized)):
        return {'decision': 'contract_invalid'}
    scope = _manifest_scope(event.get('manifest'))
    if scope is None:
        return {'decision': 'manifest_invalid'}
    slips = _read_slips(event.get('slips'))
    if slips is None:
        return {'decision': 'slip_invalid'}
    if len(slips) != scope['arrivals']:
        return {'decision': 'evidence_incomplete'}
    for slip in slips:
        for candidate in slip['candidates']:
            item = scope['by_id'].get(candidate)
            if item is None:
                return {'decision': 'candidate_unknown', 'candidate': candidate}
            if item['receiver'] != scope['receiver']:
                return {'decision': 'candidate_wrong_port', 'candidate': candidate}
            if item['generation'] != scope['generation']:
                return {'decision': 'candidate_wrong_generation', 'candidate': candidate}
            if item['sent'] > scope['sender_cut']:
                return {'decision': 'candidate_beyond_cut', 'candidate': candidate}
    if one(db, 'SELECT 1 FROM masked_packages WHERE id=?', (package,)):
        return {'decision': 'package_exists'}
    ordered = scope['scoped']
    positions = {item['id']: pos for pos, item in enumerate(ordered)}
    cands = [[] for _ in ordered]
    for index, slip in enumerate(slips):
        for candidate in slip['candidates']:
            cands[positions[candidate]].append(index)
    feasible, intervals = _kept_intervals(len(ordered), len(slips), cands)
    if not feasible:
        return {'decision': 'no_legal_history'}
    authorized_set = set(authorized) if contract == 'process' else set()
    tokens = [_token_view(item, authorized_set) for item in ordered]
    kept = len(ordered) - len(slips)
    plan = _common_prefix(kept, intervals, tokens)
    digest = sha256(json.dumps({'sender': scope['sender'], 'receiver': scope['receiver'],
                                'generation': scope['generation'],
                                'sender_cut': scope['sender_cut'], 'slips': slips},
                               sort_keys=True).encode()).hexdigest()
    status = 'held' if not plan and kept else 'ready'
    db.execute('INSERT INTO masked_packages VALUES (?,?,?,?,?,?,?,?,?)',
               (package, contract, scope['generation'], scope['sender_cut'],
                scope['arrivals'], digest, kept, len(plan), status))
    by_id = {item['id']: item for item in ordered}
    pending = []
    for position, token in enumerate(plan):
        if token[0] == 'batch':
            row = (package, position, 'batch', token[1], token[2], token[2], 0)
            pending.append({'kind': 'batch', 'batch': token[1], 'instruction': token[2]})
        else:
            item = by_id[token[1]]
            row = (package, position, 'job', item['id'], item['instruction'], item['body'], 0)
            pending.append({'kind': 'job', 'job': item['id']})
        db.execute('INSERT INTO masked_pending VALUES (?,?,?,?,?,?,?)', row)
    if status == 'held':
        return {'decision': 'inbox_held', 'pending': []}
    return {'decision': 'inbox_ready', 'pending': pending}


def deliver_masked(db, event):
    """Consume one proven-deliverable position in a single durable commit."""
    package = one(db, 'SELECT * FROM masked_packages WHERE id=?', (event.get('id'),))
    if not package:
        return {'decision': 'inbox_missing'}
    item = one(db, '''SELECT * FROM masked_pending WHERE package=? AND consumed=0
                     ORDER BY position LIMIT 1''', (package['id'],))
    if not item:
        if package['kept'] > package['planned']:
            return {'decision': 'inbox_held'}
        return {'decision': 'inbox_empty'}
    db.execute('UPDATE masked_pending SET consumed=1 WHERE package=? AND position=? AND consumed=0',
               (package['id'], item['position']))
    seqno = item['position'] + 1
    db.execute('INSERT INTO masked_receipts VALUES (?,?,?,?,?,?)',
               (package['id'], item['position'], item['kind'], item['ref'],
                item['instruction'], seqno))
    if item['kind'] == 'batch':
        return {'decision': 'work_delivered', 'batch': item['ref'],
                'instruction': item['instruction'],
                'receipt': {'kind': 'batch', 'batch': item['ref'],
                            'instruction': item['instruction'], 'order': seqno}}
    return {'decision': 'mail_delivered', 'delivery': item['ref'], 'body': item['body'],
            'receipt': {'kind': 'job', 'job': item['ref'], 'order': seqno}}


def handle(db, event):
    operation = event['op']
    if operation == 'inbox-recover':
        return recover(db, event)
    if operation == 'inbox-deliver':
        return deliver(db, event)
    if operation == 'inbox-recover-masked':
        return recover_masked(db, event)
    if operation == 'inbox-deliver-masked':
        return deliver_masked(db, event)
    return {'decision': 'unknown_operation'}
