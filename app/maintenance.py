import json
from . import db

MARKER = 'queue_cleanup_2026_10_14_v1'
REASON = 'Cancelled by one-time deployment cleanup'


def clear_existing_queue_once():
    db.init()
    with db.conn() as c:
        if c.execute('SELECT 1 FROM kv WHERE k=?',(MARKER,)).fetchone(): return {'already_ran':True}
    from .documents import restore_retained_batch_sources
    restore_retained_batch_sources()
    with db.conn() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute('SELECT 1 FROM kv WHERE k=?',(MARKER,)).fetchone(): return {'already_ran':True}
        jobs=c.execute("UPDATE ai_jobs SET status='cancelled',stage='cancelled',error=?,text=NULL,image=NULL,lease_id=NULL,lease_until=NULL,updated_at=? WHERE status IN ('queued','leased')",(REASON,db.now())).rowcount
        files=c.execute("UPDATE files SET status='cancelled',reason=? WHERE status IN ('processing','queued_ai')",(REASON,)).rowcount
        batches=c.execute("UPDATE batches SET status='cancelled',result=?,updated_at=? WHERE status='processing'",(json.dumps({'error':REASON}),db.now())).rowcount
        for row in c.execute("SELECT id,result FROM batches WHERE status='done' AND result IS NOT NULL").fetchall():
            payload=json.loads(row['result']);changed=False
            for item in payload.get('items',[]):
                if item.get('status') in ('queued_ai','processing'):
                    item.update(status='cancelled',reason=REASON);changed=True
            if changed:
                summary=payload.get('summary') or {}
                summary.update(queued_ai=0,processing=0,cancelled=sum(i.get('status')=='cancelled' for i in payload['items']))
                payload['summary']=summary
                c.execute('UPDATE batches SET result=?,updated_at=? WHERE id=?',(json.dumps(payload),db.now(),row['id']))
        c.execute('DELETE FROM file_budget')
        c.execute('UPDATE files SET budget_file_id=NULL')
        report={'already_ran':False,'cancelled_jobs':jobs,'cancelled_files':files,'cancelled_batches':batches,'budget_reset':True}
        c.execute('INSERT INTO kv(k,v) VALUES(?,?)',(MARKER,json.dumps(report)))
    return report
