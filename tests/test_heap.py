import json
from pathlib import Path
import tempfile
import unittest

from checkpointcourt.store import connect, replay, run

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'heap.json'


class HeapTests(unittest.TestCase):
    def test_capture_gate_census_counts_shared_and_cyclic_nodes_once(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'heap.sqlite'
            db = connect(path)
            items = json.loads(FIXTURE.read_bytes())
            results = [run(db, item['command'], item['input']) for item in items]
            self.assertEqual(results[4], {'decision': 'heap_capture_missing'})
            self.assertEqual(results[6], {'decision': 'heap_catalog_loaded', 'objects': 5})
            self.assertEqual(results[7], {'decision':'heap_counted','live':['A','B','S'],
                                         'unreachable':['U','V'],'live_bytes':14,'unreachable_bytes':11})
            db.close()
            self.assertTrue(replay(path)['verified'])
