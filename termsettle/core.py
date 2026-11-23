"""Immutable offline evidence and deterministic business projections."""
import hashlib, json, sqlite3
from pathlib import Path
from . import policy

SCHEMA="""
CREATE TABLE IF NOT EXISTS source_files (sha TEXT PRIMARY KEY, subsystem TEXT NOT NULL, raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, subsystem TEXT NOT NULL, at TEXT NOT NULL, version INTEGER NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL, sha TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS business_entities (subsystem TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, state TEXT NOT NULL, source_id TEXT NOT NULL, PRIMARY KEY(subsystem,entity_type,entity_id));
CREATE TABLE IF NOT EXISTS current_decisions (subject TEXT NOT NULL, decision TEXT NOT NULL, result TEXT NOT NULL, reason TEXT NOT NULL, evidence TEXT NOT NULL, PRIMARY KEY(subject,decision));
CREATE TABLE IF NOT EXISTS decision_history (revision INTEGER NOT NULL, subject TEXT NOT NULL, decision TEXT NOT NULL, result TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(revision,subject,decision));
CREATE TABLE IF NOT EXISTS transition_log (revision INTEGER NOT NULL, subsystem TEXT NOT NULL, source_sha TEXT NOT NULL, subject TEXT NOT NULL, decision TEXT NOT NULL, old_result TEXT, new_result TEXT NOT NULL, PRIMARY KEY(revision,subject,decision));
"""

def connect(path):
    path=Path(path)
    if str(path)!=':memory:': path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(str(path));db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db

def canonical(value): return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))

def import_file(db,subsystem,file):
    raw=Path(file).read_bytes();sha=hashlib.sha256(raw).hexdigest();payload=json.loads(raw)
    if not isinstance(payload,list) or not payload:raise ValueError('nonempty JSON event list required')
    if db.execute('SELECT 1 FROM source_files WHERE sha=?',(sha,)).fetchone():return {'inserted':0,'replayed':len(payload),'sha':sha}
    inserted=replayed=0
    db.execute('SAVEPOINT import_events')
    try:
        for event in payload:
            if event.get('subsystem')!=subsystem or not all(k in event for k in ('id','at','version','subject','action','data')):raise ValueError('event envelope mismatch')
            body=canonical(event);digest=hashlib.sha256(body.encode()).hexdigest()
            old=db.execute('SELECT sha FROM events WHERE id=?',(event['id'],)).fetchone()
            if old:
                if old[0]!=digest:raise ValueError('same event id with different content')
                replayed+=1;continue
            db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?)',(event['id'],subsystem,event['at'],event['version'],event['subject'],body,digest));inserted+=1
        before={(r['subject'],r['decision']):r['result'] for r in db.execute('SELECT subject,decision,result FROM current_decisions')}
        db.execute('INSERT INTO source_files VALUES(?,?,?)',(sha,subsystem,raw))
        rebuild(db)
        revision=db.execute('SELECT COUNT(*) FROM source_files').fetchone()[0]
        for r in db.execute('SELECT subject,decision,result FROM current_decisions'):
            old=before.get((r['subject'],r['decision']))
            if old!=r['result']:db.execute('INSERT INTO transition_log VALUES(?,?,?,?,?,?,?)',(revision,subsystem,sha,r['subject'],r['decision'],old,r['result']))
        db.execute('RELEASE SAVEPOINT import_events');db.commit()
    except Exception:
        db.execute('ROLLBACK TO SAVEPOINT import_events');db.execute('RELEASE SAVEPOINT import_events');db.rollback();raise
    return {'inserted':inserted,'replayed':replayed,'sha':sha}

def rebuild(db):
    events=[json.loads(r[0]) for r in db.execute('SELECT body FROM events ORDER BY at,version,id')]
    entities,decisions=policy.calculate(events)
    revision=db.execute('SELECT COUNT(*) FROM source_files').fetchone()[0]
    db.execute('DELETE FROM business_entities');db.execute('DELETE FROM current_decisions')
    for item in entities:
        db.execute('INSERT INTO business_entities VALUES(?,?,?,?,?)',item)
    for subject,name,result,reason,evidence in decisions:
        db.execute('INSERT INTO current_decisions VALUES(?,?,?,?,?)',(subject,name,result,reason,canonical(evidence)))
        db.execute('INSERT OR REPLACE INTO decision_history VALUES(?,?,?,?,?)',(revision,subject,name,result,reason))

def report(db):
    return {'entities':[dict(r) for r in db.execute('SELECT * FROM business_entities ORDER BY subsystem,entity_type,entity_id')],
            'decisions':[dict(r) for r in db.execute('SELECT * FROM current_decisions ORDER BY subject,decision')],
            'sources':[dict(r) for r in db.execute('SELECT sha,subsystem FROM source_files ORDER BY sha')],
            'history':[dict(r) for r in db.execute('SELECT * FROM decision_history ORDER BY revision,subject,decision')],
            'transitions':[dict(r) for r in db.execute('SELECT * FROM transition_log ORDER BY revision,subject,decision')]}

def audit(db):
    before=report(db);memory=connect(':memory:')
    import tempfile
    for subsystem,raw in db.execute('SELECT subsystem,raw FROM source_files ORDER BY rowid'):
        with tempfile.NamedTemporaryFile(delete=False) as f:f.write(raw);name=f.name
        try:import_file(memory,subsystem,name)
        finally:Path(name).unlink()
    after=report(memory);memory.close()
    return {'ok':before==after,'source_count':len(before['sources']),'decision_count':len(before['decisions'])}
