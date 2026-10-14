import json,time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from app import opencode_provider,llm
from app.schema import Order
from tests_expected import checks
import pdfplumber
ROOT=Path(__file__).resolve().parent.parent
with pdfplumber.open(ROOT/'tests/fixtures/sample_po.pdf') as pdf:
    text='\n\f'.join(p.extract_text(layout=True) or '' for p in pdf.pages)
output=ROOT/'data/acceptance';output.mkdir(parents=True,exist_ok=True)

def one(model):
    t=time.monotonic()
    r={'model':model}
    try:
        raw=opencode_provider.run(llm.PROMPT+'\nDOCUMENT TEXT:\n'+text,model=model,timeout=180)
        d=llm._json(raw);o=Order.model_validate(d).model_dump(mode='json')
        r.update(elapsed_s=round(time.monotonic()-t,1),checks=checks(o),data=d)
        print(model,r['elapsed_s'],'seconds',sum(x['ok'] for x in r['checks']),'/',len(r['checks']),flush=True)
    except Exception as e:
        r.update(elapsed_s=round(time.monotonic()-t,1),error=str(e));print(model,'ERROR',str(e)[:160],flush=True)
    (output/(model.replace('/','-')+'-benchmark.json')).write_text(json.dumps(r,indent=2))
    return r
with ThreadPoolExecutor(max_workers=2) as pool:
    list(pool.map(one,['opencode/mimo-v2.6-flash-free','opencode/step-5-preview-free']))
