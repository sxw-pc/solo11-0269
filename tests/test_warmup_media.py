import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from checkpointcourt.store import connect, replay, run


class WarmupMediaTests(unittest.TestCase):
    def test_first_request_reads_resident_pages_and_never_exceeds_budget(self):
        fixture = Path(__file__).resolve().parents[1] / 'fixtures' / 'warmup_media.json'
        with TemporaryDirectory() as folder:
            database = Path(folder) / 'warmup.sqlite'
            db = connect(database)
            results = {}
            for operation in json.loads(fixture.read_text(encoding='utf-8')):
                event = operation['input']
                results[event['event_id']] = run(db, operation['command'], event)
            db.close()
            self.assertEqual(results['initial-miss'], {'decision':'first_miss','missing':['Q','S']})
            self.assertEqual(results['good-serve-W3'], {'decision':'first_served','reply':['q3','s3'],'reads':3})
            self.assertEqual(results['decoy-serve-W0']['decision'], 'first_served')
            self.assertEqual(results['decoy-W3-Z']['body'], results['decoy-W0-Z']['body'])
            self.assertEqual(results['decoy-W3-Q']['decision'], 'read_budget_exhausted')
            self.assertEqual(results['decoy-serve-W3'], {'decision':'first_miss','missing':['Q','S']})
            self.assertTrue(replay(database)['verified'])