"""Recoverable model slips: continuation chunk without PO number, text in price fields, one failed chunk."""
from pathlib import Path
from app import extract, llm, config, guard


def test_text_in_price_fields_becomes_null_with_warning():
    d={'fob':'Landed Warehouse / Ex - Godown','pos':[{'po_number':'P1','stated_total_value':'see annex',
        'lines':[{'color':'Navy','sizes':{'S':1},'order_qty':1,'rate':'FOB Chittagong','net_value':None}]}]}
    notes=guard.soften(d)
    assert d['fob'] is None and d['pos'][0]['stated_total_value'] is None and d['pos'][0]['lines'][0]['rate'] is None
    assert len(notes)==3 and all(n.startswith('warning:') for n in notes)
    assert guard.soften({'fob':'4.90','pos':[]})==[]


def test_missing_po_number_inherits_hint_and_is_not_flagged_as_hallucinated():
    d={'pos':[{'po_number':None,'lines':[{'color':'Red','sizes':{'S':5},'order_qty':5}]}]}
    notes=guard.soften(d,'4500012345')
    assert d['pos'][0]['po_number']=='4500012345' and notes
    assert not [e for e in guard.text_errors(d,'Red S 5') if 'po_number' in e]


def _units(monkeypatch,units):
    monkeypatch.setattr(config,'llm_provider',lambda:'queue')
    monkeypatch.setattr(extract,'_need_llm',lambda *a:None)
    monkeypatch.setattr(extract,'_pdf_units',lambda *a:iter(units))


def test_continuation_chunk_inherits_previous_chunk_po(monkeypatch):
    units=[{'text':'Purchase Order No: 4500012345 Navy S 10','page':'1-4','det':None,'partial':True},
           {'text':'Red S 20','page':'5-6','det':None,'partial':True,'context':''}]
    _units(monkeypatch,units)
    def read(**kw):
        if 'Navy' in kw['text']:
            return {'pos':[{'po_number':'4500012345','lines':[{'color':'Navy','sizes':{'S':10},'order_qty':10}]}]}
        return {'fob':'Landed / Ex-Godown','pos':[{'po_number':None,'lines':[{'color':'Red','sizes':{'S':20},'order_qty':20}]}]}
    monkeypatch.setattr(llm,'read_page',read)
    ctx={};data,_=extract.extract_ai_primary(Path('x.pdf'),'pdf',lambda *a:None,ctx)
    assert [p['po_number'] for p in data['pos']]==['4500012345']
    assert sum(sum(l['sizes'].values()) for l in data['pos'][0]['lines'])==30
    assert data.get('fob') is None and ctx['consensus']['status']=='needs_review'


def test_one_failed_chunk_gives_partial_needs_review(monkeypatch):
    units=[{'text':'Purchase Order No: 4500012345 Navy S 10','page':'1-4','det':None,'partial':True},
           {'text':'garbled','page':'5-6','det':None,'partial':True}]
    _units(monkeypatch,units)
    def read(**kw):
        if 'Navy' in kw['text']:
            return {'pos':[{'po_number':'4500012345','lines':[{'color':'Navy','sizes':{'S':10},'order_qty':10}]}]}
        return {'pos':'not a list'}
    monkeypatch.setattr(llm,'read_page',read)
    ctx={};data,_=extract.extract_ai_primary(Path('x.pdf'),'pdf',lambda *a:None,ctx)
    assert data['pos'][0]['po_number']=='4500012345'
    c=ctx['consensus'];assert c['status']=='needs_review'
