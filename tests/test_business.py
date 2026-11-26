import json,tempfile,unittest
from pathlib import Path
import checkpointcourt
from checkpointcourt.store import connect,run,replay,one,rows

ROOT=Path(__file__).resolve().parents[1]
class BusinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'db.sqlite';self.db=connect(str(self.path))
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def feed(self,name):
        commands=json.loads((ROOT/'fixtures'/name).read_text(encoding='utf-8'))
        return [run(self.db,v['command'],v['input']) for v in commands]
    def test_end_to_end_persists_independent_decisions(self):
        result=self.feed('path.json')
        self.assertGreaterEqual(len(result),10)
        self.assertEqual(one(self.db,"SELECT status FROM transfer_plans WHERE id='plan'")['status'],'committed')
        self.assertEqual(one(self.db,"SELECT node FROM active_instances WHERE pod='p'")['node'],'target-node')
    def test_failure_changes_result_without_erasing_prior_state(self):
        results=self.feed('path.json')
        self.assertIn('fallback_unproven',[r['decision'] for r in results])
        self.assertTrue(rows(self.db,'SELECT * FROM events'))
    def test_replay_and_event_conflict(self):
        self.feed('path.json')
        event=json.loads((ROOT/'fixtures'/'path.json').read_text(encoding='utf-8'))[0]
        self.assertEqual(run(self.db,event['command'],event['input']),json.loads(one(self.db,'SELECT result FROM events WHERE event_id=?',(event['input']['event_id'],))['result']))
        changed=dict(event['input'],op='changed')
        self.assertEqual(run(self.db,event['command'],changed)['decision'],'event_identity_conflict')
        report=replay(str(self.path))
        self.assertTrue(report['verified'],report['divergences'])
    def test_six_fixtures_are_runnable(self):
        for command in ['checkpoint', 'transfer', 'restore']:
            for kind in ('normal','failure'):
                with self.subTest(command=command,kind=kind):
                    db=connect(':memory:')
                    for v in json.loads((ROOT/'fixtures'/f'{command}_{kind}.json').read_text(encoding='utf-8')):
                        run(db,v['command'],v['input'])
                    self.assertGreaterEqual(one(db,'SELECT COUNT(*) n FROM events')['n'],1)
                    db.close()
    def test_module_origin(self):
        self.assertEqual(Path(checkpointcourt.__file__).resolve().parents[1],ROOT)
    def test_target_cannot_restore_before_source_fence(self):
        events=json.loads((ROOT/'fixtures'/'path.json').read_text(encoding='utf-8'))
        for event in events[:6]:run(self.db,event['command'],event['input'])
        self.assertEqual(run(self.db,'restore',{'event_id':'too-early','op':'target-restore','id':'too-early','plan':'plan','target':'target-node','digest':events[1]['input']['digest'],'at':15})['decision'],'restore_denied')
        self.assertIsNone(one(self.db,"SELECT * FROM active_instances WHERE pod='p'"))
    def test_fallback_requires_persisted_source_witness(self):
        events=json.loads((ROOT/'fixtures'/'path.json').read_text(encoding='utf-8'))
        for event in events[:7]:run(self.db,event['command'],event['input'])
        request={'event_id':'fallback','op':'failure-fallback','id':'fallback','plan':'plan','reason':'target_failed','at':25}
        self.assertEqual(run(self.db,'restore',request)['decision'],'fallback_unproven')
        self.assertEqual(run(self.db,'transfer',{'event_id':'witness','op':'source-recovery-witness','id':'witness','plan':'plan','request':'req','uid':'u1','generation':3,'at':20})['decision'],'source_recoverable')
        self.assertEqual(run(self.db,'restore',dict(request,event_id='fallback-2',id='fallback-2'))['decision'],'source_restored')
        self.assertEqual(one(self.db,"SELECT node FROM active_instances WHERE pod='p'")['node'],'source-node')
    def test_second_transfer_cannot_overwrite_committed_target(self):
        events=json.loads((ROOT/'fixtures'/'path.json').read_text(encoding='utf-8'))
        for event in events[:9]:run(self.db,event['command'],event['input'])
        second={'event_id':'second-plan','op':'transfer-reserve','id':'second-plan','reservation':'second-slot','request':'req','target':'third-node','expires':100,'at':21}
        self.assertEqual(run(self.db,'transfer',second)['decision'],'transfer_already_owned')
        self.assertEqual(run(self.db,'transfer',{'event_id':'second-freeze','op':'source-freeze','id':'second-fence','plan':'second-plan','uid':'u1','generation':3,'at':22})['decision'],'freeze_denied')
        self.assertEqual(one(self.db,"SELECT node FROM active_instances WHERE pod='p'")['node'],'target-node')

print('module_file',checkpointcourt.__file__)
