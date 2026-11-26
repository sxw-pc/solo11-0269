import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from checkpointcourt.store import connect, replay, run


class MailroomTests(unittest.TestCase):
    def test_receiving_cut_controls_the_physical_resume_inbox(self):
        fixture = Path(__file__).resolve().parents[1] / 'fixtures' / 'mailroom.json'
        with TemporaryDirectory() as folder:
            database = Path(folder) / 'mail.sqlite'
            db = connect(database)
            results = {}
            for event in json.loads(fixture.read_text(encoding='utf-8')):
                result = run(db, event['command'], event['input'])
                results[event['input']['event_id']] = result
            db.close()
            self.assertEqual(results['recover-work'], {'decision': 'inbox_ready', 'pending': ['a']})
            self.assertEqual(results['actual-work'], {'decision': 'mail_delivered', 'delivery': 'a', 'body': 'tile A'})
            self.assertEqual(results['recover-invalid'], {'decision': 'inbox_inconsistent', 'delivery': 'a'})
            self.assertEqual(results['recover-empty'], {'decision': 'inbox_ready', 'pending': []})
            self.assertEqual(results['actual-empty'], {'decision': 'inbox_empty'})
            self.assertEqual(replay(database), {'decisions': 13, 'verified': True, 'divergences': []})