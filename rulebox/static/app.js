'use strict';
const $ = id => document.getElementById(id);
let state, editing = null, source = '', proposal = null;
const status = message => { $('status').textContent = message; };
function invalidate() { proposal = null; $('preview-panel').hidden = true; }
async function api(path, data) {
  const options = data === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-Rulebox-Token':state.token},body:JSON.stringify(data)};
  const response = await fetch('/api/' + path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Request failed');
  return result;
}
function button(label, action, extra='') {
  const el = document.createElement('button'); el.type='button'; el.textContent=label; el.className='secondary '+extra;
  el.addEventListener('click', () => run(action)); return el;
}
async function run(action) {
  try { await action(); } catch(error) { status(error.message); }
}
function edit(rule) {
  editing=rule.id || null; source=rule.source;
  $('title').value=rule.title; $('instruction').value=rule.instruction; $('enabled').checked=Boolean(rule.enabled ?? true);
  $('editor').hidden=false; $('edit-label').textContent=editing ? 'EDIT YOUR RULE' : 'REVIEW YOUR RULE';
  $('instruction').focus();
}
async function refresh() {
  state = await api('state'); $('project').textContent=state.project+'/AGENTS.md';
  $('engine').textContent=state.engine || 'Local formatter · no model required';
  $('count').textContent=state.rules.length;
  const list=$('rules'); list.replaceChildren();
  if (!state.rules.length) {
    const empty=document.createElement('div'); empty.className='empty';
    const symbol=document.createElement('div'); symbol.className='empty-symbol'; symbol.textContent='⊞';
    const heading=document.createElement('strong'); heading.textContent='A little clarity goes a long way.';
    const note=document.createElement('p'); note.textContent='Your first rule starts in the box on the left.';
    empty.append(symbol,heading,note); list.append(empty);
  }
  state.rules.forEach(rule=>{
    const item=document.createElement('article'); item.className='rule';
    const head=document.createElement('div'); head.className='rule-head';
    const title=document.createElement('h3'); title.textContent=rule.title;
    const tag=document.createElement('span'); tag.className='rule-tag'; tag.textContent=rule.enabled?'● Enabled':'○ Disabled';
    head.append(title,tag);
    const body=document.createElement('p'); body.textContent=rule.instruction;
    const actions=document.createElement('div'); actions.className='rule-actions';
    actions.append(button('Edit',()=>edit(rule)),button(rule.enabled?'Disable':'Enable',async()=>{
      await api('save',{...rule,enabled:!rule.enabled}); invalidate(); await refresh(); status('Library updated. Preview and apply to update Codex.');
    }),button('Delete',async()=>{
      if (!confirm('Delete this rule from your local library? Apply afterward to remove it from Codex.')) return;
      await api('delete',{id:rule.id}); invalidate(); await refresh(); status('Rule deleted locally. Apply to update Codex.');
    },'danger'));
    item.append(head,body,actions); list.append(item);
  });
}
$('draft').addEventListener('click',()=>run(async()=>{
  $('draft').disabled=true;
  try { const draft=await api('draft',{source:$('source').value}); edit(draft); status(draft.note); }
  finally { $('draft').disabled=false; }
}));
$('editor').addEventListener('submit',event=>{event.preventDefault();run(async()=>{
  await api('save',{id:editing,title:$('title').value,source,instruction:$('instruction').value,enabled:$('enabled').checked});
  invalidate(); $('editor').hidden=true; $('source').value=''; editing=null; await refresh(); status('Saved on your machine. Preview and apply when you are ready.');
});});
$('cancel').addEventListener('click',()=>{$('editor').hidden=true;editing=null;});
$('preview').addEventListener('click',()=>run(async()=>{
  proposal=await api('preview',{}); $('before').textContent=proposal.before || '(No existing instructions)';
  $('after').textContent=proposal.after || '(Empty file)'; $('preview-info').textContent=proposal.active_count+' enabled rules · '+proposal.warning;
  $('apply').disabled=!proposal.changed; $('preview-panel').hidden=false; $('preview-panel').scrollIntoView({behavior:'smooth',block:'start'});
  status(proposal.changed?'Review the exact file update below.':'Project instructions already match the library.');
}));
$('apply').addEventListener('click',()=>run(async()=>{
  if (!proposal) return; $('apply').disabled=true;
  try {
    const result=await api('apply',{revision:proposal.revision}); invalidate();
    status('Applied to '+result.path+'. Start a new Codex session in that project.'+(result.backup?'\nBackup: '+result.backup:''));
  } catch(error) { invalidate(); throw error; }
}));
$('close-preview').addEventListener('click',invalidate);
$('export').addEventListener('click',()=>run(async()=>{
  await refresh(); const blob=new Blob([JSON.stringify({schema_version:1,rules:state.rules},null,2)],{type:'application/json'});
  const url=URL.createObjectURL(blob); const a=document.createElement('a');a.href=url;a.download='rulebox-library.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  status('Library exported. It can contain private instructions; share intentionally.');
}));
run(refresh);
