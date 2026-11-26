import json
import tempfile
import unittest
from pathlib import Path

from checkpointcourt.store import connect, one, replay, run

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'target-copy.json'


class CustodyTests(unittest.TestCase):
    def test_copy_required_for_opted_in_restore(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'migration.sqlite')
            db = connect(path)
            events = json.loads(FIXTURE.read_text())
            decisions = [run(db, item['command'], item['input'])['decision'] for item in events]
            self.assertEqual(decisions[7:], [
                'copy_open', 'target_copy_unproven', 'copy_part_mismatch', 'copy_part_stored',
                'copy_incomplete', 'copy_part_stored', 'copy_unproven', 'copy_verified',
                'target_verified', 'target_active',
            ])
            self.assertEqual(one(db, 'SELECT COUNT(*) n FROM target_parts')['n'], 2)
            self.assertEqual(one(db, "SELECT node FROM active_instances WHERE pod='p'")['node'], 'target-node')
            db.close()
            self.assertTrue(replay(path)['verified'])

    def test_wrong_destination_and_expiration_cannot_open_copy(self):
        db = connect(':memory:')
        self.addCleanup(db.close)
        events = json.loads(FIXTURE.read_text())
        for item in events[:7]:
            run(db, item['command'], item['input'])
        event = events[7]['input']
        self.assertEqual(run(db, 'transfer', dict(event, event_id='wrong-destination', target='third'))['decision'], 'copy_denied')
        self.assertEqual(run(db, 'transfer', dict(event, event_id='expired', at=100))['decision'], 'copy_expired')
        self.assertIsNone(one(db, 'SELECT * FROM target_copies'))
