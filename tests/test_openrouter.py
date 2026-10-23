import json
import pytest
import httpx
from app import config, db, llm


FREE_TEXT='test/text:free'
FREE_VISION='test/vision:free'


def model(mid,images=False,price='0'):
    return {'id':mid,'pricing':{'prompt':price,'completion':'0'},'context_length':131072,
            'architecture':{'input_modalities':['text','image'] if images else ['text'],'output_modalities':['text']},
            'top_provider':{'max_completion_tokens':16384},'supported_parameters':['response_format','temperature']}


@pytest.fixture
def api(monkeypatch,tmp_path):
    from app import openrouter_provider as provider
    monkeypatch.setenv('OPENROUTER_API_KEY','private-test-key')
    monkeypatch.setenv('OPENROUTER_MODEL',FREE_TEXT)
    monkeypatch.setenv('OPENROUTER_VISION_MODEL',FREE_VISION)
    monkeypatch.setattr(config,'DB_PATH',tmp_path/'api.db');db.init()
    monkeypatch.setattr(llm.CURRENT,'file_id',None,raising=False)
    monkeypatch.setattr(provider,'_catalog',None)
    monkeypatch.setattr(provider,'_cooldown_until',0)
    requests=[]
    responses=[]
    def route(request):
        requests.append(request)
        if request.url.path.endswith('/models'):
            return httpx.Response(200,json={'data':[model(FREE_TEXT),model(FREE_VISION,True),model('test/paid',price='1')]})
        mid=json.loads(request.content)['model']
        return responses.pop(0) if responses else httpx.Response(200,json={'model':mid,'usage':{'cost':0},'choices':[{'finish_reason':'stop','message':{'content':'{"pos":[]}'}}]})
    factory=httpx.Client
    monkeypatch.setattr(provider,'client',lambda:factory(transport=httpx.MockTransport(route)))
    return provider,requests,responses


def test_openrouter_key_overrides_old_worker_settings(monkeypatch):
    monkeypatch.setenv('OPENROUTER_API_KEY','private-test-key')
    monkeypatch.setenv('AI_QUEUE','1');monkeypatch.setenv('WORKER_TOKEN','old-worker-token')
    monkeypatch.setenv('EXTRACTOR','codex')
    assert config.llm_provider()=='openrouter'


def test_paid_model_is_rejected_without_inference(api):
    provider,requests,_=api
    with pytest.raises(ValueError,match='free'):
        provider.read('prompt',text='document',model='test/paid')
    assert not any(r.url.path.endswith('/chat/completions') for r in requests)


def test_nonzero_priced_free_variant_is_rejected(api,monkeypatch):
    provider,requests,_=api
    monkeypatch.setattr(provider,'catalog',lambda:{FREE_TEXT:model(FREE_TEXT,price='0.1')})
    with pytest.raises(ValueError,match='zero'):
        provider.read('prompt',text='document',model=FREE_TEXT)
    assert not requests


def test_text_request_uses_only_free_model_and_direct_progress(api):
    provider,requests,_=api
    fid=db.add_file('input.txt');llm.CURRENT.file_id=fid;llm.CURRENT.page='1-4';llm.CURRENT.vote='A'
    assert llm.read_page(text='PO source')=={'pos':[]}
    body=json.loads(next(r.content for r in requests if r.url.path.endswith('/chat/completions')))
    assert body['model']==FREE_TEXT and body['provider']['max_price']=={'prompt':0,'completion':0}
    assert body['response_format']=={'type':'json_object'}
    assert 'tools' not in body and 'models' not in body
    progress=db.file_job_progress([fid])[fid]
    assert progress['done']==1 and progress['jobs'][0]['worker']=='OpenRouter'
    assert db.processing_budget_status()['used']==1
    assert db.claim_job('old-box') is None
    assert llm.read_page(text='PO source')=={'pos':[]}
    assert sum(r.url.path.endswith('/chat/completions') for r in requests)==1


def test_vision_request_preserves_all_page_images(api):
    provider,requests,_=api
    provider.read('prompt',images=[b'one',b'two',b'three',b'four'])
    body=json.loads(next(r.content for r in requests if r.url.path.endswith('/chat/completions')))
    assert body['model']==FREE_VISION
    content=body['messages'][-1]['content']
    assert len([part for part in content if part['type']=='image_url'])==4


