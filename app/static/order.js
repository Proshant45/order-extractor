const H=['buyer_name','brand','style_number','factory','planned_line','fob','smv','cm','department'];
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const iso2dmy=d=>d?d.split('-').reverse().join('/'):'';
const dmy2iso=d=>{d=d.trim();if(!d)return null;const m=d.match(/^(\d{1,2})[\/.\-](\d{1,2})[\/.\-](\d{2,4})$/);if(!m)return d;const y=m[3].length==2?'20'+m[3]:m[3];return `${y}-${m[2].padStart(2,'0')}-${m[1].padStart(2,'0')}`};
let sizes=(O.sizes&&O.sizes.length)?O.sizes:['XS','S','M','L','XL'];
function init(){H.forEach(k=>$(k).value=O[k]??'');$('sizes').value=sizes.join(', ');$('colors').value=(O.colors||[]).join(', ');
 $('sizes').onchange=()=>{const cur=collect();sizes=$('sizes').value.split(',').map(s=>s.trim()).filter(Boolean);renderPOs(cur.pos)};
 renderPOs(O.pos.length?O.pos:(O.id?[]:[{po_number:'',lines:[]}]));showWarn(O.warnings)}
function showWarn(w,err){$('warn').innerHTML=(err||[]).map(x=>`<div class="e">✖ ${esc(x)}</div>`).join('')+(w||[]).map(x=>`<div>⚠ ${esc(x)}</div>`).join('')}
function lineRow(l,i){return `<tr><td>${i+1}</td><td><input class="color" value="${esc(l.color)}"><input type="hidden" class="meta" value="${esc(JSON.stringify({style:l.style,color_code:l.color_code,rate:l.rate,net_value:l.net_value,order_qty:l.order_qty}))}"><small class="muted">${esc(l.style||'')} ${l.rate!=null?'@ '+l.rate:''}</small></td>`+
 sizes.map(s=>`<td><input class="q" data-s="${esc(s)}" value="${l.sizes?.[s]??''}" placeholder="Enter ${esc(s)}"></td>`).join('')+
 `<td class="oq">0</td><td class="act"><button class="ic b" onclick="addLine(this)">＋</button><button class="ic r" onclick="this.closest('tr').remove();tot()">🗑</button></td></tr>`}
function poBlock(p){return `<section class="card po"><h3>Add PO <span class="plus">＋</span><button class="ic r right" onclick="this.closest('.po').remove()">Remove PO</button></h3>
<div class="row seven"><label>PO Number *<input class="po_number" value="${esc(p.po_number)}" placeholder="Enter a po number"></label>
<label>Destination *<input class="destination" value="${esc(p.destination)}" placeholder="Select your destination"></label>
<label>Ship Date *<input class="ship_date" value="${iso2dmy(p.ship_date)}" placeholder="DD/MM/YYYY"></label>
<label>PO Date<input class="po_date" value="${iso2dmy(p.po_date)}" placeholder="DD/MM/YYYY"></label>
<label>PO Sheet<div class="chip">📄 ${esc(O.file_name||'—')}</div></label>
<label>Sales Contract No.<input class="sales_contract_number" value="${esc(p.sales_contract_number)}" placeholder="Enter sales contract number"></label>
<label>Stated Total Qty<input class="stated_total_qty" value="${p.stated_total_qty??''}"></label></div>
<input type="hidden" class="pometa" value="${esc(JSON.stringify({stated_total_value:p.stated_total_value,currency:p.currency}))}">
<table class="grid sz"><thead><tr><th>SL</th><th>Color</th>${sizes.map(s=>`<th>${esc(s)}</th>`).join('')}<th>Order Qty</th><th>Action</th></tr></thead>
<tbody>${(p.lines.length?p.lines:[{color:'',sizes:{}}]).map(lineRow).join('')}</tbody>
<tfoot><tr><td></td><td><b>Total</b></td>${sizes.map(s=>`<td class="ts" data-s="${esc(s)}">0</td>`).join('')}<td class="tt">0</td><td></td></tr></tfoot></table></section>`}
function renderPOs(pos){$('pos').innerHTML=pos.map(poBlock).join('');document.querySelectorAll('#pos').forEach(e=>e.oninput=tot);tot()}
function addPO(){$('pos').insertAdjacentHTML('beforeend',poBlock({po_number:'',lines:[]}));tot()}
function addLine(b){const tb=b.closest('tbody');tb.insertAdjacentHTML('beforeend',lineRow({color:'',sizes:{}},tb.rows.length));tot()}
function tot(){document.querySelectorAll('.po').forEach(po=>{const t={};let all=0;po.querySelectorAll('tbody tr').forEach((r,i)=>{r.cells[0].textContent=i+1;let s=0;
 r.querySelectorAll('.q').forEach(q=>{const v=parseInt(q.value.replace(/,/g,''))||0;q.classList.toggle('bad',q.value!==''&&!/^\d[\d,]*$/.test(q.value.trim()));s+=v;t[q.dataset.s]=(t[q.dataset.s]||0)+v});r.querySelector('.oq').textContent=s.toLocaleString();all+=s});
 po.querySelectorAll('.ts').forEach(c=>c.textContent=(t[c.dataset.s]||0).toLocaleString());po.querySelector('.tt').textContent=all.toLocaleString()})}
