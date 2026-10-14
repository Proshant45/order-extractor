'use strict';
const H = ['buyer_name', 'brand', 'style_number', 'factory', 'planned_line', 'fob', 'smv', 'cm', 'department'];
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const splitList = value => [...new Set(value.split(',').map(s => s.trim()).filter(Boolean))];
const SIZE_ALIASES = {SMALL:'S', MEDIUM:'M', MED:'M', LARGE:'L', XLARGE:'XL', EXTRALARGE:'XL', XSMALL:'XS', EXTRASMALL:'XS', XXLARGE:'XXL', '2XL':'XXL', XXXL:'3XL', SM:'S', LG:'L', MD:'M'};
function normSize(value) {
  const s = String(value).trim(), key = s.toUpperCase().replace(/[\s_.-]/g, '');
  return SIZE_ALIASES[key] || (/^\d+(\.\d+)?$/.test(s) ? String(Number(s)) : s.length <= 4 ? s.toUpperCase() : s);
}
const iso2dmy = value => /^(\d{4})-(\d{2})-(\d{2})$/.test(value || '') ? value.split('-').reverse().join('/') : (value || '');
function dmy2iso(value) {
  const text = value.trim();
  if (!text) return null;
  if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return text;
  const match = text.match(/^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2}|\d{4})$/);
  if (!match) return text;
  return `${match[3].length === 2 ? '20' + match[3] : match[3]}-${match[2].padStart(2, '0')}-${match[1].padStart(2, '0')}`;
}
function fullDate(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value || '')) return false;
  const date = new Date(value + 'T00:00:00Z');
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value;
}
function numberValue(value, integer = false) {
  if (value == null || String(value).trim() === '') return null;
  const s = String(value).trim();
  if (!/^\d+(?:,\d{3})*(?:\.\d+)?$/.test(s)) throw new Error(`Invalid ${integer ? 'quantity' : 'number'}: ${s}`);
  const n = Number(s.replace(/,/g, ''));
  if (!Number.isFinite(n) || (integer && (!Number.isSafeInteger(n) || n < 0))) throw new Error(`Invalid quantity: ${s}`);
  return n;
}
function displayNumber(value) {
  try { return numberValue(value, true) ?? 0; } catch { return 0; }
}
const originalSizes = Array.isArray(O.sizes) ? O.sizes : [];
let sizes = originalSizes.length ? originalSizes.map(normSize) : (O.id ? [] : ['XS', 'S', 'M', 'L', 'XL']);
let styleImage = O.style_image || null;
let saving = false, extracting = false, imageLoading = false, dirty = false;
let lastWarnings = Array.isArray(O.warnings) ? O.warnings : [];
let savedControlStates = null;
function showWarn(warnings = [], errors = [], message = '') {
  lastWarnings = Array.isArray(warnings) ? warnings : [];
  $('warn').innerHTML = errors.map(e => `<div class="e">${esc(e)}</div>`).join('') +
    (message ? `<div class="ok">${esc(message)}</div>` : '') +
    (lastWarnings.length ? `<details><summary>${lastWarnings.length} order warning${lastWarnings.length === 1 ? '' : 's'} — review details</summary>${lastWarnings.map(w => `<div>${esc(w)}</div>`).join('')}</details>` : '');
}
function setStatus(uncertain = '') {
  const known = O.status === 'saved' ? 'saved' : 'draft';
  $('save-state').className = 'pill ' + (uncertain ? 'skipped' : known);
  $('save-state').textContent = uncertain || (dirty ? `${known} · unsaved changes` : known);
  const state = O.extraction_status || 'unverified';
  const messages = {accepted: 'Two reads agree', needs_review: 'Check highlighted fields', unverified: 'Incomplete checks'};
  $('extraction-state').className = Object.hasOwn(messages, state) ? state : 'unverified';
  $('extraction-state').textContent = Object.hasOwn(messages, state) ? `${state.replaceAll('_', ' ')} · ${messages[state]}` : `unverified · Incomplete checks (${state})`;
}
function markDirty() { dirty = true; setStatus(); }
function setBusy() {
  // Preserve readonly/disabled states when a save locks the form for its read-back.
  if (saving && !savedControlStates) {
    savedControlStates = [...document.querySelectorAll('main input, main select, main button')].map(el => [el, el.disabled]);
    savedControlStates.forEach(([el]) => { el.disabled = true; });
  } else if (!saving && savedControlStates) {
    savedControlStates.forEach(([el, disabled]) => { el.disabled = disabled; });
    savedControlStates = null;
  }
  document.querySelectorAll('.save-button').forEach(b => { b.disabled = saving || extracting || imageLoading; });
  document.querySelectorAll('input[type=file]').forEach(el => { el.disabled = saving || extracting || imageLoading; });
  document.querySelectorAll('.save-button').forEach(b => { b.setAttribute('aria-busy', String(saving)); });
}
function showImage() {
  const valid = typeof styleImage === 'string' && /^data:image\/(?:png|jpe?g);base64,[A-Za-z0-9+/=\s]+$/.test(styleImage);
  $('style-preview-wrap').hidden = !valid;
  if (valid) $('style-preview').src = styleImage;
  else $('style-preview').removeAttribute('src');
}
async function loadStyleImage(event) {
  const file = event.target.files[0];
  if (!file) return;
  if (!['image/jpeg', 'image/png'].includes(file.type)) {
    showWarn(lastWarnings, ['Style image must be JPEG or PNG.']); event.target.value = ''; return;
  }
  imageLoading = true; setBusy();
  try {
    const data = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error('Style image could not be read.'));
      reader.onabort = () => reject(new Error('Style image reading was interrupted.'));
      reader.readAsDataURL(file);
    });
    await new Promise((resolve, reject) => {
      const image = new Image(); image.onload = resolve;
      image.onerror = () => reject(new Error('The selected file could not be decoded as an image.'));
      image.src = data;
    });
    styleImage = data; showImage(); markDirty();
  } catch (error) { showWarn(lastWarnings, [error.message]); }
  finally { imageLoading = false; event.target.value = ''; setBusy(); }
}
const choices = Object.fromEntries(Object.entries(typeof OPTIONS === 'undefined' ? {} : OPTIONS).map(([key,values]) => [key, [...values]]));
let choiceTarget = null;
const choiceKeys = {buyer_name:'Buyer name',brand:'Brand',factory:'Factory',department:'Department',destination:'Destination',color:'Color',sizes:'Size range',currency:'Currency', 'cm-currency':'Currency'};
function choiceOptions(key, current, placeholder) {
  const values = [...new Set([...(choices[key] || []), ...(current ? [current] : [])])];
  return `<option value="">${esc(placeholder)}</option>` + values.map(value => `<option value="${esc(value)}"${value === current ? ' selected' : ''}>${esc(value)}</option>`).join('') + '<option value="__add__">＋ Add new…</option>';
}
function fillSelect(select, key, current, placeholder) {
  select.innerHTML = choiceOptions(key,current,placeholder);
  select.value = current || '';
  select.dataset.choiceKey = key;
  select.dataset.selected = current || '';
}
function populateLists() {
  ['buyer_name','brand','factory','department'].forEach(key => fillSelect($(key),key,O[key],$(key).dataset.placeholder));
  choices.sizes = [...new Set(['XS, S, M, L, XL',...(choices.sizes || []), ...(sizes.length ? [sizes.join(', ')] : [])])];
  fillSelect($('sizes'),'sizes',originalSizes.length ? sizes.join(', ') : '', 'Select size');
  choices.currency = [...new Set(['USD','EUR','GBP','BDT',...(choices.currency || []),...(O.currency ? [O.currency] : [])])];
  const currency = O.currency || (O.id ? '' : 'USD');
  fillSelect($('currency'),'currency',currency,'CCY');
  fillSelect($('cm-currency'),'currency',currency,'CCY');
  updateColorList();
}
function updateColorList() {
  choices.color = [...new Set([...(choices.colors || []),...(choices.color || []),...splitList($('colors').value),...(O.pos || []).flatMap(p => (p.lines || []).map(l => l.color).filter(Boolean))])];
  document.querySelectorAll('select.color').forEach(select => fillSelect(select,'color',select.value,'Select color name'));
}
function syncCurrency(value) {
  ['currency','cm-currency'].forEach(id => fillSelect($(id),'currency',value,'CCY'));
}
function choiceChanged(select) {
  if (select.value === '__add__') {
    select.value = select.dataset.selected || '';
    choiceTarget = select;
    const key = select.dataset.choiceKey;
    $('choice-title').textContent = 'Add ' + (choiceKeys[key] || key).toLowerCase();
    $('choice-label').textContent = choiceKeys[key] || key;
    $('choice-hint').textContent = key === 'sizes' ? 'Enter size labels separated by commas, for example XS, S, M, L, XL.' : '';
    $('choice-value').value = '';
    $('choice-dialog').showModal(); $('choice-value').focus();
    return;
  }
  select.dataset.selected = select.value;
  if (select.id === 'sizes') changeSizes();
  if (select.dataset.choiceKey === 'currency') syncCurrency(select.value);
  markDirty();
}
function dateControl(key,value) {
  const iso = fullDate(value) ? value : '';
  return `<span class="date-field"><input class="${key}" value="${esc(iso2dmy(value))}" placeholder="DD/MM/YY" title="A date without a year stays as printed"><input type="date" class="date-picker" value="${esc(iso)}" aria-label="Choose ${key.replace('_',' ')}" onchange="this.previousElementSibling.value=iso2dmy(this.value);markDirty()"></span>`;
}
function lineRow(line, index) {
  const l = line || {}, meta = {...l, sizes: l.sizes || {}};
  const hasSizes = Object.keys(meta.sizes).length > 0;
  const qty = hasSizes ? Object.values(meta.sizes).reduce((sum, value) => sum + displayNumber(value), 0) : l.order_qty;
  return `<tr><td>${index + 1}</td><td><select class="color" data-choice-key="color" data-selected="${esc(l.color)}" aria-label="Color row ${index + 1}">${choiceOptions('color',l.color,'Select color name')}</select><input type="hidden" class="meta" value="${esc(JSON.stringify(meta))}"><small class="muted line-meta">${esc([l.style, l.color_code, l.rate != null ? '@ ' + l.rate : ''].filter(Boolean).join(' · '))}</small></td>` +
    sizes.map(s => `<td><input class="q" data-s="${esc(s)}" value="${esc(l.sizes?.[s] ?? '')}" placeholder="Enter ${esc(s)}" inputmode="numeric" aria-label="${esc(s)} quantity row ${index + 1}"></td>`).join('') +
    `<td class="oq"><input class="order_qty" value="${esc(qty ?? '')}" data-fallback="${esc(l.order_qty ?? '')}" placeholder="order qty" inputmode="numeric" aria-label="Order quantity row ${index + 1}" ${hasSizes ? 'readonly' : ''}></td><td><div class="row-actions"><button class="row-add" onclick="addLine(this)" aria-label="Add color row" title="Add row">+</button><button class="ic row-delete" onclick="deleteLine(this)" aria-label="Delete color row" title="Delete row"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/></svg></button><button class="ic row-copy" onclick="duplicateLine(this)" aria-label="Duplicate color row" title="Duplicate row"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h3"/></svg></button></div></td></tr>`;
}
function blankLines() { return Array.from({length: 4}, () => ({color: '', sizes: {}})); }
function poBlock(p) {
  const lines = p.lines?.length ? p.lines : blankLines();
  return `<section class="card po"><h3>Add PO <button class="plus" onclick="addPO()" aria-label="Add PO">+</button><span class="po-heading-actions"><label class="stated-label">Stated Total Qty <input class="stated_total_qty" value="${esc(p.stated_total_qty ?? '')}" inputmode="numeric" placeholder="Optional"></label><button class="ic r right" onclick="deletePO(this)">Remove PO</button></span></h3>
<div class="row seven"><label>PO Number <span class="required">*</span><input class="po_number" value="${esc(p.po_number)}" placeholder="Enter a PO number"></label>
<label>Destination <span class="required">*</span><select class="destination" data-choice-key="destination" data-selected="${esc(p.destination)}" aria-label="Destination">${choiceOptions('destination',p.destination,'Select your destination')}</select></label>
<label>Ship Date <span class="required">*</span>${dateControl('ship_date',p.ship_date || p.date_raw?.ship_date || '')}</label>
<label>PO Date${dateControl('po_date',p.po_date || p.date_raw?.po_date || '')}</label>
<label>PO Sheet<span class="chip" title="${esc(O.file_name || '')}"><span class="sheet-icon">▧</span>${esc(O.file_name || 'No source document')}</span></label>
<label>Sales Contract No.<input class="sales_contract_number" value="${esc(p.sales_contract_number)}" placeholder="Enter sales contract number"></label>
<label>PO Document<span class="upl file-button po-upload"><span>↥ &nbsp; Upload a file</span><small>Auto Extract into a new order</small><input type="file" accept=".pdf,.png,.jpg,.jpeg,.webp,.tif,.tiff,.xlsx,.xls,.csv,.docx,.zip" aria-label="Auto Extract PO document" onchange="autoExtract(this)"></span></label></div>
<input type="hidden" class="pometa" value="${esc(JSON.stringify({stated_total_value: p.stated_total_value ?? null, currency: p.currency ?? null, date_raw: p.date_raw || {}}))}">
<div class="table-surround"><table class="grid sz"><thead><tr><th>SL</th><th>Color</th>${sizes.map(s => `<th>${esc(s)}</th>`).join('')}<th>Order Qty</th><th>Action</th></tr></thead><tbody>${lines.map(lineRow).join('')}</tbody>
<tfoot><tr><td></td><td>Total</td>${sizes.map(s => `<td class="ts" data-s="${esc(s)}">0</td>`).join('')}<td class="tt">0</td><td></td></tr></tfoot></table></div></section>`;
}
function readLine(row, strict = true) {
  const meta = JSON.parse(row.querySelector('.meta').value);
  const sizeMap = {...(meta.sizes || {})};
  row.querySelectorAll('.q').forEach(input => {
    if (input.value.trim() === '') delete sizeMap[input.dataset.s];
    else sizeMap[input.dataset.s] = strict ? numberValue(input.value, true) : input.value;
  });
  if (strict) Object.keys(sizeMap).forEach(key => { sizeMap[key] = numberValue(sizeMap[key], true); });
  const qtyInput = row.querySelector('.order_qty');
  const hasSizes = Object.keys(sizeMap).length > 0;
  const fallback = qtyInput.readOnly ? qtyInput.dataset.fallback : qtyInput.value;
  const qty = hasSizes ? Object.values(sizeMap).reduce((sum, value) => sum + (strict ? numberValue(value, true) : displayNumber(value)), 0) : (strict ? numberValue(fallback, true) : fallback);
  return {...meta, color: row.querySelector('.color').value.trim(), sizes: sizeMap, order_qty: hasSizes && meta.order_qty != null ? meta.order_qty : (qty ?? null)};
}
function readPO(po, strict = true) {
  const get = name => po.querySelector('.' + name).value.trim() || null;
  const meta = JSON.parse(po.querySelector('.pometa').value);
  const dateRaw = {...meta.date_raw};
  const dates = {};
  ['ship_date', 'po_date'].forEach(key => {
    const text = get(key), date = text && dmy2iso(text);
    dates[key] = fullDate(date) ? date : null;
    if (text && !dates[key]) dateRaw[key] = text;
    else delete dateRaw[key];
  });
  const lines = [...po.querySelectorAll('tbody tr')].map(row => readLine(row, strict));
  return {...meta, ...dates, date_raw: dateRaw, po_number: get('po_number') || '', destination: get('destination'), sales_contract_number: get('sales_contract_number'),
    stated_total_qty: strict ? numberValue(get('stated_total_qty'), true) : get('stated_total_qty'),
    lines: lines.filter(l => l.color || Object.keys(l.sizes).length || (l.order_qty !== null && l.order_qty !== '') || l.style || l.color_code || l.rate != null || l.net_value != null)};
}
function collect(strict = true) {
  const order = {};
  H.forEach(key => { order[key] = $(key).value.trim() || null; });
  ['planned_line', 'fob', 'smv', 'cm'].forEach(key => { if (strict) order[key] = numberValue(order[key], key === 'planned_line'); });
  order.sizes = sizes.slice();
  order.colors = splitList($('colors').value);
  order.currency = $('currency').value.trim().toUpperCase() || null;
  order.style_image = styleImage;
  order.pos = [...document.querySelectorAll('.po')].map(po => readPO(po, strict));
  return order;
}
function renderPOs(pos) { $('pos').innerHTML = pos.map(poBlock).join(''); tot(); highlights(); }
function addPO() { $('pos').insertAdjacentHTML('beforeend', poBlock({po_number: '', lines: []})); tot(); markDirty(); }
function deletePO(button) { button.closest('.po').remove(); tot(); markDirty(); }
function addLine(button) {
  const row = button.closest('tr'); row.insertAdjacentHTML('afterend', lineRow({color: '', sizes: {}}, 0)); tot(); markDirty();
}
function duplicateLine(button) {
  const row = button.closest('tr'); row.insertAdjacentHTML('afterend', lineRow(readLine(row, false), 0)); tot(); highlights(); markDirty();
}
function deleteLine(button) { button.closest('tr').remove(); tot(); markDirty(); }
function tot() {
  let hiddenCount = 0;
  document.querySelectorAll('.po').forEach(po => {
    const totals = {}; let all = 0;
    po.querySelectorAll('tbody tr').forEach((row, index) => {
      row.cells[0].textContent = index + 1;
      const line = readLine(row, false), qty = row.querySelector('.order_qty');
      const hasSizes = Object.keys(line.sizes).length > 0;
      row.querySelectorAll('.q').forEach(input => {
        try { numberValue(input.value, true); input.classList.remove('bad'); }
        catch { input.classList.add('bad'); }
      });
      Object.entries(line.sizes).forEach(([s, value]) => { totals[s] = (totals[s] || 0) + displayNumber(value); if (!sizes.includes(s)) hiddenCount++; });
      if (!qty.readOnly) qty.dataset.fallback = qty.value;
      qty.readOnly = hasSizes;
      qty.value = hasSizes ? Object.values(line.sizes).reduce((sum,v) => sum + displayNumber(v), 0) : qty.dataset.fallback;
      qty.title = hasSizes ? 'Calculated from all sizes, including retained hidden sizes' : 'Enter total quantity when size quantities are unavailable';
      try { numberValue(qty.value, true); qty.classList.remove('bad'); } catch { qty.classList.add('bad'); }
      all += displayNumber(qty.value);
    });
    po.querySelectorAll('.ts').forEach(cell => { cell.textContent = (totals[cell.dataset.s] || 0).toLocaleString(); });
    po.querySelector('.tt').textContent = all.toLocaleString();
  });
  $('hidden-size-note').textContent = hiddenCount ? `${hiddenCount} hidden size entr${hiddenCount === 1 ? 'y' : 'ies'} retained in totals and save` : '';
}
function changeSizes() {
  const current = collect(false);
  sizes = [...new Set(splitList($('sizes').value).map(normSize))];
  $('sizes').value = sizes.join(', ');
  renderPOs(current.pos.map(p => ({...p, lines: p.lines.length ? p.lines : blankLines()})));
  markDirty();
}
function mark(element, value) {
  if (!element) return;
  element.classList.add('alt'); element.title = 'Second AI read: ' + (value ?? '(empty)');
}
function highlights() {
  Object.entries(HL || {}).forEach(([key, value]) => {
    const parts = key.split('|');
    if (parts[0] === 'h') { mark($(parts[1]), value); return; }
    document.querySelectorAll('.po').forEach(po => {
      if (po.querySelector('.po_number').value !== parts[1]) return;
      if (parts[0] === 'po') {
        const field = [...po.querySelectorAll('input,select')].find(input => input.classList.contains(parts[2])); mark(field, value);
      }
      if (parts[0] === 'q') po.querySelectorAll('tbody tr').forEach(row => {
        if (row.querySelector('.color').value.trim().replace(/\s+/g,' ').toUpperCase() !== parts[2]) return;
        mark([...row.querySelectorAll('.q')].find(input => input.dataset.s === parts[3]), value);
        if (!sizes.includes(parts[3])) mark(row.querySelector('.order_qty'), `Hidden size ${parts[3]}: ${value ?? '(empty)'}`);
      });
    });
  });
}
const realId = id => /^(?:[1-9]\d*)$/.test(String(id)) && Number.isSafeInteger(Number(id));
function responseError(body, fallback) {
  if (!body || typeof body !== 'object') return fallback;
  if (Array.isArray(body.errors)) return body.errors.map(e => typeof e === 'string' ? e : JSON.stringify(e)).join('; ');
  return typeof (body.detail || body.error || body.reason) === 'string' ? (body.detail || body.error || body.reason) : (body.detail ? JSON.stringify(body.detail) : fallback);
}
async function requestJSON(url, options = {}) {
  let response;
  try { response = await fetch(url, {...options, cache: 'no-store'}); }
  catch (error) { throw new Error(`Network error: ${error.message}`); }
  let body;
  try { body = await response.json(); }
  catch { throw new Error(`HTTP ${response.status}: response was not valid JSON`); }
  if (!response.ok) throw new Error(responseError(body, `HTTP ${response.status} ${response.statusText || 'request failed'}`));
  if (!body || typeof body !== 'object') throw new Error('Server returned an invalid response');
  return body;
}
function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])]));
  return value;
}
function verifiedData(expected, actual) {
  const failures = [];
  const equal = (a, b) => JSON.stringify(canonical(a)) === JSON.stringify(canonical(b));
  const check = (label, a, b) => { if (!equal(a, b)) failures.push(label); };
  [...H, 'currency', 'style_image', 'status'].forEach(key => check(key, expected[key] ?? null, actual[key] ?? null));
  const derivedSizes = [...new Set(expected.pos.flatMap(p => p.lines.flatMap(l => Object.keys(l.sizes))))];
  const derivedColors = [...new Set(expected.pos.flatMap(p => p.lines.map(l => l.color)))];
  check('sizes', expected.sizes.length ? expected.sizes : derivedSizes, actual.sizes);
  check('colors', expected.colors.length ? expected.colors : derivedColors, actual.colors);
  if (!Array.isArray(actual.pos) || expected.pos.length !== actual.pos.length) { failures.push('PO count'); return failures; }
  expected.pos.forEach((po, index) => {
    const saved = actual.pos[index];
    ['po_number', 'destination', 'ship_date', 'po_date', 'sales_contract_number', 'stated_total_qty', 'stated_total_value', 'currency'].forEach(key => check(`PO ${index + 1}: ${key}`, po[key] ?? null, saved[key] ?? null));
    check(`PO ${index + 1}: date_raw`, po.date_raw || {}, saved.date_raw || {});
    if (!Array.isArray(saved.lines) || po.lines.length !== saved.lines.length) { failures.push(`PO ${index + 1}: line count`); return; }
    po.lines.forEach((line, row) => {
      ['color', 'sizes', 'style', 'color_code', 'order_qty', 'rate', 'net_value'].forEach(key => check(`PO ${index + 1}, row ${row + 1}: ${key}`, line[key] ?? null, saved.lines[row][key] ?? null));
    });
  });
  return failures;
}
async function save(status) {
  if (saving || extracting || imageLoading) return;
  if (!['draft', 'saved'].includes(status)) { showWarn(lastWarnings, ['Unknown save status.']); return; }
  let payload;
  try { payload = {...collect(), status}; }
  catch (error) { showWarn(lastWarnings, [error.message]); return; }
  saving = true; setBusy(); setStatus('Saving · awaiting verification');
  let acknowledged = false;
  try {
    const result = await requestJSON(O.id ? '/api/orders/' + O.id : '/api/orders', {method: O.id ? 'PUT' : 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
    if (result.ok !== true || (result.status && !['draft', 'saved'].includes(result.status)) || (result.outcome && !['created', 'updated', 'saved', 'draft'].includes(result.outcome))) throw new Error(responseError(result, `Save response: ${result.outcome || result.status || 'ok was not true'}`));
    const id = result.id ?? O.id;
    if (!realId(id) || (O.id && Number(id) !== Number(O.id))) throw new Error('Save returned an invalid or different order ID');
    O.id = Number(id); acknowledged = true;
    const actual = await requestJSON('/api/orders/' + O.id);
    if (Number(actual.id) !== O.id) throw new Error('Read-back returned a different order');
    const differences = verifiedData(payload, actual);
    if (differences.length) throw new Error('Read-back differs: ' + differences.join('; '));
    O.status = actual.status; O.extraction_status = actual.extraction_status || O.extraction_status;
    dirty = false; setStatus();
    $('order-title').textContent = $('crumb-title').textContent = 'Order #' + O.id;
    document.title = 'Order #' + O.id + ' · Texora 360';
    document.querySelectorAll('.primary-save').forEach(button => { button.textContent = 'Save'; });
    window.history.replaceState(null, '', '/orders/' + O.id);
    showWarn(actual.warnings || result.warnings || [], [], status === 'draft' ? 'Draft saved · verified by read-back' : 'Saved · verified by read-back');
  } catch (error) {
    setStatus('Save unverified');
    showWarn(lastWarnings, [`${acknowledged ? 'Write acknowledged for order #' + O.id + '; read-back not verified' : 'Save not verified'}: ${error.message}. ${acknowledged ? 'Review before retrying.' : 'The server may have received the request; check Order Book before creating another order.'}`]);
  } finally { saving = false; setBusy(); }
}
function extractionItems(result) {
  if (Array.isArray(result.items)) return result.items;
  if (Array.isArray(result.results)) return result.results;
  return [];
}
function showExtraction(result) {
  const items = extractionItems(result);
  const summary = result.summary;
  $('extract-result').innerHTML = (result.status || result.outcome ? `<p>Outcome: ${esc(result.outcome || result.status)}${result.reason ? ' · ' + esc(result.reason) : ''}</p>` : '') + (items.length ? '<p>Extraction outcome</p><ul>' + items.map(item => `<li>${esc(item.name || 'Document')}: <b>${esc(item.status || 'unverified')}</b>${item.doc_type ? ' · ' + esc(item.doc_type) : ''}${item.reason ? ' · ' + esc(item.reason) : ''}${item.error ? ' · ' + esc(item.error) : ''}${realId(item.order_id) && ['done', 'cached'].includes(item.status) ? ` · <a href="/orders/${Number(item.order_id)}">Open order #${Number(item.order_id)}</a>` : ''}${item.status === 'done' && !realId(item.order_id) ? ' · No order ID returned; no order opened' : ''}${(item.warnings || []).length ? ' · ' + esc(item.warnings.join('; ')) : ''}</li>`).join('') + '</ul>' : '<p>No order result returned; extraction is unverified.</p>') +
    (summary ? '<p>' + ['extracted_ok', 'classified_only', 'queued_ai', 'skipped', 'failed'].filter(key => summary[key] != null).map(key => `${esc(key)}: ${esc(summary[key])}`).join(' · ') + '</p>' : '') +
    (result.error || result.detail ? `<p class="err">${esc(responseError(result, 'Extraction unverified'))}</p>` : '');
}
async function autoExtract(input) {
  const file = input.files[0];
  if (!file || extracting || saving || imageLoading) return;
  extracting = true; setBusy();
  let batchId = null, phase = 'Uploading', finalShown = false;
  $('extract-result').textContent = `Uploading ${file.name}…`;
  try {
    const form = new FormData(); form.append('file', file);
    let result = await requestJSON('/api/upload?wait=0', {method: 'POST', body: form});
    if (result.batch_id) {
      batchId = String(result.batch_id); phase = 'processing';
      for (let count = 0; ; count++) {
        if (count >= 120) throw new Error('Still processing; automatic polling paused. Check Order Book for the final outcome.');
        await new Promise(resolve => setTimeout(resolve, 3000));
        const batch = await requestJSON('/api/batches/' + encodeURIComponent(batchId));
        phase = batch.status || 'unverified';
        $('extract-result').textContent = `${file.name}: ${phase} (batch ${batchId})`;
        if (phase === 'processing') continue;
        if (phase === 'failed') throw new Error(responseError(batch.result || batch, 'Extraction failed'));
        if (phase !== 'done') { showExtraction(batch.result || {items: [{name: file.name, status: phase, reason: 'No completed order result returned'}]}); return; }
        result = batch.result || {}; break;
      }
    }
    showExtraction(result); finalShown = true;
    const items = extractionItems(result);
    if (result.ok !== false && (!result.status || ['done', 'cached'].includes(result.status)) && (!result.outcome || ['done', 'cached'].includes(result.outcome)) && items.length === 1 && ['done', 'cached'].includes(items[0].status) && realId(items[0].order_id) && !result.error && !result.detail && !['failed', 'queued_ai', 'skipped', 'classified_only'].some(key => Number(result.summary?.[key]) > 0)) {
      const id = Number(items[0].order_id), actual = await requestJSON('/api/orders/' + id);
      if (Number(actual.id) !== id || !Array.isArray(actual.pos)) throw new Error('Extracted order could not be verified by read-back');
      window.location.assign('/orders/' + id);
    }
  } catch (error) {
    if (!finalShown) $('extract-result').textContent = `${file.name}: ${phase === 'failed' ? 'failed' : 'no verified final outcome'}`;
    const detail = document.createElement('p'); detail.className = 'err';
    detail.textContent = `Extraction not verified${batchId ? ' (batch ' + batchId + ', last status: ' + phase + ')' : ''}: ${error.message}`;
    $('extract-result').appendChild(detail);
  } finally { extracting = false; input.value = ''; setBusy(); }
}
async function del() {
  if (saving || extracting || !O.id || !confirm('Delete this order? This cannot be undone.')) return;
  saving = true; setBusy();
  try {
    const result = await requestJSON('/api/orders/' + O.id, {method: 'DELETE'});
    if (result.ok !== true) throw new Error(responseError(result, 'Deletion was not confirmed'));
    const readBack = await fetch('/api/orders/' + O.id, {cache: 'no-store'});
    if (readBack.status !== 404) throw new Error('Deletion could not be verified by read-back');
    window.location.assign('/');
  } catch (error) { showWarn(lastWarnings, ['Delete not verified: ' + error.message]); }
  finally { saving = false; setBusy(); }
}
function init() {
  populateLists();
  H.forEach(key => { if ($(key).tagName !== 'SELECT') $(key).value = O[key] ?? ''; });
  $('colors').value = (O.colors || []).join(', ');
  updateColorList();
  $('colors').addEventListener('input', updateColorList);
  $('style-image').addEventListener('change', loadStyleImage);
  $('remove-image').addEventListener('click', () => { styleImage = null; showImage(); markDirty(); });
  $('auto-extract').addEventListener('change', event => autoExtract(event.target));
  $('pos').addEventListener('input', tot);
  document.querySelector('main').addEventListener('input', event => { if (event.target.type !== 'file' && event.target.tagName !== 'SELECT') markDirty(); });
  document.querySelector('main').addEventListener('change', event => {
    if (event.target.tagName === 'SELECT') choiceChanged(event.target);
  });
  $('choice-form').addEventListener('submit', event => {
    event.preventDefault();
    let value = $('choice-value').value.trim(); if (!value || !choiceTarget) return;
    const key = choiceTarget.dataset.choiceKey;
    if (key === 'sizes') value = [...new Set(splitList(value).map(normSize))].join(', ');
    if (key === 'currency') value = value.toUpperCase();
    if (!value || value === '__add__') return;
    choices[key] = [...new Set([...(choices[key] || []),value])];
    fillSelect(choiceTarget,key,value,choiceTarget.dataset.placeholder || 'Select ' + (choiceKeys[key] || key).toLowerCase());
    $('choice-dialog').close();
    choiceChanged(choiceTarget);
    if (key === 'color') updateColorList();
  });
  $('see-color').addEventListener('click', () => {
    const selected = splitList($('colors').value);
    const values = [...new Set([...(choices.color || []),...selected])];
    $('color-choices').innerHTML = values.map(value => `<label class="color-choice"><input type="checkbox" value="${esc(value)}"${selected.includes(value) ? ' checked' : ''}>${esc(value)}</label>`).join('') || '<p class="muted">No colors added yet.</p>';
    $('new-color').value = ''; $('color-dialog').showModal();
  });
  $('color-form').addEventListener('submit', event => {
    event.preventDefault();
    const selected = [...document.querySelectorAll('#color-choices input:checked')].map(input => input.value);
    const added = $('new-color').value.trim(); if (added) selected.push(added);
    $('colors').value = [...new Set(selected)].join(', ');
    updateColorList();markDirty();$('color-dialog').close();
  });
  renderPOs(O.pos?.length ? O.pos : (O.id ? [] : [{po_number: '', lines: []}, {po_number: '', lines: []}]));
  showImage(); setStatus(); showWarn(lastWarnings);
}
init();highlights();
