import os,sys,json,time,signal,subprocess
from pathlib import Path
import httpx
from dotenv import dotenv_values
ROOT=Path(__file__).resolve().parent.parent
cfg=dotenv_values(ROOT/'.env');url='http://127.0.0.1:8765'
header={'Authorization':'Bearer '+cfg['API_TOKEN']}
text=b'Buyer: Recovery Test\nPurchase Order: RECOVERY-2026\nStyle: QA-REC\nFactory: QA Factory\nDepartment: Shirts\nColor S M\nNavy 10 20\nTotal Quantity 30\n'
start=time.monotonic()
with httpx.Client(base_url=url,headers=header,timeout=20) as c:
    b=c.post('/api/extract?wait=0',files={'file':('recovery-po.txt',text)});b.raise_for_status();bid=b.json()['batch_id']
    deadline=time.monotonic()+60
    while time.monotonic()<deadline:
        w=c.get('/api/ai-status').json()
        if w['queued'] or w['leased']:break
        time.sleep(.3)
    processes=subprocess.check_output(['ps','-eo','pid,args'],text=True).splitlines()
    matches=[int(line.strip().split()[0]) for line in processes if 'uvicorn app.main:app --host 127.0.0.1 --port 8765' in line]
    if len(matches)!=1:raise RuntimeError('Cannot uniquely identify local server; no process killed')
    print('Stopping local server while batch has AI jobs; retained batch',bid,flush=True)
    os.kill(matches[0],signal.SIGKILL)
    deadline=time.monotonic()+300
    last=None
    while time.monotonic()<deadline:
        time.sleep(2)
        try:r=c.get('/api/batches/'+bid,params={'view':'api'});r.raise_for_status();b=r.json()
        except httpx.HTTPError:continue
        if b['status']!=last:print('Recovered status',b['status'],flush=True);last=b['status']
        if b['status']!='processing':break
    assert b['status']=='done',b
    result=b['result']['results'][0];assert result['status']=='done',result
    oid=result['order']['id'];o=c.get('/api/orders/'+str(oid)).json()
    qty=sum(sum(l['sizes'].values()) if l['sizes'] else (l['order_qty'] or 0) for p in o['pos'] for l in p['lines'])
    assert qty==30,o
    assert o['buyer_name']=='Recovery Test',o
    print('Real server SIGKILL recovery verified:',round(time.monotonic()-start,1),'seconds; quantity',qty,flush=True)
    report={'batch_id':bid,'server_killed':True,'elapsed_s':round(time.monotonic()-start,1),'order_id':oid,'readback':o}
    out=ROOT/'data/acceptance/recovery.json';out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(report,indent=2))
    assert c.delete('/api/orders/'+str(oid)).json()['ok']
    assert c.get('/api/orders/'+str(oid)).status_code==404
