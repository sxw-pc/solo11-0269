import json
import tempfile
import unittest
from pathlib import Path

from checkpointcourt.store import connect, one, replay, run

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'wire.json'


class TransportTests(unittest.TestCase):
    def test_corrupt_bytes_charge_capacity_without_becoming_a_part(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'wire.sqlite')
            db = connect(path)
            events = json.loads(FIXTURE.read_bytes())
            results = [run(db, row['command'], row['input']) for row in events]
            self.assertEqual(results[9:13], [
                {'decision': 'wire_sent', 'receipt': 'copy_part_mismatch', 'remaining': 2},
                {'decision': 'wire_window_required'},
                {'decision': 'wire_capacity_exceeded', 'remaining': 2},
                {'decision': 'wire_sent', 'receipt': 'copy_part_stored', 'remaining': 0},
            ])
            self.assertEqual(results[-1], {'decision': 'target_active'})
            self.assertEqual(run(db, events[9]['command'], events[9]['input']), results[9])
            self.assertEqual(one(db, "SELECT used FROM wire_windows WHERE id='first'")['used'], 4)
            self.assertEqual(one(db, 'SELECT COUNT(*) n FROM target_parts')['n'], 2)
            db.close()
            self.assertTrue(replay(path)['verified'])

    def test_window_end_is_exclusive(self):
        events = json.loads(FIXTURE.read_bytes())
        db = connect(':memory:')
        self.addCleanup(db.close)
        for item in events[:9]:
            run(db, item['command'], item['input'])
        late = dict(events[9]['input'], at=20)
        self.assertEqual(run(db, 'transport', late), {'decision': 'wire_outside_window'})
        self.assertEqual(one(db, "SELECT used FROM wire_windows WHERE id='first'")['used'], 0)
