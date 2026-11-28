import json
import random
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from checkpointcourt.mailcheck import _common_prefix, _kept_intervals
from checkpointcourt.store import connect, replay, rows, run

ROOT = Path(__file__).resolve().parents[1]


def item(ident, sent, body, batch=None, interchangeable=False,
         receiver='receiver', generation='wo-7'):
    return {'id': ident, 'sent': sent, 'receiver': receiver, 'generation': generation,
            'body': body, 'instruction': body, 'batch': batch,
            'interchangeable': interchangeable}


def shop_items(order):
    meta = {'A': ('burn-in', None, False), 'B': ('dry-run', None, False),
            'C': ('flush', 'fl-1', True), 'D': ('flush', 'fl-1', True),
            'E': ('rinse', None, False)}
    return [item(ident, n + 1, *meta[ident]) for n, ident in enumerate(order)]


MAIN_ITEMS = shop_items('ABCDE') + [
    item('F', 6, 'polish'),
    item('G', 7, 'audit', receiver='other'),
    item('H', 8, 'wax', generation='wo-8'),
]


def manifest(items, sender_cut=5, arrivals=3):
    return {'sender': 'sender', 'receiver': 'receiver', 'generation': 'wo-7',
            'sender_cut': sender_cut, 'receiver_arrivals': arrivals, 'items': items}


def slips(*groups):
    return [{'id': f'slip-{n}', 'candidates': list(group)} for n, group in enumerate(groups)]


MAIN_SLIPS = slips(('A', 'B'), ('A', 'B'), ('C', 'D'))


def recover(db, event_id, box, contract, mfst, slip_list, batches=None):
    event = {'op': 'inbox-recover-masked', 'event_id': event_id, 'id': box,
             'contract': contract, 'manifest': mfst, 'slips': slip_list}
    if batches is not None:
        event['authorized_batches'] = batches
    return run(db, 'mailcheck', event)


def deliver(db, event_id, box):
    return run(db, 'mailcheck',
               {'op': 'inbox-deliver-masked', 'event_id': event_id, 'id': box})


def brute_assignments(cands, count):
    out = []
    used = [False] * count
    current = []

    def walk(k):
        if k == len(cands):
            out.append(tuple(current))
            return
        for pos in cands[k]:
            if not used[pos]:
                used[pos] = True
                current.append(pos)
                walk(k + 1)
                current.pop()
                used[pos] = False

    walk(0)
    return out


def brute_slots(count, cands):
    images = {frozenset(assignment) for assignment in brute_assignments(cands, count)}
    if not images:
        return None
    slots = [set() for _ in range(count - len(cands))]
    for image in images:
        rest = [pos for pos in range(count) if pos not in image]
        for slot, pos in enumerate(rest):
            slots[slot].add(pos)
    return slots


def brute_plan(count, cands, tokens):
    images = {frozenset(assignment) for assignment in brute_assignments(cands, count)}
    if not images:
        return None
    sequences = [[tokens[pos] for pos in range(count) if pos not in image]
                 for image in images]
    plan = []
    for column in zip(*sequences):
        if len(set(column)) != 1:
            break
        plan.append(column[0])
    return plan


def impl_analysis(count, cands):
    by_job = [[] for _ in range(count)]
    for index, group in enumerate(cands):
        for pos in group:
            by_job[pos].append(index)
    feasible, intervals = _kept_intervals(count, len(cands), by_job)
    if not feasible:
        return None
    return intervals


def impl_slots(count, cands):
    intervals = impl_analysis(count, cands)
    if intervals is None:
        return None
    slots = [set() for _ in range(count - len(cands))]
    for pos, (first, last) in intervals.items():
        for slot in range(first, last + 1):
            slots[slot].add(pos)
    return slots


def impl_plan(count, cands, tokens):
    intervals = impl_analysis(count, cands)
    if intervals is None:
        return None
    return _common_prefix(count - len(cands), intervals, tokens)


class MaskedContractTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(':memory:')

    def tearDown(self):
        self.db.close()

    def test_named_contract_holds_behind_ambiguous_head(self):
        result = recover(self.db, 'e1', 'named', 'named', manifest(MAIN_ITEMS), MAIN_SLIPS)
        self.assertEqual(result, {'decision': 'inbox_held', 'pending': []})
        # A and B provably arrived and rinse is provably undelivered, yet the
        # undetermined C/D head blocks the whole named queue.
        self.assertEqual(deliver(self.db, 'e2', 'named'), {'decision': 'inbox_held'})
        self.assertEqual(rows(self.db, 'SELECT * FROM masked_pending WHERE package="named"'), [])

    def test_process_contract_delivers_common_prefix_once_each(self):
        result = recover(self.db, 'e1', 'proc', 'process', manifest(MAIN_ITEMS),
                         MAIN_SLIPS, batches=['fl-1'])
        self.assertEqual(result, {'decision': 'inbox_ready', 'pending': [
            {'kind': 'batch', 'batch': 'fl-1', 'instruction': 'flush'},
            {'kind': 'job', 'job': 'E'}]})
        first = deliver(self.db, 'e2', 'proc')
        self.assertEqual(first['decision'], 'work_delivered')
        self.assertEqual(first['instruction'], 'flush')
        self.assertNotIn('delivery', first)  # no original job id is impersonated
        self.assertEqual(first['receipt'],
                         {'kind': 'batch', 'batch': 'fl-1', 'instruction': 'flush', 'order': 1})
        second = deliver(self.db, 'e3', 'proc')
        self.assertEqual(second['decision'], 'mail_delivered')
        self.assertEqual(second['delivery'], 'E')
        self.assertEqual(second['body'], 'rinse')
        self.assertEqual(second['receipt'], {'kind': 'job', 'job': 'E', 'order': 2})
        self.assertEqual(deliver(self.db, 'e4', 'proc'), {'decision': 'inbox_empty'})
        receipts = rows(self.db, 'SELECT * FROM masked_receipts WHERE package="proc" ORDER BY position')
        self.assertEqual([(r['kind'], r['ref'], r['seqno']) for r in receipts],
                         [('batch', 'fl-1', 1), ('job', 'E', 2)])

    def test_same_counts_different_order_must_hold(self):
        swapped = shop_items('ABCED')  # second input: only D and E swap places
        result = recover(self.db, 'e1', 'proc', 'process', manifest(swapped),
                         MAIN_SLIPS, batches=['fl-1'])
        # Both legal histories leave one flush and one rinse, but in opposite
        # order, so a count-based check would wrongly release the head.
        self.assertEqual(result, {'decision': 'inbox_held', 'pending': []})
        self.assertEqual(deliver(self.db, 'e2', 'proc'), {'decision': 'inbox_held'})

    def test_underlying_named_ledgers_would_deliver_in_opposite_orders(self):
        def ledger(arrivals):
            db = connect(':memory:')
            run(db, 'mailroom', {'event_id': 'o1', 'op': 'port-open', 'port': 'sender'})
            run(db, 'mailroom', {'event_id': 'o2', 'op': 'port-open', 'port': 'receiver'})
            for ident, body in [('A', 'burn-in'), ('B', 'dry-run'), ('C', 'flush'),
                                ('E', 'rinse'), ('D', 'flush')]:
                run(db, 'mailroom', {'event_id': f'send-{ident}', 'op': 'send', 'id': ident,
                                     'port': 'sender', 'receiver': 'receiver', 'body': body,
                                     'instruction': body,
                                     'batch': 'fl-1' if body == 'flush' else None,
                                     'generation': 'wo-7'})
            run(db, 'mailroom', {'event_id': 'cut-s', 'op': 'cut', 'id': 's1', 'port': 'sender'})
            for ident in arrivals:
                run(db, 'mailroom', {'event_id': f'arr-{ident}', 'op': 'arrive',
                                     'id': ident, 'port': 'receiver'})
            run(db, 'mailroom', {'event_id': 'cut-r', 'op': 'cut', 'id': 'r1', 'port': 'receiver'})
            run(db, 'mailcheck', {'event_id': 'rec', 'op': 'inbox-recover', 'id': 'box',
                                  'sender_cut': 's1', 'receiver_cut': 'r1'})
            bodies = []
            while True:
                result = run(db, 'mailcheck',
                             {'event_id': f'dlv-{len(bodies)}', 'op': 'inbox-deliver', 'id': 'box'})
                if result['decision'] != 'mail_delivered':
                    break
                bodies.append(result['body'])
            db.close()
            return bodies

        # The two legal ledgers ABCED/ABC and ABCED/BAD both remain with one
        # flush and one rinse, yet the existing named entry would release
        # them in opposite orders; picking either matching is wrong somewhere.
        self.assertEqual(ledger('ABC'), ['rinse', 'flush'])
        self.assertEqual(ledger('BAD'), ['flush', 'rinse'])

    def test_common_prefix_delivers_then_hold_without_spreading(self):
        mfst = manifest(MAIN_ITEMS, arrivals=2)
        result = recover(self.db, 'e1', 'box', 'named', mfst, slips(('A',), ('C', 'D')))
        self.assertEqual(result, {'decision': 'inbox_ready', 'pending': [{'kind': 'job', 'job': 'B'}]})
        first = deliver(self.db, 'e2', 'box')
        self.assertEqual(first['decision'], 'mail_delivered')
        self.assertEqual(first['delivery'], 'B')
        # The ambiguity stays at the C/D head; it must not spread to the
        # already decided prefix, and rinse stays blocked behind it.
        self.assertEqual(deliver(self.db, 'e3', 'box'), {'decision': 'inbox_held'})
        self.assertEqual(deliver(self.db, 'e4', 'box'), {'decision': 'inbox_held'})

    def test_evidence_incomplete_without_full_slip_set(self):
        result = recover(self.db, 'e1', 'short', 'named', manifest(MAIN_ITEMS),
                         slips(('A',), ('B',)))
        self.assertEqual(result, {'decision': 'evidence_incomplete'})
        self.assertEqual(deliver(self.db, 'e2', 'short'), {'decision': 'inbox_missing'})

    def test_no_legal_history_is_not_an_empty_box(self):
        mfst = manifest(MAIN_ITEMS, arrivals=2)
        result = recover(self.db, 'e1', 'clash', 'named', mfst, slips(('A',), ('A',)))
        self.assertEqual(result, {'decision': 'no_legal_history'})
        # No package may be persisted, so nothing can be delivered or held.
        self.assertEqual(deliver(self.db, 'e2', 'clash'), {'decision': 'inbox_missing'})

    def test_candidates_outside_the_sealed_scope_are_rejected(self):
        cases = [('F', 'candidate_beyond_cut'), ('G', 'candidate_wrong_port'),
                 ('H', 'candidate_wrong_generation'), ('Z', 'candidate_unknown')]
        for n, (candidate, decision) in enumerate(cases):
            with self.subTest(candidate=candidate):
                mfst = manifest(MAIN_ITEMS, arrivals=1)
                result = recover(self.db, f'e{n}', f'box-{candidate}', 'named', mfst,
                                 slips((candidate,)))
                self.assertEqual(result, {'decision': decision, 'candidate': candidate})

    def test_invalid_evidence_shapes_are_rejected(self):
        mfst = manifest(MAIN_ITEMS)
        self.assertEqual(recover(self.db, 'e1', 'b1', 'wildcard', mfst, MAIN_SLIPS),
                         {'decision': 'contract_invalid'})
        self.assertEqual(recover(self.db, 'e2', 'b2', 'named', {'sender': 'x'}, MAIN_SLIPS),
                         {'decision': 'manifest_invalid'})
        self.assertEqual(recover(self.db, 'e3', 'b3', 'named', mfst,
                                 [{'id': 's1', 'candidates': []}]),
                         {'decision': 'slip_invalid'})
        self.assertEqual(recover(self.db, 'e4', 'b4', 'named', mfst, MAIN_SLIPS),
                         {'decision': 'inbox_held', 'pending': []})
        self.assertEqual(recover(self.db, 'e5', 'b4', 'named', mfst, MAIN_SLIPS),
                         {'decision': 'package_exists'})


