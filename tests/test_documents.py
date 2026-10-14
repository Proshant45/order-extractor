from fastapi.testclient import TestClient
from app import config, db, main
import pytest

@pytest.fixture
def documents_client(tmp_path,monkeypatch):
    monkeypatch.setattr(config,'DATA_DIR',tmp_path)
    monkeypatch.setattr(config,'DB_PATH',tmp_path/'docs.db')
    monkeypatch.setattr(config,'TMP_DIR',tmp_path/'tmp');config.TMP_DIR.mkdir()
    with TestClient(main.app,base_url='https://testserver') as c:
        assert c.post('/login',data={'username':'tester','password':'pw-123456'}).status_code==200
        yield c


def test_each_uploaded_document_can_be_opened_and_downloaded(documents_client):
    c=documents_client
    content=b'Buyer:,Document Test\nPO Number:,DOC-1\nColor,S,M\nNavy,10,20\n'
    first=c.post('/api/upload',files={'file':('doc.csv',content)}).json()['items'][0]
    fid=first['file_id'];oid=first['order_id']
    assert c.get(f'/api/files/{fid}/source').content==content
    assert 'attachment' in c.get(f'/api/files/{fid}/source?download=1').headers['content-disposition']
    page=c.get(f'/files/{fid}');assert page.status_code==200 and 'Navy' in page.text and f'/orders/{oid}' in page.text
    cached=c.post('/api/upload',files={'file':('duplicate.csv',content)}).json()['items'][0]
    assert cached['cached']
    row=next(x for x in c.get('/api/files').json() if x['id']==cached['file_id'])
    assert row['order_id']==oid and row['source_available'] is True
    assert c.get(f"/api/files/{cached['file_id']}/source").content==content
    assert f"/files/{cached['file_id']}" in c.get('/').text


def test_supporting_documents_have_view_action_without_fake_order(documents_client):
    c=documents_client
    item=c.post('/api/upload',files={'file':('tech-pack.txt',b'Tech Pack\nConstruction details for jacket\n')} ).json()['items'][0]
    assert item['status']=='classified'
    fid=item['file_id'];page=c.get(f'/files/{fid}')
    assert page.status_code==200 and 'Supporting document' in page.text
    assert 'Construction details' in page.text
    row=c.get(f'/api/files/{fid}').json()
    assert row['order_id'] is None and row['source_available']


def test_missing_source_is_explicit_and_logs_are_reachable(documents_client):
    fid=db.add_file('old.pdf',kind='pdf',status='failed',reason='Old extraction failed')
    db.log(fid,'Reader timed out','error')
    c=documents_client
    assert c.get(f'/api/files/{fid}/source').status_code==404
    page=c.get(f'/files/{fid}')
    assert page.status_code==200 and 'Original document unavailable' in page.text and 'Reader timed out' in page.text


def test_library_does_not_hide_files_after_fifty(documents_client):
    for n in range(58):db.add_file(f'document-{n}.txt',kind='text',status='classified')
    assert len(documents_client.get('/api/files').json())==58


def test_document_shows_actual_saved_data_and_export(documents_client):
    c=documents_client
    content=b'Buyer:,Document Test\nPO Number:,DOC-1\nColor,S,M\nNavy,10,20\n'
    item=c.post('/api/upload',files={'file':('doc.csv',content)}).json()['items'][0]
    order=c.get(f"/api/orders/{item['order_id']}").json()
    order['buyer_name']='Edited buyer';order['pos'][0]['lines'][0]['sizes']={'S':7,'M':20}
    assert c.put(f"/api/orders/{item['order_id']}",json=order).json()['ok']
    data=c.get(f"/api/files/{item['file_id']}/data")
    assert data.status_code==200
    assert data.json()['order']['buyer_name']=='Edited buyer'
    assert data.json()['order']['pos'][0]['lines'][0]['sizes']['S']==7
    page=c.get(f"/files/{item['file_id']}").text
    assert 'Stored data' in page and 'Edited buyer' in page and 'DOC-1' in page


def test_retained_zip_registers_every_file_even_without_extraction(documents_client,tmp_path):
    import zipfile
    from app import documents
    path=tmp_path/'retained.zip'
    with zipfile.ZipFile(path,'w') as archive:
        for n in range(58):archive.writestr(f'GEAL/input-{n}.txt',f'Unprocessed source {n}')
    db.save_batch('old-zip','processing','retained.zip',input_path=path)
    documents.restore_retained_batch_sources()
    rows=documents_client.get('/api/files').json()
    entries=[row for row in rows if row['kind']!='zip']
    assert len(entries)==58 and all(row['source_available'] for row in entries)
    assert all(row['status']=='pending' and row['order_id'] is None for row in entries)
    documents.restore_retained_batch_sources()
    assert len(documents_client.get('/api/files').json())==59


