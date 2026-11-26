from . import custody
from .store import one
SCHEMA='''
CREATE TABLE IF NOT EXISTS restore_attempts(id TEXT PRIMARY KEY,plan TEXT,target TEXT,digest TEXT,at INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS active_instances(pod TEXT PRIMARY KEY,uid TEXT,node TEXT,generation INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS fallback_cases(id TEXT PRIMARY KEY,plan TEXT,reason TEXT,status TEXT);
'''
def handle(db,x):
    if x['op']=='target-restore':
        p=one(db,'SELECT * FROM transfer_plans WHERE id=?',(x['plan'],))
        r=one(db,'SELECT * FROM checkpoint_requests WHERE id=?',(p['request'],)) if p else None
        m=one(db,'SELECT * FROM manifests WHERE request=?',(r['id'],)) if r else None
        reserve=one(db,"SELECT * FROM target_reservations WHERE plan=? AND status='active'",(x['plan'],))
        fence=one(db,"SELECT * FROM source_fences WHERE request=? AND status='frozen'",(r['id'],)) if r else None
        if not p or p['status']!='frozen' or not reserve or not fence or reserve['expires']<=x['at'] or x['target']!=p['target'] or x['digest']!=m['digest']:
            return {'decision':'restore_denied'}
        if not custody.authorizes_restore(db,p,m,x['at']):
            return {'decision':'target_copy_unproven'}
        db.execute('INSERT INTO restore_attempts VALUES(?,?,?,?,?,?)',(x['id'],p['id'],p['target'],x['digest'],x['at'],'verified'))
        return {'decision':'target_verified'}
    if x['op']=='single-active-commit':
        a=one(db,'SELECT * FROM restore_attempts WHERE id=?',(x['restore'],))
        p=one(db,'SELECT * FROM transfer_plans WHERE id=?',(a['plan'],)) if a else None
        r=one(db,'SELECT * FROM checkpoint_requests WHERE id=?',(p['request'],)) if p else None
        if not r or a['status']!='verified' or p['status']!='frozen':return {'decision':'activation_denied'}
        old=one(db,'SELECT * FROM active_instances WHERE pod=?',(r['pod'],))
        if old and old['generation']>=r['generation']:return {'decision':'active_conflict'}
        db.execute('INSERT INTO active_instances VALUES(?,?,?,?,?) ON CONFLICT(pod) DO UPDATE SET uid=excluded.uid,node=excluded.node,generation=excluded.generation,status=excluded.status',(r['pod'],r['uid'],p['target'],r['generation'],'active'))
        db.execute("UPDATE transfer_plans SET status='committed' WHERE id=?",(p['id'],))
        return {'decision':'target_active'}
    if x['op']=='failure-fallback':
        p=one(db,'SELECT * FROM transfer_plans WHERE id=?',(x['plan'],))
        r=one(db,'SELECT * FROM checkpoint_requests WHERE id=?',(p['request'],)) if p else None
        fence=one(db,'SELECT * FROM source_fences WHERE request=?',(r['id'],)) if r else None
        witness=one(db,"SELECT * FROM source_recovery_receipts WHERE plan=? AND request=? AND status='intact' ORDER BY at DESC LIMIT 1",(p['id'],r['id'])) if r else None
        active=one(db,'SELECT * FROM active_instances WHERE pod=?',(r['pod'],)) if r else None
        committed=one(db,"SELECT 1 FROM transfer_plans WHERE request=? AND id<>? AND status='committed'",(r['id'],p['id'])) if r else None
        if not r or p['status']!='frozen' or not fence or not witness or witness['at']>x['at'] or committed or active and active['node']!=r['source']:
            return {'decision':'fallback_unproven'}
        db.execute('INSERT INTO fallback_cases VALUES(?,?,?,?)',(x['id'],p['id'],x['reason'],'recovered'))
        db.execute('INSERT INTO active_instances VALUES(?,?,?,?,?) ON CONFLICT(pod) DO UPDATE SET uid=excluded.uid,node=excluded.node,generation=excluded.generation,status=excluded.status',(r['pod'],r['uid'],r['source'],r['generation'],'active'))
        db.execute("UPDATE transfer_plans SET status='rolled_back' WHERE id=?",(p['id'],))
        return {'decision':'source_restored'}
    return {'decision':'unknown_operation'}
