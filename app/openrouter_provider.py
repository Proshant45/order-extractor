from __future__ import annotations
import base64
import json
import os
import threading
import time
import httpx
from . import config, db

BASE='https://openrouter.ai/api/v1'
DEFAULT_MODEL='google/gemma-4-31b-it:free'
DEFAULT_FALLBACK='dots-studio/dots-3-note-preview:free'
_catalog=None
_catalog_at=0
_catalog_lock=threading.Lock()
_cooldown_until=0


class IncompleteOutput(RuntimeError):
    pass


def client():
    return httpx.Client(timeout=httpx.Timeout(config.LLM_TIMEOUT_S,connect=15))


def catalog():
    global _catalog,_catalog_at
    with _catalog_lock:
        if _catalog is not None and time.time()-_catalog_at<300:return _catalog
        with client() as c:
            response=c.get(BASE+'/models');response.raise_for_status()
        _catalog={m['id']:m for m in response.json()['data']};_catalog_at=time.time()
        return _catalog


def configured_model(image=False):
    return os.getenv('OPENROUTER_VISION_MODEL' if image else 'OPENROUTER_MODEL',DEFAULT_MODEL)


def free_model(mid,image=False):
    if not mid.endswith(':free'):raise ValueError('Only explicit OpenRouter :free models are allowed')
    entry=catalog().get(mid)
    if not entry:raise ValueError('Configured free model is absent from the current OpenRouter catalog')
    prices=entry.get('pricing') or {}
    try:zero='prompt' in prices and 'completion' in prices and all(float(v)==0 for v in prices.values())
    except (ValueError,TypeError):zero=False
    if not zero:raise ValueError('OpenRouter model must have zero pricing for every listed component')
    modalities=entry.get('architecture',{}).get('input_modalities') or []
    if 'text' not in modalities or (image and 'image' not in modalities):
        raise ValueError('Configured free model does not support the required text/page images')
    if int(entry.get('context_length') or 0)<32768:
        raise ValueError('Configured free model has insufficient context for document chunks')
    return entry


def ready_for_retry():
    global _cooldown_until
    with db.conn() as c:
        row=c.execute("SELECT v FROM kv WHERE k='openrouter_retry_after'").fetchone()
    if row:_cooldown_until=max(_cooldown_until,float(row['v']))
    return time.time()>=_cooldown_until


def _defer(message,seconds):
    global _cooldown_until
    from .llm import AIUnavailable
    _cooldown_until=max(_cooldown_until,time.time()+seconds)
    with db.conn() as c:
        c.execute("INSERT INTO kv(k,v) VALUES('openrouter_retry_after',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",(str(_cooldown_until),))
    raise AIUnavailable(message)