function collect(){const o={};H.forEach(k=>o[k]=$(k).value.trim()||null);o.planned_line=o.planned_line?parseInt(o.planned_line):null;
 o.sizes=$('sizes').value.split(',').map(s=>s.trim()).filter(Boolean);o.colors=$('colors').value.split(',').map(s=>s.trim()).filter(Boolean);o.currency=O.currency;
 o.pos=[...document.querySelectorAll('.po')].map(po=>{const g=c=>po.querySelector('.'+c).value.trim()||null;const pm=JSON.parse(po.querySelector('.pometa').value);
  return {...pm,po_number:g('po_number')||'',destination:g('destination'),ship_date:g('ship_date')&&dmy2iso(g('ship_date')),po_date:g('po_date')&&dmy2iso(g('po_date')),
  sales_contract_number:g('sales_contract_number'),stated_total_qty:g('stated_total_qty'),
  lines:[...po.querySelectorAll('tbody tr')].map(r=>{const m=JSON.parse(r.querySelector('.meta')?.value||'{}');const sz={};r.querySelectorAll('.q').forEach(q=>{if(q.value.trim()!=='')sz[q.dataset.s]=q.value.trim()});
   return {...m,color:r.querySelector('.color').value.trim(),sizes:sz}}).filter(l=>l.color||Object.keys(l.sizes).length)}});return o}
async function save(){const r=await fetch(O.id?'/api/orders/'+O.id:'/api/orders',{method:O.id?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(collect())});const j=await r.json().catch(()=>({}));
 if(!r.ok){showWarn([],j.errors||[j.detail||'Save failed']);return}
 if(!O.id){location.href='/orders/'+j.id;return}
 showWarn(j.warnings);$('warn').insertAdjacentHTML('afterbegin','<div class="ok">✔ Saved</div>')}
async function del(){if(!confirm('Delete this order? This cannot be undone.'))return;const r=await fetch('/api/orders/'+O.id,{method:'DELETE'});if(r.ok)location.href='/';else showWarn([],['Delete failed'])}
function mark(el,v){if(!el)return;el.classList.add('alt');el.title='Second AI read: '+(v??'(empty)')}
function highlights(){Object.entries(HL||{}).forEach(([k,v])=>{const p=k.split('|');
 if(p[0]==='h')mark($(p[1]),v);
 document.querySelectorAll('.po').forEach(po=>{if(po.querySelector('.po_number').value!==p[1])return;
  if(p[0]==='po')mark(po.querySelector('.'+p[2]),v);
  if(p[0]==='q')po.querySelectorAll('tbody tr').forEach(r=>{if(r.querySelector('.color').value===p[2])mark(r.querySelector(`.q[data-s="${CSS.escape(p[3])}"]`),v)})})})}
init();highlights();