def test_rate_limit_defers_without_paid_fallback(api):
    provider,requests,responses=api
    responses.append(httpx.Response(429,json={'error':{'message':'Free daily limit'}},headers={'Retry-After':'120'}))
    with pytest.raises(llm.AIUnavailable,match='429'):
        provider.read('prompt',text='document')
    assert not provider.ready_for_retry()
    assert sum(r.url.path.endswith('/chat/completions') for r in requests)==1


def test_truncated_output_is_never_accepted(api):
    provider,_,responses=api
    responses.append(httpx.Response(200,json={'usage':{'cost':0},'choices':[{'finish_reason':'length','message':{'content':'{"pos":[]}'}}]}))
    with pytest.raises(RuntimeError,match='truncated'):
        provider.read('prompt',text='document')


def test_optional_thinking_cannot_consume_the_json_response(api,monkeypatch):
    provider,requests,_=api
    entry=model(FREE_TEXT);entry['supported_parameters'].append('reasoning');entry['reasoning']={'mandatory':False}
    monkeypatch.setattr(provider,'catalog',lambda:{FREE_TEXT:entry})
    factory=httpx.Client
    def route(request):
        requests.append(request)
        body=json.loads(request.content)
        disabled=body.get('reasoning',{}).get('enabled') is False
        return httpx.Response(200,json={'model':FREE_TEXT,'usage':{'cost':0},'choices':[{'finish_reason':'stop' if disabled else 'length','message':{'content':'{"pos":[]}' if disabled else ''}}]})
    monkeypatch.setattr(provider,'client',lambda:factory(transport=httpx.MockTransport(route)))
    assert provider.read('prompt',text='document')=={'pos':[]}


def test_truncated_read_retries_a_verified_free_model_without_saving_partial(api,monkeypatch):
    provider,requests,responses=api
    monkeypatch.setenv('OPENROUTER_FALLBACK_MODEL',FREE_VISION)
    llm.CURRENT.file_id=db.add_file('output.txt')
    responses.append(httpx.Response(200,json={'usage':{'cost':0,'completion_tokens':16384,'completion_tokens_details':{'reasoning_tokens':16380}},'choices':[{'finish_reason':'length','message':{'content':'{"pos":['}}]}))
    assert provider.read('prompt',text='document')=={'pos':[]}
    jobs=db.file_job_progress([llm.CURRENT.file_id])[llm.CURRENT.file_id]
    assert jobs['done']==1 and jobs['failed']==1
    assert 'reasoning_tokens=16380' in jobs['jobs'][0]['error']
    assert llm.CURRENT.used_model==FREE_VISION


def test_exhausted_guard_reports_underlying_reader_error():
    from app import guard
    def failed(extra):raise RuntimeError('OpenRouter truncated: finish=length')
    with pytest.raises(RuntimeError,match='finish=length'):
        guard.ai_extract_with_guard(failed,'source',lambda *a:None,max_repairs=1)


def test_unavailable_primary_falls_back_only_to_verified_free_model(api,monkeypatch):
    provider,requests,responses=api
    monkeypatch.setenv('OPENROUTER_FALLBACK_MODEL',FREE_VISION)
    responses.append(httpx.Response(503,json={'error':{'message':'Provider busy'}}))
    assert provider.read('prompt',text='document')=={'pos':[]}
    models=[json.loads(r.content)['model'] for r in requests if r.url.path.endswith('/chat/completions')]
    assert models==[FREE_TEXT,FREE_VISION]


def test_catalog_outage_preserves_retry_instead_of_failing_file(api,monkeypatch):
    provider,_,_=api
    def failed():raise httpx.ConnectError('catalog unreachable')
    monkeypatch.setattr(provider,'catalog',failed)
    with pytest.raises(llm.AIUnavailable,match='catalog'):
        provider.read('prompt',text='document')
    assert not provider.ready_for_retry()


@pytest.mark.parametrize('code',[429,502,504])
def test_embedded_api_outage_retains_input_for_retry(api,monkeypatch,code):
    provider,_,responses=api
    monkeypatch.setenv('OPENROUTER_FALLBACK_MODEL',FREE_TEXT)
    monkeypatch.setattr(provider,'_sleep',lambda s:None)   # 429 is retried with backoff first; a persistent outage still defers
    responses.extend(httpx.Response(200,json={'error':{'code':code,'message':'Unavailable'}}) for _ in range(3))
    with pytest.raises(llm.AIUnavailable):provider.read('prompt',text='document')
    assert not provider.ready_for_retry()


