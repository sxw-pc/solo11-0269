import json
import tempfile
import unittest
from pathlib import Path

from checkpointcourt.store import connect, replay, run

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'forensics.json'


class ForensicsTests(unittest.TestCase):
    def test_capture_requires_seal_and_comparison_preserves_gaps(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'forensics.sqlite')
            db = connect(path)
            items = json.loads(FIXTURE.read_bytes())
            results = [run(db, e['command'], e['input']) for e in items]
            self.assertEqual(results[3]['decision'], 'capture_denied')
            self.assertEqual(results[5], {'decision':'capture_read','byte_count':3})
            self.assertEqual(results[-1], {'decision':'comparison_recorded','differences':[
                {'region':'heap','offset':3,'before':2,'after':4},
                {'region':'heap','offset':4,'before':3,'after':None},
            ]})
            db.close()
            self.assertTrue(replay(path)['verified'])
