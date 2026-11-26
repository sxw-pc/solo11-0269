import json
from pathlib import Path
import tempfile
import unittest

from checkpointcourt.store import connect, one, replay, run

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'archive.json'


class ArchiveTests(unittest.TestCase):
    def test_repair_recovers_verified_bytes_and_replay(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'archive.sqlite'
            db = connect(path)
            events = json.loads(FIXTURE.read_bytes())
            results = [run(db, item['command'], item['input']) for item in events]
            self.assertEqual(results[5], {'decision': 'archive_source_unsealed'})
            self.assertEqual(results[8], {'decision': 'archive_part_mismatch'})
            self.assertEqual(results[10], {'decision': 'archive_insufficient', 'missing': [1, 2]})
            self.assertEqual(results[12], {'decision': 'archive_part_repaired', 'part': 1, 'payload_hex': '53454544'})
            self.assertEqual(results[13], {'decision': 'archive_complete'})
            self.assertEqual(one(db, 'SELECT payload,origin FROM archive_members WHERE part=1'),
                             {'payload': b'SEED', 'origin': 'recovered'})
            self.assertEqual(run(db, 'archive', events[12]['input']), results[12])
            db.close()
            self.assertTrue(replay(path)['verified'])

    def test_new_arrival_cannot_change_repaired_provenance(self):
        db = connect(':memory:')
        for item in json.loads(FIXTURE.read_bytes()):
            run(db, item['command'], item['input'])
        received = {'op': 'member-receive', 'event_id': 'late', 'stripe': 'stripe', 'part': 1, 'content': 'SEED'}
        self.assertEqual(run(db, 'archive', received), {'decision': 'archive_part_already_present'})
        self.assertEqual(one(db, 'SELECT origin FROM archive_members WHERE part=1'), {'origin': 'recovered'})
        db.close()
