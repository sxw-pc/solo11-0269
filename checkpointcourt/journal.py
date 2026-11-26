"""Record committed synthetic source writes as atomic page batches."""
from .store import one, rows

SCHEMA = '''
CREATE TABLE IF NOT EXISTS page_sources (
    id TEXT PRIMARY KEY, head INTEGER, pages INTEGER, width INTEGER
);
CREATE TABLE IF NOT EXISTS page_versions (
    source TEXT, tick INTEGER, page INTEGER, payload BLOB,
    PRIMARY KEY (source,tick,page)
);
'''


def decode(changes, width, pages):
    if not isinstance(changes, dict) or not changes:
        return None
    result = {}
    for key, raw in changes.items():
        if not isinstance(key,str) or not key.isascii() or not key.isdigit() or len(key) > len(str(pages)):
            return None
        part = int(key)
        if key != str(part) or part >= pages or not isinstance(raw,str):
            return None
        try:
            payload = bytes.fromhex(raw)
        except ValueError:
            return None
        if len(payload) != width:
            return None
        result[part] = payload
    return result


def create(db, event):
    source, pages, width = event['id'], event['pages'], event['width']
    if (not isinstance(source,str) or not source or type(pages) is not int or type(width) is not int
            or not 1 <= pages <= 256 or not 1 <= width <= 4096):
        return {'decision': 'pages_invalid'}
    if one(db,'SELECT 1 FROM page_sources WHERE id=?',(source,)):
        return {'decision': 'pages_exist'}
    db.execute('INSERT INTO page_sources VALUES (?,?,?,?)',(source,0,pages,width))
    db.executemany('INSERT INTO page_versions VALUES (?,?,?,?)',
                   ((source,0,page,bytes(width)) for page in range(pages)))
    return {'decision': 'pages_created', 'pages': pages, 'width': width}


def write(db, event):
    source = one(db,'SELECT * FROM page_sources WHERE id=?',(event['source'],))
    tick = event['tick']
    if not source or type(tick) is not int or tick != source['head']+1:
        return {'decision': 'page_commit_not_next'}
    changes = decode(event['changes'],source['width'],source['pages'])
    if changes is None:
        return {'decision': 'page_commit_invalid'}
    db.executemany('INSERT INTO page_versions VALUES (?,?,?,?)',
                   ((source['id'],tick,page,payload) for page,payload in sorted(changes.items())))
    db.execute('UPDATE page_sources SET head=? WHERE id=?',(tick,source['id']))
    return {'decision': 'pages_committed','tick':tick,'changed':sorted(changes)}


def image_at(db, source, tick):
    found = rows(db, '''
        SELECT v.page,v.payload FROM page_versions v
        JOIN (SELECT page,MAX(tick) as newest FROM page_versions WHERE source=? AND tick<=? GROUP BY page) p
          ON v.page=p.page AND v.tick=p.newest
        WHERE v.source=? ORDER BY v.page
    ''',(source,tick,source))
    return {part['page']: part['payload'] for part in found}


def handle(db, event):
    if event['op'] == 'page-source':
        return create(db,event)
    if event['op'] == 'page-commit':
        return write(db,event)
    return {'decision': 'unknown_operation'}
