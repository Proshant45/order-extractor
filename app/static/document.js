'use strict';
const documentForm=document.getElementById('document-editor');
if(documentForm){
  const original=JSON.parse(document.getElementById('document-data').textContent);
  const message=document.getElementById('document-save-state');
  const value=(input,kind)=>{
    if(input.value==='')return null;
    if(kind==='number'){const n=Number(input.value);if(!Number.isFinite(n))throw new Error('Enter a valid number');return n;}
    if(kind==='bool'){const v=input.value.trim().toLowerCase();if(!['true','false'].includes(v))throw new Error('Enter true or false');return v==='true';}
    return input.value;
  };
  const kind=v=>typeof v==='number'?'number':typeof v==='boolean'?'bool':'string';
  const canonical=v=>JSON.stringify(normalize(v));
  function normalize(v){if(Array.isArray(v))return v.map(normalize);if(v&&typeof v==='object')return Object.fromEntries(Object.keys(v).sort().map(k=>[k,normalize(v[k])]));return v;}
  documentForm.addEventListener('click',event=>{
    const remove=event.target.closest('[data-delete-row]');if(remove){remove.closest('tr').remove();return;}
    const add=event.target.closest('[data-add-row]');if(!add)return;
    const index=Number(add.dataset.addRow);const tbody=documentForm.querySelector(`[data-table="${index}"] tbody`);
    const row=document.createElement('tr');
    original.tables[index].columns.forEach((_,col)=>{
      const td=document.createElement('td'),input=document.createElement('input');
      input.dataset.kind=kind(original.tables[index].rows[0]?.[col]);td.append(input);row.append(td);
    });
    const td=document.createElement('td'),button=document.createElement('button');button.type='button';button.dataset.deleteRow='';button.className='btn ghost sm';button.textContent='Remove';td.append(button);row.append(td);tbody.append(row);
  });
  documentForm.addEventListener('submit',async event=>{
    event.preventDefault();const button=documentForm.querySelector('[type="submit"]');button.disabled=true;message.textContent='Saving…';
    try{
      const payload=structuredClone(original);payload.summary=document.getElementById('document-summary').value;
      documentForm.querySelectorAll('[data-field]').forEach(input=>{const i=Number(input.dataset.field);payload.fields[i].value=value(input,kind(original.fields[i].value));});
      documentForm.querySelectorAll('[data-table]').forEach(table=>{payload.tables[Number(table.dataset.table)].rows=Array.from(table.querySelectorAll('tbody tr'),row=>Array.from(row.querySelectorAll('input'),input=>value(input,input.dataset.kind)));});
      const path='/api/files/'+documentForm.dataset.fileId+'/data';
      const response=await fetch(path,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      const result=await response.json();if(!response.ok||result.ok!==true)throw new Error(typeof result.detail==='string'?result.detail:'Save was not confirmed');
      const check=await fetch(path,{cache:'no-store'});if(!check.ok)throw new Error('Saved data could not be read back');
      const saved=(await check.json()).metadata?.document;
      if(canonical(saved)!==canonical(payload))throw new Error('Read-back differs from your changes');
      message.textContent='Saved and verified';
    }catch(error){message.textContent='Save unverified: '+error.message;}
    finally{button.disabled=false;}
  });
}