def test_supporting_data_is_extracted_editable_and_saved(documents_client,monkeypatch):
    from app import llm
    monkeypatch.setattr(config,'llm_provider',lambda:'queue')
    calls=[]
    def read(**kwargs):
        calls.append(kwargs)
        return {'document_type':'tech_pack','summary':'Jacket specifications','fields':[{'name':'Style','value':'JACKET-1'}],
                'tables':[{'name':'Measurements','columns':['Size','Chest'], 'rows':[['S',92],['M',96]]}], 'warnings':[]}
    monkeypatch.setattr(llm,'read_page',read)
    c=documents_client
    item=c.post('/api/upload',files={'file':('tech-pack.txt',b'Tech Pack\nStyle JACKET-1\nChest S 92 M 96')} ).json()['items'][0]
    fid=item['file_id'];data=c.get(f'/api/files/{fid}/data').json()
    assert calls and data['metadata']['document']['tables'][0]['rows'][0]==['S',92]
    document=data['metadata']['document'];document['fields'][0]['value']='JACKET-EDITED';document['tables'][0]['rows'][0][1]=94
    response=c.put(f'/api/files/{fid}/data',json=document)
    assert response.status_code==200 and response.json()['ok'] is True
    saved=c.get(f'/api/files/{fid}/data').json()['metadata']
    assert saved['document']['fields'][0]['value']=='JACKET-EDITED'
    assert saved['document']['tables'][0]['rows'][0][1]==94
    assert saved['original_document']['tables'][0]['rows'][0][1]==92
    page=c.get(f'/files/{fid}').text
    assert 'Measurements' in page and 'Save document data' in page and 'JACKET-EDITED' in page
    assert db.processing_budget_status()['used']==1
    cached=c.post('/api/upload',files={'file':('duplicate-tech-pack.txt',b'Tech Pack\nStyle JACKET-1\nChest S 92 M 96')}).json()['items'][0]
    assert cached.get('cached') is not True


def test_invalid_document_edit_cannot_overwrite_saved_data(documents_client):
    fid=db.add_file('bom.txt',kind='text',status='classified')
    c=documents_client
    valid={'document_type':'bom','summary':'Materials','fields':[], 'tables':[{'name':'Materials','columns':['Fabric','Usage'],'rows':[['Cotton',1.5]]}],'warnings':[]}
    assert c.put(f'/api/files/{fid}/data',json=valid).status_code==200
    bad={**valid,'tables':[{'name':'Materials','columns':['Fabric'],'rows':[['Cotton',1.5]]}]}
    assert c.put(f'/api/files/{fid}/data',json=bad).status_code==422
    assert c.get(f'/api/files/{fid}/data').json()['metadata']['document']==valid


@pytest.mark.parametrize('columns',[['Fabric','Usage'],2,'Fabric',{'Fabric':1}])
def test_malformed_ai_table_is_re_read_before_any_document_is_saved(documents_client,monkeypatch,columns):
    from app import llm
    monkeypatch.setattr(config,'llm_provider',lambda:'openrouter')
    responses=iter([
        {'document_type':'bom','fields':[],'tables':[{'name':'Materials','columns':columns,'rows':[['Cotton',1.5,'shifted']]}]},
        {'document_type':'bom','fields':[],'tables':[{'name':'Materials','columns':['Fabric','Usage'],'rows':[['Cotton',1.5],['Linen',2.5]]}]},
    ])
    monkeypatch.setattr(llm,'read_page',lambda **kwargs:next(responses))
    item=documents_client.post('/api/upload',files={'file':('bom.txt',b'BILL OF MATERIAL\nCotton 1.5\nLinen 2.5')}).json()['items'][0]
    assert item['status']=='classified',item
    data=documents_client.get(f"/api/files/{item['file_id']}/data").json()['metadata']['document']
    assert data['tables'][0]['rows']==[['Cotton',1.5],['Linen',2.5]]
    assert any('re-read' in entry['message'] for entry in db.file_logs(item['file_id']))


@pytest.mark.parametrize('long_cell,late_fallback',[('Cotton\norganic',False),('Cotton\norganic'+'x'*2000,False),('Cotton\norganic'+'x'*2000,True)])
def test_spreadsheet_bom_keeps_source_cells_when_ai_table_columns_shift(documents_client,monkeypatch,tmp_path,long_cell,late_fallback):
    import openpyxl
    from app import llm
    monkeypatch.setattr(config,'llm_provider',lambda:'openrouter')
    monkeypatch.setattr(config,'AI_CHUNK_MAX_CHARS',1000)
    book=openpyxl.Workbook();sheet=book.active;sheet.title='Materials'
    sheet.append(['BILL OF MATERIAL','Style','SHIRT-1'])
    sheet.append(['Item','Colour','Consumption']);sheet.append(['Cotton',None,1.5]);sheet.append(['Linen','Blue',2.5])
    sheet.append([long_cell,None,1.5]);sheet.append(['Trim\tbutton','Blue',2.5])
    path=tmp_path/'bom.xlsx';book.save(path)
    reads=[]
    def malformed(**kwargs):
        reads.append(kwargs)
        if late_fallback and len(reads)==1:
            return {'document_type':'bom','fields':[{'name':'Style','value':'SHIRT-1'}],
                    'tables':[{'name':'Materials','columns':['Item','Colour','Consumption'],'rows':[['Cotton',None,1.5]]}]}
        return {'document_type':'bom','fields':[{'name':'Style','value':'SHIRT-1'}],
                'tables':[{'name':'Materials','columns':['Item','Colour','Consumption'],'rows':[['Cotton',1.5],['Linen','Blue',2.5]]}]}
    monkeypatch.setattr(llm,'read_page',malformed)
    item=documents_client.post('/api/upload',files={'file':('bom.xlsx',path.read_bytes())}).json()['items'][0]
    assert item['status']=='classified',item
    document=documents_client.get(f"/api/files/{item['file_id']}/data").json()['metadata']['document']
    assert any([3,'Cotton',None,1.5] in table['rows'] for table in document['tables'])
    assert any([4,'Linen','Blue',2.5] in table['rows'] for table in document['tables'])
    assert any([5,long_cell,None,1.5] in table['rows'] for table in document['tables'])
    assert any([6,'Trim\tbutton','Blue',2.5] in table['rows'] for table in document['tables'])
    assert len(document['tables'])==1
    assert any('source cells' in warning for warning in document['warnings'])
