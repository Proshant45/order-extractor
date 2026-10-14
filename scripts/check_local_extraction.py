#!/usr/bin/env python3
import json,sys,time
from pathlib import Path
import httpx
from dotenv import dotenv_values
ROOT=Path(__file__).resolve().parent.parent
cfg=dotenv_values(ROOT/'.env')
headers={'Authorization':'Bearer '+cfg['API_TOKEN']}
source=Path(sys.argv[1]) if len(sys.argv)>1 else ROOT/'tests/fixtures/sample_po.pdf'
start=time.monotonic()
with httpx.Client(base_url='http://127.0.0.1:8765',headers=headers,timeout=120) as c:
    with source.open('rb') as f:
        r=c.post('/api/extract?wait=0'+('&reader=ai' if source.suffix.lower()!='.zip' else ''),files={'file':(source.name,f)});r.raise_for_status();bid=r.json()['batch_id']
    print('Submitted',source.name,'batch',bid,flush=True)
    last=''
    while time.monotonic()-start<1800:
        time.sleep(3)
        b=c.get('/api/batches/'+bid,params={'view':'api'});b.raise_for_status();b=b.json()
        status=c.get('/api/ai-status').json()
        progress=c.get('/api/progress',params={'bid':bid}).json()
        msg=f"{round(time.monotonic()-start)}s batch={b['status']} files={progress.get('finished',0)}/{progress.get('total',0)} jobs={status['queued']}/{status['leased']}"
        if msg!=last: print(msg,flush=True);last=msg
        if b['status']!='processing':break
    else: raise SystemExit('Still processing after 1800s; retained for recovery')
    elapsed=round(time.monotonic()-start,1)
    report={'source':source.name,'batch_id':bid,'elapsed_s':elapsed,'batch':b,'checks':[]}
    if source.name=='sample_po.pdf' and b['status']=='done':
        res=b['result']['results'][0]
        if res.get('order'):
            oid=res['order']['id'];o=c.get(f'/api/orders/{oid}').json()
            sys.path.insert(0,str(ROOT));from tests_expected import checks
            report['checks']=checks(o)
            report['readback']=o
    out=ROOT/'data/acceptance';out.mkdir(parents=True,exist_ok=True)
    path=out/(bid+'.json');path.write_text(json.dumps(report,indent=2))
    results=b.get('result',{}).get('results',[])
    print('Final:',b['status'],'elapsed:',elapsed,'seconds',flush=True)
    print('File outcomes:',[(x['file'],x['status'],x.get('agreement',{}).get('status'),x.get('reason')) for x in results],flush=True)
    print('Ground-truth checks:',sum(x['ok'] for x in report['checks']),'/',len(report['checks']),flush=True)
    print('Report:',path,flush=True)