class MaskedDurabilityTests(unittest.TestCase):
    def test_restart_and_lost_answer_retry_never_double_deliver(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'masked.sqlite'
            db = connect(path)
            recover(db, 'e1', 'proc', 'process', manifest(MAIN_ITEMS), MAIN_SLIPS,
                    batches=['fl-1'])
            first = deliver(db, 'd1', 'proc')
            self.assertEqual(first['decision'], 'work_delivered')
            db.close()
            # A new process sees the durable commit: the same delivery event
            # returns the recorded answer instead of delivering again.
            db = connect(path)
            self.assertEqual(deliver(db, 'd1', 'proc'), first)
            second = deliver(db, 'd2', 'proc')
            self.assertEqual(second['decision'], 'mail_delivered')
            self.assertEqual(second['delivery'], 'E')
            self.assertEqual(deliver(db, 'd3', 'proc'), {'decision': 'inbox_empty'})
            pending = rows(db, 'SELECT * FROM masked_pending WHERE package="proc" ORDER BY position')
            self.assertEqual([row['consumed'] for row in pending], [1, 1])
            db.close()
            self.assertEqual(replay(path),
                             {'decisions': 4, 'verified': True, 'divergences': []})

    def test_held_package_survives_restart_without_delivering(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'held.sqlite'
            db = connect(path)
            recover(db, 'e1', 'named', 'named', manifest(MAIN_ITEMS), MAIN_SLIPS)
            db.close()
            db = connect(path)
            self.assertEqual(deliver(db, 'd1', 'named'), {'decision': 'inbox_held'})
            self.assertEqual(rows(db, 'SELECT * FROM masked_receipts'), [])
            db.close()
            self.assertTrue(replay(path)['verified'])


class MaskedExportTests(unittest.TestCase):
    def test_ledger_export_feeds_masked_recovery_without_arrival_leak(self):
        db = connect(':memory:')
        run(db, 'mailroom', {'event_id': 'o1', 'op': 'port-open', 'port': 'sender'})
        run(db, 'mailroom', {'event_id': 'o2', 'op': 'port-open', 'port': 'receiver'})
        for ident, body, batch in [('A', 'burn-in', None), ('B', 'dry-run', None),
                                   ('C', 'flush', 'fl-1'), ('D', 'flush', 'fl-1'),
                                   ('E', 'rinse', None)]:
            run(db, 'mailroom', {'event_id': f'send-{ident}', 'op': 'send', 'id': ident,
                                 'port': 'sender', 'receiver': 'receiver', 'body': body,
                                 'instruction': body, 'batch': batch, 'generation': 'wo-7'})
        run(db, 'mailroom', {'event_id': 'cut-s', 'op': 'cut', 'id': 's1', 'port': 'sender'})
        for ident in 'ABC':
            run(db, 'mailroom', {'event_id': f'arr-{ident}', 'op': 'arrive',
                                 'id': ident, 'port': 'receiver'})
        run(db, 'mailroom', {'event_id': 'cut-r', 'op': 'cut', 'id': 'r1', 'port': 'receiver'})
        named = run(db, 'mailcheck', {'event_id': 'rec', 'op': 'inbox-recover', 'id': 'named',
                                      'sender_cut': 's1', 'receiver_cut': 'r1'})
        self.assertEqual(named, {'decision': 'inbox_ready', 'pending': ['D', 'E']})
        sent_ledger = run(db, 'mailroom', {'event_id': 'exp-s', 'op': 'ledger-export',
                                           'port': 'sender'})
        recv_ledger = run(db, 'mailroom', {'event_id': 'exp-r', 'op': 'ledger-export',
                                           'port': 'receiver'})
        self.assertEqual(sent_ledger['decision'], 'ledger_export')
        for exported in sent_ledger['items']:
            self.assertNotIn('arrived', exported)
        sender_cut = next(cut['seq'] for cut in sent_ledger['cuts'] if cut['id'] == 's1')
        arrivals = next(cut['seq'] for cut in recv_ledger['cuts'] if cut['id'] == 'r1')
        mfst = {'sender': 'sender', 'receiver': 'receiver', 'generation': 'wo-7',
                'sender_cut': sender_cut, 'receiver_arrivals': arrivals,
                'items': sent_ledger['items']}
        # Slips that name the true arrivals recover exactly the named pending queue.
        exact = slips(('A',), ('B',), ('C',))
        masked = recover(db, 'rec-m', 'masked', 'named', mfst, exact)
        self.assertEqual(masked, {'decision': 'inbox_ready', 'pending': [
            {'kind': 'job', 'job': 'D'}, {'kind': 'job', 'job': 'E'}]})
        # The same cut with masked slips must hold, matching the contract rule.
        masked_held = recover(db, 'rec-m2', 'masked-2', 'named', mfst, MAIN_SLIPS)
        self.assertEqual(masked_held, {'decision': 'inbox_held', 'pending': []})
        db.close()


class MaskedRegressionTests(unittest.TestCase):
    """Regression over slip sets with exactly two feasible assignments."""

    @staticmethod
    def generate_cases():
        rng = random.Random(20261128)
        cases = []
        seen = set()
        attempts = 0
        while len(cases) < 24 and attempts < 60000:
            attempts += 1
            count = rng.randint(3, 9)
            size = rng.randint(2, min(6, count - 1))
            cands = tuple(tuple(sorted(rng.sample(range(count), rng.randint(1, 3))))
                          for _ in range(size))
            if cands in seen:
                continue
            seen.add(cands)
            if len(brute_assignments(cands, count)) != 2:
                continue
            cases.append((count, cands))
        return cases

    @staticmethod
    def batch_layout(count, cands, rng):
        """Pair some jobs into interchangeable batches for the process view."""
        batches = {}
        positions = list(range(count))
        rng.shuffle(positions)
        while len(positions) >= 2 and rng.random() < 0.7:
            first, second = positions.pop(), positions.pop()
            token = f'batch-{first}'
            batches[first] = token
            batches[second] = token
        return batches

    def test_regression_two_assignment_slip_sets(self):
        cases = self.generate_cases()
        self.assertGreaterEqual(len(cases), 24)
        self.assertLessEqual(sum(len(cands) for _, cands in cases), 300)
        competing = 0
        divergent = 0
        swapped = 0
        rng = random.Random(4179)
        db = connect(':memory:')
        for number, (count, cands) in enumerate(cases):
            with self.subTest(case=number, count=count, cands=cands):
                assignments = brute_assignments(cands, count)
                self.assertEqual(len(assignments), 2)
                images = {frozenset(assignment) for assignment in assignments}
                if len(images) == 2:
                    divergent += 1
                else:
                    swapped += 1
                if any(set(left) & set(right)
                       for i, left in enumerate(cands) for right in cands[i + 1:]):
                    competing += 1
                # The analysis must match brute force on every kept position.
                self.assertEqual(impl_slots(count, cands), brute_slots(count, cands))
                batches = self.batch_layout(count, cands, rng)
                views = {
                    'named': [('job', f'j{pos}') for pos in range(count)],
                    'process': [('batch', batches[pos], 'flush')
                                if pos in batches else ('job', f'j{pos}')
                                for pos in range(count)],
                }
                items = []
                for pos in range(count):
                    if pos in batches:
                        entry = item(f'j{pos}', pos + 1, f'work-{pos}', batch=batches[pos],
                                      interchangeable=True)
                        entry['instruction'] = 'flush'
                    else:
                        entry = item(f'j{pos}', pos + 1, f'work-{pos}')
                    items.append(entry)
                mfst = manifest(items, sender_cut=count, arrivals=len(cands))
                slip_list = [{'id': f's{index}', 'candidates': [f'j{pos}' for pos in group]}
                             for index, group in enumerate(cands)]
                for contract, tokens in views.items():
                    self.assertEqual(impl_plan(count, cands, tokens),
                                     brute_plan(count, cands, tokens))
                    box = f'case-{number}-{contract}'
                    batches_arg = sorted(set(batches.values())) if contract == 'process' else None
                    result = recover(db, f'rec-{number}-{contract}', box, contract,
                                     mfst, slip_list, batches=batches_arg)
                    expected = brute_plan(count, cands, tokens)
                    persisted = rows(db, '''SELECT kind, ref, instruction FROM masked_pending
                                           WHERE package=? ORDER BY position''', (box,))
                    actual = [('batch', row['ref'], row['instruction']) if row['kind'] == 'batch'
                              else ('job', row['ref']) for row in persisted]
                    self.assertEqual(actual, expected)
                    self.assertEqual(result['decision'],
                                     'inbox_ready' if expected else 'inbox_held')
        self.assertGreaterEqual(competing, 8)
        self.assertGreaterEqual(divergent, 6)
        self.assertGreaterEqual(swapped, 2)
        db.close()

    def test_random_crosscheck_against_brute_force(self):
        rng = random.Random(916)
        for _ in range(300):
            count = rng.randint(2, 7)
            size = rng.randint(1, count)
            cands = tuple(tuple(sorted(rng.sample(range(count), rng.randint(1, min(3, count)))))
                          for _ in range(size))
            self.assertEqual(impl_slots(count, cands), brute_slots(count, cands))
            batches = {pos: 'shared' for pos in range(count) if rng.random() < 0.3}
            tokens = [('batch', batches[pos], 'flush')
                      if pos in batches else ('job', f'j{pos}') for pos in range(count)]
            self.assertEqual(impl_plan(count, cands, tokens),
                             brute_plan(count, cands, tokens))

    def test_acceptance_scale_three_hundred_slips(self):
        # 150 competing slip pairs over 450 jobs: every pair forces its two
        # jobs to be delivered, leaving a fully determined queue of 150 jobs.
        pairs = 150
        count = 3 * pairs
        cands = []
        for k in range(pairs):
            cands.append((2 * k, 2 * k + 1))
            cands.append((2 * k, 2 * k + 1))
        tokens = [('job', f'j{pos}') for pos in range(count)]
        expected = [('job', f'j{pos}') for pos in range(2 * pairs, count)]
        self.assertEqual(impl_plan(count, cands, tokens), expected)
        # One ambiguous pair hidden among 299 forced slips: exactly two legal
        # histories, and only the interchangeability of p/q releases the box.
        count = 302
        cands = [(pos,) for pos in range(299)] + [(299, 300)]
        named = [('job', f'j{pos}') for pos in range(count)]
        self.assertEqual(impl_plan(count, cands, named), [])
        process = [('batch', 'fl-x', 'flush') if pos in (299, 300)
                   else ('job', f'j{pos}') for pos in range(count)]
        self.assertEqual(impl_plan(count, cands, process),
                         [('batch', 'fl-x', 'flush'), ('job', 'j301')])


class MaskedFixtureTests(unittest.TestCase):
    def test_fixture_flow_and_replay(self):
        fixture = json.loads((ROOT / 'fixtures' / 'mailmasked.json').read_text(encoding='utf-8'))
        with TemporaryDirectory() as folder:
            database = Path(folder) / 'masked.sqlite'
            db = connect(database)
            results = {}
            for event in fixture:
                results[event['input']['event_id']] = run(db, event['command'], event['input'])
            db.close()
            self.assertEqual(results['recover-named'], {'decision': 'inbox_held', 'pending': []})
            self.assertEqual(results['deliver-named-1'], {'decision': 'inbox_held'})
            self.assertEqual(results['recover-proc'], {'decision': 'inbox_ready', 'pending': [
                {'kind': 'batch', 'batch': 'fl-1', 'instruction': 'flush'},
                {'kind': 'job', 'job': 'E'}]})
            self.assertEqual(results['proc-deliver-1']['decision'], 'work_delivered')
            self.assertEqual(results['proc-deliver-2']['decision'], 'mail_delivered')
            self.assertEqual(results['proc-deliver-3'], {'decision': 'inbox_empty'})
            self.assertEqual(results['recover-swap'], {'decision': 'inbox_held', 'pending': []})
            self.assertEqual(results['deliver-swap-1'], {'decision': 'inbox_held'})
            self.assertEqual(results['recover-prefix'],
                             {'decision': 'inbox_ready', 'pending': [{'kind': 'job', 'job': 'B'}]})
            self.assertEqual(results['prefix-deliver-1']['decision'], 'mail_delivered')
            self.assertEqual(results['prefix-deliver-2'], {'decision': 'inbox_held'})
            self.assertEqual(results['recover-short'], {'decision': 'evidence_incomplete'})
            self.assertEqual(results['recover-clash'], {'decision': 'no_legal_history'})
            self.assertEqual(results['deliver-clash'], {'decision': 'inbox_missing'})
            self.assertEqual(results['recover-beyond'],
                             {'decision': 'candidate_beyond_cut', 'candidate': 'F'})
            self.assertEqual(results['recover-port'],
                             {'decision': 'candidate_wrong_port', 'candidate': 'G'})
            self.assertEqual(results['recover-generation'],
                             {'decision': 'candidate_wrong_generation', 'candidate': 'H'})
            self.assertEqual(results['recover-unknown'],
                             {'decision': 'candidate_unknown', 'candidate': 'Z'})
            self.assertEqual(replay(database),
                             {'decisions': 18, 'verified': True, 'divergences': []})


if __name__ == '__main__':
    unittest.main()
