from app import config, db
from app.schema import Order


def test_deployment_cleanup_runs_once_and_preserves_orders(tmp_path,monkeypatch):
    from app.maintenance import clear_existing_queue_once
    monkeypatch.setattr(config,'DB_PATH',tmp_path/'cleanup.db');db.init()
    fid=db.add_file('old.txt',status='queued_ai')
    job=db.enqueue_job('prompt','model','text',None,owner='tester',file_id=fid)
    db.claim_job('box','tester')
    oid=db.save_order(Order(buyer_name='Keep this order'),[])
    db.save_batch('old-batch','processing','old.zip')
    report=clear_existing_queue_once()
    assert report['cancelled_jobs']==1 and report['cancelled_files']==1 and report['cancelled_batches']==1
    assert db.get_job(job)['status']=='cancelled' and db.get_batch('old-batch')['status']=='cancelled'
    assert db.get_order(oid)['buyer_name']=='Keep this order'
    assert db.processing_budget_status()['used']==0
    fresh=db.add_file('fresh.txt');new_job=db.enqueue_job('p','m','t',None,owner='tester',file_id=fresh)
    db.save_batch('new-batch','processing','new.txt')
    report=clear_existing_queue_once()
    assert report['already_ran'] is True
    assert db.get_job(new_job)['status']=='queued' and db.get_batch('new-batch')['status']=='processing'
    assert db.processing_budget_status()['used']==1


def test_cleanup_reconciles_deferred_batch_result(tmp_path,monkeypatch):
    from app.maintenance import clear_existing_queue_once
    monkeypatch.setattr(config,'DB_PATH',tmp_path/'deferred.db');db.init()
    fid=db.add_file('offline.txt',status='queued_ai')
    db.save_batch('deferred','done','offline.txt',{'items':[{'file_id':fid,'name':'offline.txt','status':'queued_ai'}],
                                            'summary':{'queued_ai':1,'processing':0,'extracted_ok':0}})
    clear_existing_queue_once()
    batch=db.get_batch('deferred')
    assert batch['result']['items'][0]['status']=='cancelled'
    assert batch['result']['summary']['queued_ai']==0 and batch['result']['summary']['cancelled']==1
    assert not db.queued_files()
