from . import custody, transport
from .store import one
SCHEMA='''
CREATE TABLE IF NOT EXISTS transfer_plans(id TEXT PRIMARY KEY,request TEXT,target TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS source_fences(id TEXT PRIMARY KEY,request TEXT,uid TEXT,generation INTEGER,at INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS target_reservations(id TEXT PRIMARY KEY,plan TEXT,target TEXT,expires INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS source_recovery_receipts(id TEXT PRIMARY KEY,plan TEXT,request TEXT,uid TEXT,generation INTEGER,at INTEGER,status TEXT);
''' + custody.SCHEMA
def handle(db,x):
    if x['op'] in ('copy-open','copy-part','copy-verify'):
        if x['op']=='copy-part' and one(db,'SELECT 1 FROM windowed_plans WHERE plan=?',(x['plan'],)):
            return {'decision':'wire_window_required'}
        return custody.transfer(db,x)
    if x['op']=='transfer-reserve':
        r=one(db,'SELECT * FROM checkpoint_requests WHERE id=?',(x['request'],))
        m=one(db,"SELECT * FROM manifests WHERE request=? AND status='sealed'",(x['request'],))
        if not r or not m or r['status']!='sealed' or x['target']==r['source'] or x['expires']<=x['at']:
            return {'decision':'transfer_denied'}
        prior=one(db,"SELECT * FROM transfer_plans WHERE request=? AND status IN ('reserved','frozen','committed')",(r['id'],))
        if prior:return {'decision':'transfer_already_owned','plan':prior['id']}
        db.execute('INSERT INTO transfer_plans VALUES(?,?,?,?)',(x['id'],r['id'],x['target'],'reserved'))
        db.execute('INSERT INTO target_reservations VALUES(?,?,?,?,?)',(x['reservation'],x['id'],x['target'],x['expires'],'active'))
        if x.get('wire_only') is True:
            db.execute('INSERT INTO windowed_plans VALUES (?)',(x['id'],))
        return {'decision':'target_reserved'}
    if x['op']=='source-freeze':
        p=one(db,'SELECT * FROM transfer_plans WHERE id=?',(x['plan'],))
        r=one(db,'SELECT * FROM checkpoint_requests WHERE id=?',(p['request'],)) if p else None
        if not r or p['status']!='reserved' or x['uid']!=r['uid'] or x['generation']!=r['generation']:
            return {'decision':'freeze_denied'}
        db.execute('INSERT INTO source_fences VALUES(?,?,?,?,?,?)',(x['id'],r['id'],r['uid'],r['generation'],x.get('at',0),'frozen'))
        db.execute("UPDATE transfer_plans SET status='frozen' WHERE id=?",(p['id'],))
        return {'decision':'source_frozen'}
    if x['op']=='cancel-transfer':
        p=one(db,'SELECT * FROM transfer_plans WHERE id=?',(x['plan'],))
        if not p or p['status']=='committed':return {'decision':'cancel_denied'}
        db.execute("UPDATE transfer_plans SET status='cancelled' WHERE id=?",(p['id'],))
        db.execute("UPDATE target_reservations SET status='released' WHERE plan=?",(p['id'],))
        return {'decision':'transfer_cancelled'}
    if x['op']=='source-recovery-witness':
        r=one(db,'SELECT * FROM checkpoint_requests WHERE id=?',(x['request'],))
        p=one(db,'SELECT * FROM transfer_plans WHERE id=?',(x['plan'],))
        f=one(db,'SELECT * FROM source_fences WHERE request=?',(x['request'],))
        if not r or not p or p['request']!=r['id'] or p['status']!='frozen' or not f or x['uid']!=r['uid'] or x['generation']!=r['generation'] or x['at']<f['at']:
            return {'decision':'source_witness_denied'}
        db.execute('INSERT INTO source_recovery_receipts VALUES(?,?,?,?,?,?,?)',(x['id'],p['id'],r['id'],r['uid'],r['generation'],x['at'],'intact'))
        return {'decision':'source_recoverable'}
    return {'decision':'unknown_operation'}
