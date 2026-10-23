from collections import Counter
from pathlib import Path
import pytest
from app import config, consensus, db, extract, guard, llm


def order(no, color, qty, total=None):
    return {'pos':[{'po_number':no,'stated_total_qty':total if total is not None else qty,
                    'stated_total_value':(total if total is not None else qty)*2,
                    'lines':[{'color':color,'sizes':{'S':qty},'order_qty':qty,'rate':2,'net_value':qty*2}]}]}


def test_page_groups_split_at_new_po_and_cover_continuations(monkeypatch):
    texts=['Purchase Order Number: 10001']+['continuation']*5+['Purchase Order Number: 10002']+['continuation']*3
    groups=extract._bounded_groups(texts,extract._po_groups(texts))
    assert [(lo,hi) for lo,hi,_ in groups]==[(0,4),(4,6),(6,10)]
    assert [p for lo,hi,_ in groups for p in range(lo,hi)]==list(range(10))


def test_dense_pages_respect_worker_text_limit():
    texts=['row '*2000]*5
    groups=extract._bounded_groups(texts,[(0,5,1)])
    assert [(lo,hi) for lo,hi,_ in groups]==[(0,3),(3,5)]
    assert all(len('\n\f'.join(texts[lo:hi]))<=30000 for lo,hi,_ in groups)


def test_long_spreadsheet_text_is_split_without_lost_rows():
    text='Style A\tSize S\tQty 15\n'*3000
    chunks=list(extract._text_chunks(text))
    assert len(chunks)>1 and all(len(chunk)<=30000 for chunk in chunks)
    assert ''.join(chunks)==text


def test_only_invalid_chunk_is_read_again(monkeypatch):
    monkeypatch.setattr(config,'llm_provider',lambda:'queue')
    monkeypatch.setattr(extract,'_need_llm',lambda *a:None)
    units=[{'text':'PO Number: 10001 Navy S 10 Total 10 Value 20','page':'1-4','det':None},
           {'text':'PO Number: 10002 Red S 20 Total 20 Value 40','page':'5-8','det':None}]
    monkeypatch.setattr(extract,'_pdf_units',lambda *a:iter(units))
    calls=Counter();models=[]
    def read(**kw):
        calls[kw['text']]+=1;models.append(kw['model'])
        if '10001' in kw['text']:return order('10001','Navy',10)
        return order('10002','Red',19 if calls[kw['text']]==1 else 20,total=20)
    monkeypatch.setattr(llm,'read_page',read)
    ctx={};data,_=extract.extract_ai_primary(Path('input.pdf'),'pdf',lambda *a:None,ctx)
    assert [calls[u['text']] for u in units]==[1,2]
    assert all(m=='gpt-6-luna' for m in models)
    assert ctx['consensus']['status']=='checked'
    assert sum(p['stated_total_qty'] for p in data['pos'])==30


def test_continuation_chunks_check_full_po_totals_only_after_merge(monkeypatch):
    monkeypatch.setattr(config,'llm_provider',lambda:'queue')
    monkeypatch.setattr(extract,'_need_llm',lambda *a:None)
    units=[{'text':'PO Number: 10003 Navy 20 Total 30 Value 60','page':'1-4','det':None,'partial':True},
           {'text':'PO Number: 10003 Red 10 Total 30 Value 60','page':'5-6','det':None,'partial':True}]
    monkeypatch.setattr(extract,'_pdf_units',lambda *a:iter(units))
    calls=[]
    def read(**kw):
        calls.append(kw['text'])
        return order('10003','Navy' if 'Navy' in kw['text'] else 'Red',20 if 'Navy' in kw['text'] else 10,total=30)
    monkeypatch.setattr(llm,'read_page',read)
    ctx={};data,_=extract.extract_ai_primary(Path('input.pdf'),'pdf',lambda *a:None,ctx)
    assert len(calls)==2 and ctx['consensus']['status']=='checked'
    assert guard.arithmetic_errors(data)==[]


def test_missing_printed_po_is_a_visible_check_failure():
    d=order('10001','Navy',10)
    errors=guard.coverage_errors(d,'Purchase Order Number: 10001\nPurchase Order Number: 10002')
    assert errors and '10002' in errors[0]


def test_po_total_value_is_checked_without_rate():
    d={'pos':[{'po_number':'10001','stated_total_value':100,'lines':[{'color':'Navy','sizes':{'S':10},'net_value':20}]}]}
    assert any('stated_total_value' in e for e in guard.arithmetic_errors(d))


def test_completed_chunk_is_reused_after_file_retry(tmp_path,monkeypatch):
    monkeypatch.setenv('AI_JOB_WAIT_S','0')
    monkeypatch.setattr(config,'DB_PATH',tmp_path/'chunks.db');db.init()
    fid=db.add_file('source.pdf');llm.CURRENT.file_id=fid
    jid=db.enqueue_job('prompt','model','chunk text',None,file_id=fid)
    job=db.claim_job('worker');assert db.finish_job(jid,job['lease_id'],result='{"pos":[]}')=='ok'
    retry=db.add_file('source.pdf');db.copy_file_budget(fid,retry);llm.CURRENT.file_id=retry
    assert llm.queue_read('prompt',model='model',text='chunk text')=='{"pos":[]}'
    with db.conn() as c:assert c.execute('SELECT COUNT(*) FROM ai_jobs').fetchone()[0]==1


def test_scanned_pdf_reads_four_pages_together(tmp_path):
    import pymupdf
    source=tmp_path/'scanned.pdf'
    doc=pymupdf.open()
    for i in range(5):
        page=doc.new_page(width=100,height=100)
        page.draw_rect(pymupdf.Rect(10,10,90,90),color=(0,0,0))
    doc.save(source);doc.close()
    units=list(extract._pdf_units(source,lambda *a:None,{}))
    assert [u['page'] for u in units]==['1-4','5-5']
    assert [len(u.get('images') or [u['image']]) for u in units]==[4,1]


def test_multi_image_queue_preserves_every_page(tmp_path,monkeypatch):
    monkeypatch.setattr(config,'DB_PATH',tmp_path/'images.db');db.init()
    fid=db.add_file('scan.pdf')
    payload=llm.pack_images([b'page1',b'page2',b'page3'])
    jid=db.enqueue_job('prompt','model',None,payload,file_id=fid)
    assert db.claim_job('old-worker',image_batches=False) is None
    job=db.claim_job('new-worker',image_batches=True)
    assert job['id']==jid and llm.unpack_images(job['image'])==[b'page1',b'page2',b'page3']