def test_expired_result_acknowledgment_cannot_become_success(api,monkeypatch):
    provider,_,_=api
    original=db.finish_job
    def expired(jid,lease,**kwargs):
        with db.conn() as c:c.execute('UPDATE ai_jobs SET lease_until=0 WHERE id=?',(jid,))
        return original(jid,lease,**kwargs)
    monkeypatch.setattr(db,'finish_job',expired)
    with pytest.raises(llm.AIUnavailable,match='persisted'):provider.read('prompt',text='document')
    with db.conn() as c:
        row=c.execute('SELECT status,result FROM ai_jobs').fetchone()
    assert row['status']=='cancelled' and row['result'] is None


def test_unavailable_fallback_does_not_turn_outage_into_permanent_failure(api,monkeypatch):
    provider,requests,responses=api
    monkeypatch.setenv('OPENROUTER_FALLBACK_MODEL','test/absent:free')
    responses.append(httpx.Response(502,json={'error':{'message':'busy'}}))
    with pytest.raises(llm.AIUnavailable):provider.read('prompt',text='document')
    assert not provider.ready_for_retry()
    assert sum(r.url.path.endswith('/chat/completions') for r in requests)==1


def test_completed_free_fallback_reused_on_same_logical_read(api,monkeypatch):
    provider,requests,responses=api
    monkeypatch.setenv('OPENROUTER_FALLBACK_MODEL',FREE_VISION)
    llm.CURRENT.file_id=db.add_file('fallback.txt')
    responses.append(httpx.Response(502,json={'error':{'message':'busy'}}))
    assert provider.read('prompt',text='document')=={'pos':[]}
    assert provider.read('prompt',text='document')=={'pos':[]}
    assert sum(r.url.path.endswith('/chat/completions') for r in requests)==2
    assert llm.CURRENT.used_model==FREE_VISION


def test_deploy_retries_only_old_truncation_failures_and_reuses_budget(api,monkeypatch,tmp_path):
    from app import main,documents
    monkeypatch.setattr(config,'DATA_DIR',tmp_path)
    source=tmp_path/'bom.xls';source.write_bytes(b'original retained file')
    fid=db.add_file('bom.xls',kind='xls',status='failed',reason='RuntimeError: OpenRouter response truncated or incomplete; no partial result accepted')
    documents.attach_source(fid,source,'original-hash')
    job=db.start_api_read('p',FREE_TEXT,'text',None,file_id=fid)
    db.finish_job(job['id'],job['lease_id'],error='truncated')
    unrelated=db.add_file('bad.csv',status='failed',reason='validation: negative quantity')
    missing=db.add_file('lost.xls',status='failed',reason='RuntimeError: OpenRouter response truncated or incomplete; no partial result accepted')
    assert main._recover_openrouter_truncations()==1
    assert db.get_file(fid)['status']=='queued_ai'
    assert (tmp_path/'queue'/f'{fid}.xls').read_bytes()==source.read_bytes()
    assert db.get_file(unrelated)['status']=='failed' and db.get_file(missing)['status']=='failed'
    assert main._recover_openrouter_truncations()==0
    assert db.processing_budget_status()['used']==1


def test_short_429_backs_off_and_retries_primary_before_fallback(api,monkeypatch):
    provider,requests,responses=api
    monkeypatch.setenv('OPENROUTER_FALLBACK_MODEL',FREE_VISION)
    waits=[];monkeypatch.setattr(provider,'_sleep',waits.append)
    busy={'error':{'message':'rate limited upstream','metadata':{'provider_name':'X'}}}
    responses.extend([httpx.Response(429,json=busy),httpx.Response(429,json=busy)])
    assert provider.read('prompt',text='document')=={'pos':[]}
    models=[json.loads(r.content)['model'] for r in requests if r.url.path.endswith('/chat/completions')]
    assert models==[FREE_TEXT]*3 and waits==[4,8]


def test_429_falls_back_after_backoff_retries_are_used(api,monkeypatch):
    provider,requests,responses=api
    monkeypatch.setenv('OPENROUTER_FALLBACK_MODEL',FREE_VISION);monkeypatch.setenv('OPENROUTER_429_RETRIES','1')
    monkeypatch.setattr(provider,'_sleep',lambda s:None)
    busy={'error':{'message':'rate limited upstream','metadata':{'provider_name':'X'}}}
    responses.extend([httpx.Response(429,json=busy),httpx.Response(429,json=busy)])
    provider.read('prompt',text='document')
    models=[json.loads(r.content)['model'] for r in requests if r.url.path.endswith('/chat/completions')]
    assert models==[FREE_TEXT,FREE_TEXT,FREE_VISION]
