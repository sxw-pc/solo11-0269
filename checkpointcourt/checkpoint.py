import hashlib
from .store import one,rows
SCHEMA='''
CREATE TABLE IF NOT EXISTS checkpoint_requests(id TEXT PRIMARY KEY,pod TEXT,uid TEXT,generation INTEGER,source TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS manifests(id TEXT PRIMARY KEY,request TEXT,part_count INTEGER,digest TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS chunks(id TEXT PRIMARY KEY,manifest TEXT,part INTEGER,content TEXT,digest TEXT,UNIQUE(manifest,part));
'''
def handle(db,x):
    if x['op']=='checkpoint-request':
        old=one(db,'SELECT MAX(generation) g FROM checkpoint_requests WHERE pod=?',(x['pod'],))
        if old['g'] is not None and x['generation']<=old['g']:return {'decision':'stale_checkpoint'}
        db.execute('INSERT INTO checkpoint_requests VALUES(?,?,?,?,?,?)',(x['id'],x['pod'],x['uid'],x['generation'],x['source'],'collecting'))
        return {'decision':'checkpoint_requested'}
    if x['op']=='image-manifest':
        r=one(db,'SELECT * FROM checkpoint_requests WHERE id=?',(x['request'],))
        if not r or r['status']!='collecting' or x['part_count']<1:return {'decision':'manifest_denied'}
        db.execute('INSERT INTO manifests VALUES(?,?,?,?,?)',(x['id'],r['id'],x['part_count'],x['digest'],'open'))
        return {'decision':'manifest_open'}
    if x['op']=='chunk':
        m=one(db,'SELECT * FROM manifests WHERE id=?',(x['manifest'],))
        if not m or m['status']!='open' or not 0<=x['part']<m['part_count']:return {'decision':'chunk_denied'}
        digest=hashlib.sha256(x['content'].encode()).hexdigest()
        old=one(db,'SELECT * FROM chunks WHERE manifest=? AND part=?',(m['id'],x['part']))
        if old:return {'decision':'chunk_same' if old['digest']==digest else 'chunk_conflict'}
        db.execute('INSERT INTO chunks VALUES(?,?,?,?,?)',(x['id'],m['id'],x['part'],x['content'],digest))
        return {'decision':'chunk_stored'}
    if x['op']=='chunk-verify':
        m=one(db,'SELECT * FROM manifests WHERE id=?',(x['manifest'],))
        if not m or m['status']!='open':return {'decision':'verify_denied'}
        chunks=rows(db,'SELECT * FROM chunks WHERE manifest=? ORDER BY part',(m['id'],))
        closed=[c['part'] for c in chunks]==list(range(m['part_count']))
        digest=hashlib.sha256(''.join(c['content'] for c in chunks).encode()).hexdigest() if closed else None
        if not closed or digest!=m['digest']:return {'decision':'image_incomplete','closed':closed}
        db.execute("UPDATE manifests SET status='sealed' WHERE id=?",(m['id'],))
        db.execute("UPDATE checkpoint_requests SET status='sealed' WHERE id=?",(m['request'],))
        return {'decision':'checkpoint_sealed'}
    return {'decision':'unknown_operation'}