def read(prompt,*,text=None,images=(),model=None,_fallback=False):
    from . import llm
    key=os.getenv('OPENROUTER_API_KEY','')
    if not key:raise RuntimeError('Set OPENROUTER_API_KEY on Render')
    if not ready_for_retry():
        raise llm.AIUnavailable('OpenRouter free API is waiting for its retry window; input retained')
    mid=model or configured_model(bool(images))
    try:entry=free_model(mid,bool(images))
    except httpx.TransportError:_defer('OpenRouter model catalog network error; retained for retry',60)
    except httpx.HTTPStatusError as e:
        if e.response.status_code==429 or e.response.status_code>=500:_defer('OpenRouter model catalog unavailable; retained for retry',60)
        raise RuntimeError(f'OpenRouter catalog HTTP {e.response.status_code}') from None
    packed=llm.pack_images(images) if len(images)>1 else (images[0] if images else None)
    fid=getattr(llm.CURRENT,'file_id',None)
    cached=db.completed_read(fid,prompt,mid,text,packed)
    cached_model=mid
    fallback=os.getenv('OPENROUTER_FALLBACK_MODEL',DEFAULT_FALLBACK)
    if cached is None and not _fallback and fid is not None and fallback!=mid:
        candidate=db.completed_read(fid,prompt,fallback,text,packed)
        if candidate is not None:
            try:free_model(fallback,bool(images))
            except ValueError:pass
            else:cached=candidate;cached_model=fallback
    if cached is not None:
        llm.CURRENT.used_model=cached_model
        return llm._json(cached)
    content=[{'type':'text','text':prompt}]
    if text is not None:content.append({'type':'text','text':'DOCUMENT PAGES:\n'+text})
    for png in images:content.append({'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(png).decode()}})
    max_output=min(16384,int(entry.get('top_provider',{}).get('max_completion_tokens') or 16384))
    body={'model':mid,'messages':[{'role':'user','content':content}],'max_tokens':max_output,
          'provider':{'max_price':{'prompt':0,'completion':0},'sort':'latency'}}
    params=entry.get('supported_parameters') or []
    if 'temperature' in params:body['temperature']=0
    if 'reasoning' in params and not entry.get('reasoning',{}).get('mandatory',False):
        body['reasoning']={'enabled':False}
    if 'response_format' in params:body['response_format']={'type':'json_object'}
    job=db.start_api_read(prompt,mid,text,packed,file_id=fid,page=getattr(llm.CURRENT,'page',None),
                          vote=getattr(llm.CURRENT,'vote','A'),owner=getattr(llm.CURRENT,'owner',None))
    error=None
    try:
        with client() as c:
            response=c.post(BASE+'/chat/completions',headers={'Authorization':'Bearer '+key,'X-OpenRouter-Title':'Order Extractor'},json=body)
        try:payload=response.json()
        except ValueError:payload={}
        details=payload.get('error') or {}
        status=response.status_code
        if status==200 and details:
            try:status=int(details.get('code',500))
            except (ValueError,TypeError):status=500
        if status>=500 or status==429:
            provider_busy=status>=500 or bool(details.get('metadata',{}).get('provider_name'))
            if provider_busy and not _fallback and fallback!=mid:
                db.finish_job(job['id'],job['lease_id'],error=f'OpenRouter HTTP {status}; trying another verified free model')
                try:return read(prompt,text=text,images=images,model=fallback,_fallback=True)
                except ValueError:_defer('OpenRouter provider unavailable and free fallback invalid; input retained for retry',60)
        if status in (401,402,429) or status>=500:
            seconds=300 if status in (401,402) else 60
            try:seconds=max(seconds,min(86400,int(response.headers.get('retry-after',seconds))))
            except ValueError:pass
            _defer(f'OpenRouter HTTP {status}: free API unavailable; retained for retry',seconds)
        if status>=400:raise RuntimeError(f'OpenRouter HTTP {status}; free model request failed')
        if payload.get('error'):raise RuntimeError('OpenRouter returned an API error; no extracted data accepted')
        actual=payload.get('model')
        if actual and actual not in (mid,mid.removesuffix(':free')):free_model(actual,bool(images))
        cost=payload.get('usage',{}).get('cost')
        if cost is not None and float(cost)!=0:raise RuntimeError('OpenRouter reported nonzero cost; free-only request rejected')
        choices=payload.get('choices') or []
        if not choices:raise IncompleteOutput('OpenRouter returned no completion')
        choice=choices[0]
        if choice.get('finish_reason') not in ('stop','end_turn'):
            usage=payload.get('usage') or {}
            reasoning=(usage.get('completion_tokens_details') or {}).get('reasoning_tokens','unknown')
            raise IncompleteOutput(f"OpenRouter response truncated or incomplete: finish={choice.get('finish_reason')}, completion_tokens={usage.get('completion_tokens','unknown')}, reasoning_tokens={reasoning}; no partial result accepted")
        result=choice.get('message',{}).get('content') or ''
        if not isinstance(result,str) or not result.strip():raise IncompleteOutput('OpenRouter returned no usable text')
        try:data=llm._json(result)
        except ValueError:raise IncompleteOutput('OpenRouter returned invalid JSON; no partial result accepted') from None
        db.update_job_progress(job['id'],job['lease_id'],'output_received')
        ack=db.finish_job(job['id'],job['lease_id'],result=result)
        if ack!='ok':
            db.cancel_job(job['id'])
            _defer('OpenRouter result was not persisted; input retained for retry',60)
        llm.CURRENT.used_model=mid
        return data
    except IncompleteOutput as e:
        error=str(e)
        ack=db.finish_job(job['id'],job['lease_id'],error=error)
        if ack=='stale_lease':db.cancel_job(job['id'])
        if not _fallback and fallback!=mid:
            try:return read(prompt,text=text,images=images,model=fallback,_fallback=True)
            except ValueError:pass
        raise
    except httpx.TransportError:
        error='OpenRouter network/timeout error; retained for retry'
        _defer(error,60)
    except Exception as e:
        error=str(e).replace(key,'[redacted]')[:500]
        raise
    finally:
        if error:db.finish_job(job['id'],job['lease_id'],error=error)
