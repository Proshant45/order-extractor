'use strict';
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let activeBatch = null, uploading = false, stream = null;
try { activeBatch = localStorage.getItem('oe.activeBatch'); } catch {}
async function jsonRequest(url, options = {}) {
  const response = await fetch(url, {...options, cache:'no-store'});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail || `HTTP ${response.status}`));
  return data;
}
async function refreshOrders() {
  const rows = await jsonRequest('/api/orders' + window.location.search);
  const num = n => Number(n || 0).toLocaleString();
  document.querySelector('#orders-table tbody').innerHTML = rows.map(o => `<tr><td>${o.id}</td><td>${esc(o.buyer_name)}</td><td>${esc(o.style_number)}</td><td>${esc(o.po_numbers)}</td><td class="num">${num(o.qty)}</td><td class="num">${o.value == null ? '' : esc(Number(o.value).toFixed(2))+' '+esc(o.currency)}</td><td>${esc(o.file_name || 'manual')}</td><td>${esc((o.updated_at || '').slice(0,16).replace('T',' '))}</td><td><span class="pill ${o.status === 'saved' ? 'saved':'draft'}">${esc(o.status)}</span></td><td class="act"><a class="btn blue sm" href="/orders/${o.id}">Edit</a> <button class="btn ghost sm" onclick="delOrder(${o.id},this)">Delete</button></td></tr>`).join('') || '<tr><td colspan="10">No matching orders.</td></tr>';
}
async function refreshDocuments() {
  const files=await jsonRequest('/api/files');
  const target=document.querySelector('#documents-table tbody');if(!target)return;
  target.innerHTML=files.map(file=>{
    const statuses=new Set(['done','cached','classified','pending','processing','queued_ai','failed','skipped','cancelled','retried','edited']);
    const cls=statuses.has(file.status)?file.status:'';
    return `<tr><td class="document-name"><a href="/files/${file.id}" title="${esc(file.name)}">${esc(file.display_name || file.name)}</a>${file.folder?`<small>${esc(file.folder)}</small>`:''}</td><td>${esc(file.type_label)}<small class="muted format-label">${esc((file.kind || 'file').toUpperCase())}</small></td><td><span class="pill ${cls}">${esc(file.label)}</span><small class="outcome-detail">${esc(['failed','skipped','cancelled'].includes(file.status)?file.reason || file.explanation:'')}</small></td><td class="document-actions"><a class="btn ghost sm" href="/files/${file.id}">View document</a>${file.source_available?`<a class="btn ghost sm" href="/api/files/${file.id}/source?download=1">Download</a>`:''}${file.order_id?`<a class="btn blue sm" href="/orders/${file.order_id}">Open order</a>`:''}</td></tr>`;
  }).join('') || '<tr><td colspan="4">No documents uploaded yet.</td></tr>';
}

