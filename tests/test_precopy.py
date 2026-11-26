import json
from pathlib import Path
import tempfile
import unittest

from checkpointcourt.store import connect, replay, run

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'precopy.json'


class PrecopyTests(unittest.TestCase):
    def test_rounds_compare_committed_bytes_and_have_a_stable_cut(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'round.sqlite'
            db = connect(path)
            entries = json.loads(FIXTURE.read_bytes())
            results = [run(db,item['command'],item['input']) for item in entries]
            self.assertEqual(results[1]['actual_bytes'], 6)
            self.assertEqual(results[2], {'decision':'round_exported','tick':0,'pages':{},'actual_bytes':0})
            self.assertEqual(results[3], {'decision':'page_commit_invalid'})
            self.assertEqual(results[5], {'decision':'round_exported','tick':1,
                                         'pages':{'0':'abcd','1':'ef01'},'actual_bytes':4})
            self.assertEqual(results[7]['pages'], {'1':'ef01'})
            self.assertEqual(run(db, 'precopy', entries[5]['input']), results[5])
            db.close()
            self.assertTrue(replay(path)['verified'])
