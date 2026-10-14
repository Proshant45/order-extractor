import io
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from app import config, db, extract, llm, main

@pytest.fixture
def budget(tmp_path, monkeypatch):
    monkeypatch.setattr(config,'DB_PATH',tmp_path/'quota.db')
    monkeypatch.setattr(config,'MAX_PROCESSING_FILES',20,raising=False)
    db.init()
    from app.maintenance import clear_existing_queue_once
    clear_existing_queue_once()
    return monkeypatch


def test_twenty_files_is_system_wide_and_persistent(budget):
    ids=[db.add_file(f'{i}.txt') for i in range(21)]
    for fid in ids[:20]:db.reserve_file_budget(fid)
    db.init()
    assert db.processing_budget_status()=={'limit':20,'used':20,'remaining':0}
    with pytest.raises(db.ProcessingLimit):db.reserve_file_budget(ids[20])
    db.reserve_file_budget(ids[0])
    assert db.processing_budget_status()['used']==20


def test_concurrent_files_cannot_overrun_twenty(budget):
    ids=[db.add_file(f'{i}.txt') for i in range(25)]
    def reserve(fid):
        try:db.reserve_file_budget(fid);return True
        except db.ProcessingLimit:return False
    with ThreadPoolExecutor(max_workers=5) as pool:results=list(pool.map(reserve,ids))
    assert sum(results)==20 and db.processing_budget_status()['used']==20


def test_retry_reuses_original_file_slot(budget):
    fid=db.add_file('retry.txt',sha='same');db.reserve_file_budget(fid)
    new=db.add_file('retry.txt',sha='same');db.copy_file_budget(fid,new)
    db.reserve_file_budget(new)
    assert db.processing_budget_status()['used']==1


def test_zip_and_single_files_share_processing_budget(budget):
    budget.setattr(config,'llm_provider',lambda:'queue')
    for i in range(19):db.reserve_file_budget(db.add_file(f'prior-{i}.txt'))
    called=[]
    def reader(path,kind,log,ctx):
        extract._need_llm(log,'test document')
        called.append(ctx['name'])
        return {'buyer_name':'Limit test','pos':[{'po_number':'LIMIT','lines':[{'color':'Navy','sizes':{'S':10}}]}]},'text_ai'
    budget.setattr(extract,'extract_file',reader)
    with TestClient(main.app,base_url='https://testserver') as client:
        client.post('/login',data={'username':'tester','password':'pw-123456'})
        archive=io.BytesIO()
        with zipfile.ZipFile(archive,'w') as z:
            z.writestr('one.txt','PO 1');z.writestr('two.txt','PO 2')
        response=client.post('/api/extract',files={'file':('files.zip',archive.getvalue())})
        assert response.status_code==200
        result=response.json()
        assert result['summary']['extracted_ok']==1 and result['summary']['skipped']==1
        blocked=next(r for r in result['results'] if r['status']=='skipped')
        assert '20' in blocked['reason'] and 'processing limit' in blocked['reason'].lower()
        response=client.post('/api/extract',files={'file':('after.txt',b'PO 3')})
        assert response.json()['results'][0]['status']=='skipped'
        assert called==['one.txt']
        assert client.get('/api/processing-limit').json()['remaining']==0


def test_existing_ai_files_count_toward_limit(tmp_path,monkeypatch):
    monkeypatch.setattr(config,'DB_PATH',tmp_path/'existing.db')
    db.init()
    fid=db.add_file('old.txt',status='done');db.update_file(fid,method='text_ai_dual')
    with db.conn() as c:c.execute("DELETE FROM kv WHERE k='file_budget_initialized'")
    db.init()
    assert db.processing_budget_status()['used']==1