async function delOrder(id, button) {
  if (!confirm('Delete order #'+id+'? This cannot be undone.')) return;
  button.disabled = true;
  try {
    const result = await jsonRequest('/api/orders/'+id,{method:'DELETE'});
    if (result.ok !== true) throw new Error('Deletion not confirmed');
    const check = await fetch('/api/orders/'+id,{cache:'no-store'});
    if (check.status !== 404) throw new Error('Deletion read-back not verified');
    button.closest('tr').remove();
  } catch (error) { alert('Delete unverified: '+error.message); button.disabled=false; }
}
function displayResults(result) {
  const summary = result.summary, items = result.items || [];
  document.getElementById('res').innerHTML = (summary ? `<p>Known outcomes for ${summary.files_received} files: ${summary.extracted_ok} order drafts · ${summary.classified_only || 0} classified · ${summary.queued_ai || 0} waiting for AI · ${summary.processing || 0} processing · ${summary.skipped || 0} skipped · ${summary.failed || 0} failed.</p>` : '') + '<table class="grid"><tr><th>File</th><th>Outcome</th><th>Detail</th></tr>' + items.map(i => `<tr><td>${esc(i.name)}</td><td>${esc(i.status)}</td><td>${i.order_id ? `<a href="/orders/${Number(i.order_id)}">Open order ${Number(i.order_id)}</a> `:''}${esc(i.reason)}${(i.warnings || []).length ? ` · ${i.warnings.length} warnings`:''}</td></tr>`).join('')+'</table>';
}
document.getElementById('up').onsubmit = async event => {
  event.preventDefault(); if (uploading) return;
  const file=document.getElementById('f').files[0];if(!file)return;
  const output=document.getElementById('res'), button=event.target.querySelector('button');
  uploading=true;button.disabled=true;output.textContent='Uploading '+file.name+'…';
  try {
    const body=new FormData();body.append('file',file);
    const response=await jsonRequest('/api/upload?wait=0',{method:'POST',body});
    if (!response.batch_id || response.status !== 'processing') throw new Error('Upload returned no tracked processing batch');
    activeBatch=response.batch_id;
    try {localStorage.setItem('oe.activeBatch',activeBatch);} catch {}
    watchProgress(activeBatch);
    let terminal=false;
    for(let i=0;i<1200;i++) {
      await new Promise(resolve=>setTimeout(resolve,3000));
      const batch=await jsonRequest('/api/batches/'+encodeURIComponent(activeBatch));
      output.textContent=file.name+': '+batch.status;
      if(batch.status==='processing')continue;
      terminal=true;
      if(batch.status==='failed')throw new Error(batch.result?.error || 'Batch failed');
      if(batch.status!=='done')throw new Error('Unknown batch outcome: '+batch.status);
      displayResults(batch.result || {});await refreshOrders();await refreshDocuments();break;
    }
    if(!terminal)throw new Error('Still processing; polling paused. Upload retained. Batch '+activeBatch);
  } catch(error) { output.textContent='No verified final outcome'+(activeBatch?' (batch '+activeBatch+')':'')+': '+error.message; }
  finally { uploading=false;button.disabled=false; }
};
function fmtEta(seconds) {return seconds<60?'under a minute':'about '+Math.round(seconds/60)+' min';}
const timeLabel=stamp=>stamp ? new Date(stamp).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '';
const STAGE_LABELS={pending:'Awaiting preparation',queued:'Queued for worker',preparing:'Preparing document',retry_pending:'Read failed/cancelled; awaiting retry',read_failed:'AI read failed/cancelled',cancelled:'Cancelled',received:'Received by worker',starting:'Starting AI reader',model_started:'AI read started',output_received:'AI output received',returning:'Returning result',validating:'Validating result',saved:'Order saved',failed:'Failed',skipped:'Skipped',classified:'Supporting document'};
function renderProgress(p) {
  const target=document.getElementById('prog');
  if(!p.files){target.textContent='';return;}
  const head=p.batch_status==='cancelled' ? 'Cancelled' : p.batch_status==='failed' ? 'Batch failed' : p.phase==='waiting' ? 'Waiting for the active upload' : p.active ? 'Processing' : (p.files.some(f=>f.step==='failed')?'Finished with failures':'Processing finished');
  target.innerHTML=`<div class="eta"><b>${head}</b> · ${p.finished}/${p.total} files resolved · ${p.reads_done || 0}/${p.reads_total || 0} tracked AI reads returned${p.reads_failed?' · '+p.reads_failed+' reads failed/cancelled':''}${p.reads_queued?' · '+p.reads_queued+' AI reads queued for worker':''}${p.files_pending?' · '+p.files_pending+' files awaiting preparation':''}${p.files_retrying?' · '+p.files_retrying+' files awaiting retry':''}<span id="stream-state" class="muted"></span></div>${p.error?'<p>'+esc(p.error)+'</p>':''}<progress class="batch-progress" max="${Math.max(1,p.total || 0)}" value="${p.finished || 0}" aria-label="Files resolved"></progress><table class="grid flow">`+p.files.map(f=>{
    const jobs=f.reads?.jobs || [];
    const active=jobs.filter(j=>j.status==='leased');
    const details=jobs.length?'<details><summary>'+f.reads.done+'/'+f.reads.total+' reads returned'+(f.reads.failed?' · '+f.reads.failed+' failed/cancelled':'')+'</summary>'+jobs.map(j=>`<div>${j.vote==='Retry'?'Repair read':'Read '+esc(j.vote || '?')} · pages ${esc(j.page || '?')} · ${esc(j.worker || 'unclaimed')} · ${esc(j.model || '')} · ${esc(STAGE_LABELS[j.stage] || j.stage || j.status)}${j.received_at?' · received '+esc(timeLabel(j.received_at)):''}${j.started_at?' · started '+esc(timeLabel(j.started_at)):''}${j.returned_at?' · returned '+esc(timeLabel(j.returned_at)):''}${j.error?' · '+esc(j.error):''}</div>`).join('')+'</details>':'';
    const worker=active.map(j=>j.worker).filter(Boolean).join(', ');
    const direct=jobs.some(j=>j.worker==='OpenRouter');
    const marks=[[direct?'Prepared':'Queued',jobs.length>0],[direct?'API requested':'Worker received',jobs.some(j=>j.received_at || j.worker)],['AI started',jobs.some(j=>j.started_at)],['Result returned',(f.reads?.done || 0)>0],['Order saved',f.step==='saved']];
    const chain='<div class=\"milestones\">'+marks.map(([label,done])=>`<span class=\"${done?'done':''}\">${esc(label)}</span>`).join(' › ')+'</div>';
    return `<tr><td>${esc(f.name)}</td><td class="stage-cell">${esc(STAGE_LABELS[f.worker_stage] || f.step)}${worker?' · '+esc(worker):''}${f.order_id?` · <a href="/orders/${Number(f.order_id)}">Open order</a>`:''}${chain}${details}</td><td>${esc(f.reason)}</td></tr>`;
  }).join('')+'</table>';
}
function watchProgress(bid) {
  if(stream)stream.close();
  if(!bid || typeof EventSource==='undefined')return;
  stream=new EventSource('/api/batches/'+encodeURIComponent(bid)+'/events');
  stream.addEventListener('progress',event=>{try{renderProgress(JSON.parse(event.data));}catch{document.getElementById('prog').textContent='Progress stream unverified';}});
  stream.addEventListener('complete',event=>{try{renderProgress(JSON.parse(event.data));}catch{}stream.close();stream=null;refreshOrders().catch(()=>{});});
  stream.onerror=()=>{const el=document.getElementById('stream-state');if(el)el.textContent=' · Live stream reconnecting; checking saved state';};
}
async function prog() {
  try {
    const p=await jsonRequest('/api/progress'+(activeBatch?'?bid='+encodeURIComponent(activeBatch):''));
    renderProgress(p);
    await refreshDocuments();if(!uploading)await refreshOrders();
    if(activeBatch && !uploading) {
      const batch=await jsonRequest('/api/batches/'+encodeURIComponent(activeBatch));
      if(batch.status==='done')displayResults(batch.result || {});
    }
  } catch(error) {document.getElementById('prog').textContent='Progress unverified: '+error.message;}
}
watchProgress(activeBatch);

async function aiStatus() {
  try {
    const w=await jsonRequest('/api/ai-status');
    const limit=w.processing_limit;if(limit)document.getElementById('file-budget').textContent=`System processing limit: ${limit.limit} files total · ${limit.used} used · ${limit.remaining} remaining.`;
    const badge=document.getElementById('aiw');
    if(w.reader==='openrouter') {
      badge.className='pill '+(w.configured?'':'failed');
      badge.textContent='OpenRouter · '+(!w.configured?'key missing':w.waiting_retry?'waiting for free API retry':'key configured')+' · '+w.running_api_reads+' reads running';
      const status=document.getElementById('st_online');status.textContent=w.configured?'Key configured':'OPENROUTER_API_KEY missing';status.className=badge.className;
      document.getElementById('st_seen').textContent=w.waiting_retry?'Waiting for retry window':'No retry pending';
      document.getElementById('st_q').textContent=w.running_api_reads;
      document.getElementById('st_qf').textContent=w.queued_files;
      document.getElementById('st_c').textContent=w.cached;
      return;
    }
    badge.className='pill '+(w.online?'done':'failed');
    badge.textContent='AI worker '+(w.online?'online':'offline')+' · '+w.queued+' queued / '+w.leased+' running';
    const status=document.getElementById('st_online');status.textContent=w.online?'online':'offline';status.className=badge.className;
    document.getElementById('st_seen').textContent=w.last_seen_s==null?'never':w.last_seen_s+'s ago';
    document.getElementById('st_q').textContent=w.queued+' / '+w.leased;
    document.getElementById('st_qf').textContent=w.queued_files;
    document.getElementById('st_c').textContent=w.cached;
  } catch(error) {document.getElementById('aiw').textContent='AI status unverified';}
}
setInterval(prog,5000);setInterval(aiStatus,10000);
