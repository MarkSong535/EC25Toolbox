'use strict';
const $ = id => document.getElementById(id);
const state = {messages:new Map(), selected:null, csrf:'', cursors:{in:null,out:null}, request:null, loginCursor:null, archiveCursor:null};
const narrowScreen=window.matchMedia('(max-width:600px)');
function sidebar(open,restoreFocus=false) {
  open=narrowScreen.matches&&open;
  document.body.classList.toggle('sidebar-open',open);
  $('menu-toggle').setAttribute('aria-expanded',String(open));
  $('menu-toggle').setAttribute('aria-label',open?'Close conversations':'Open conversations');
  $('sidebar-backdrop').hidden=!open;
  $('sidebar').inert=narrowScreen.matches&&!open;
  document.querySelector('.workspace').inert=open;
  if(restoreFocus) $('menu-toggle').focus();
}
$('menu-toggle').addEventListener('click',()=>sidebar(!document.body.classList.contains('sidebar-open')));
$('sidebar-backdrop').addEventListener('click',()=>sidebar(false,true));
document.addEventListener('keydown',event=>{
  if(!document.body.classList.contains('sidebar-open')) return;
  if(event.key==='Escape') {event.preventDefault();sidebar(false,true);}
  if(event.key==='Tab') {
    const controls=[$('menu-toggle'),$('connection'),...$('sidebar').querySelectorAll('button:not(:disabled),input')];
    const index=controls.indexOf(document.activeElement);
    event.preventDefault();controls[(index+(event.shiftKey?-1:1)+controls.length)%controls.length].focus();
  }
});
narrowScreen.addEventListener('change',()=>{sidebar(false);if(narrowScreen.matches&&$('sidebar').contains(document.activeElement))$('menu-toggle').focus();});
sidebar(false);
for(const id of ['new','archives-toggle','connection']) $(id).addEventListener('click',()=>sidebar(false,narrowScreen.matches));
const date = stamp => new Date(stamp * 1000).toLocaleString();
function text(tag, value, cls) { const node=document.createElement(tag); node.textContent=value; if(cls)node.className=cls; return node; }
async function api(path, options={}) {
  const response=await fetch(path, {credentials:'same-origin', cache:'no-store', ...options});
  if(response.status===401 || response.redirected) throw new Error('Session expired. Reload this page to sign in again.');
  const result=await response.json();
  if(!response.ok) throw new Error(result.error || 'Request failed');
  return result;
}
function notice(message) { $('notice').textContent=message; }
function select(number) {
  sidebar(false,narrowScreen.matches);
  state.selected=number; $('recipient').value=number; $('title').textContent=number;
  $('subtitle').textContent=''; $('audit-panel').hidden=true; $('logins-panel').hidden=true;
  $('compose').hidden=false; $('timeline').hidden=false; render();
}
function render() {
  const query=$('search').value.toLowerCase(); const groups=new Map();
  for(const m of [...state.messages.values()].sort((a,b)=>b.created_at-a.created_at)) {
    if(!groups.has(m.number)) groups.set(m.number, []);
    groups.get(m.number).push(m);
  }
  $('contacts').replaceChildren();
  for(const [number, messages] of groups) {
    if(query && !number.toLowerCase().includes(query) && !messages.some(m=>m.body.toLowerCase().includes(query))) continue;
    const button=text('button','','contact'+(number===state.selected?' selected':''));
    button.append(text('strong',number),text('p',messages[0].body));
    button.addEventListener('click',()=>select(number)); $('contacts').append(button);
  }
  if(!state.selected) return;
  const timeline=$('timeline'); const atBottom=timeline.scrollHeight-timeline.scrollTop-timeline.clientHeight<80;
  timeline.replaceChildren();
  const messages=(groups.get(state.selected)||[]).slice().sort((a,b)=>a.created_at-b.created_at);
  if(!messages.length) timeline.append(text('p','Start this conversation with a message.','empty'));
  for(const m of messages) {
    const row=text('article','','message '+m.direction);
    const status=m.direction==='out'? ({sent:'Accepted by modem · recipient delivery not confirmed',unknown:'Outcome unknown · not retried',sending:'Submitting…',queued:'Queued'}[m.status]||m.status):'Received';
    row.append(text('div',m.body,'bubble'),text('div',`${date(m.created_at)} · ${status}`,'meta')); timeline.append(row);
  }
  if(atBottom) timeline.scrollTop=timeline.scrollHeight;
}
async function load(older=false) {
  for(const direction of ['in','out']) {
    if(older && !state.cursors[direction]) continue;
    const cursor=older?'&before='+state.cursors[direction]:'';
    const result=await api('/api/messages?direction='+direction+cursor);
    for(const m of result.items) state.messages.set(m.id,m);
    if(older || state.cursors[direction]===null) state.cursors[direction]=result.next_before;
  }
  $('older').disabled=!state.cursors.in&&!state.cursors.out; render();
}
$('search').addEventListener('input',render);
$('body').addEventListener('input',()=>{ state.request=null; });
$('recipient').addEventListener('input',()=>{state.request=null;});
$('new').addEventListener('click',()=>{state.selected=null;state.request=null;$('recipient').value='';$('body').value='';$('title').textContent='New message';$('subtitle').textContent='Use a full international phone number.';$('timeline').replaceChildren();$('timeline').hidden=false;$('compose').hidden=false;$('audit-panel').hidden=true;$('logins-panel').hidden=true;$('recipient').focus();render();});
$('older').addEventListener('click',()=>load(true).catch(e=>notice(e.message)));
$('audit-older').addEventListener('click',()=>loadArchives(true).catch(e=>notice(e.message)));
async function loadArchives(older=false) {
  const result=await api('/api/archives'+(older&&state.archiveCursor?'?before='+state.archiveCursor:''));
  if(!older) $('audit-items').replaceChildren();
  if(!older&&!result.items.length) $('audit-items').append(text('p','No verified Proton Drive archives yet.'));
  for(const e of result.items) {
    const row=text('article','','audit-entry');
    const link=text('a',e.name,'archive-link');link.href='https://drive.proton.me/';link.target='_blank';link.rel='noopener noreferrer';
    link.title='Open Proton Drive';
    row.append(link,text('p',`${date(e.created_at)} · ${(e.size/1024).toFixed(1)} KB · Upload verified`),text('p','SHA-256: '+e.sha256));
    $('audit-items').append(row);
  }
  state.archiveCursor=result.next_before;$('audit-older').disabled=!state.archiveCursor;
}
$('archives-toggle').addEventListener('click',()=>{$('title').textContent='Archives';$('subtitle').textContent='Verified cloud copies';$('audit-older').textContent='Load older archives';$('logins-panel').hidden=true;$('audit-panel').hidden=false;$('compose').hidden=true;$('timeline').hidden=true;loadArchives().catch(e=>notice(e.message));});
async function loadLogins(older=false) {
  const result=await api('/api/logins'+(older&&state.loginCursor?'?before='+state.loginCursor:''));
  if(!older) $('logins-items').replaceChildren();
  for(const entry of result.items) {
    const row=text('article','','login-entry');
    const when=text('div',date(entry.created_at)); when.append(text('small','First seen'));
    const who=text('div',entry.actor); who.append(text('small',entry.identity_source));
    const client=text('div',entry.client_type); client.append(text('small',entry.browser));
    row.append(when,who,client); $('logins-items').append(row);
  }
  if(!older&&!result.items.length) $('logins-items').append(text('p','No sessions recorded yet.'));
  state.loginCursor=result.next_before;$('logins-older').disabled=!state.loginCursor;
}
$('connection').addEventListener('click',()=>{$('title').textContent='Access logs';$('subtitle').textContent='';$('logins-panel').hidden=false;$('audit-panel').hidden=true;$('compose').hidden=true;$('timeline').hidden=true;loadLogins().catch(e=>notice(e.message));});
$('logins-older').addEventListener('click',()=>loadLogins(true).catch(e=>notice(e.message)));
$('compose').addEventListener('submit',async event=>{
  event.preventDefault(); const number=$('recipient').value.trim(), body=$('body').value;
  if(!/^\+[1-9][0-9]{6,14}$/.test(number)||!body.trim()||body.length>70||/[\uD800-\uDFFF\x00-\x08\x0b-\x1f\x7f]/.test(body)) {notice('Use an international number and up to 70 characters, without emoji or control characters.');return;}
  if(!window.confirm(`Send this SMS to ${number}? Carrier charges may apply.`)) return;
  $('send').disabled=true;
  // Keep the same key on network errors: retrying cannot queue a duplicate.
  state.request ||= crypto.randomUUID();
  try {
    await api('/api/send',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':state.csrf},body:JSON.stringify({request_id:state.request,number,body})});
    state.request=null;$('body').value='';notice('Queued. The message status will update below.');select(number);await load();
  } catch(error) {notice(error.message);} finally {$('send').disabled=false;}
});
async function refresh() {
  try {const session=await api('/api/session');state.csrf=session.csrf;simStatus(session.modem_ready,session.modem_ready?'connected':'offline');$('send').disabled=!session.modem_ready;await load();}
  catch(error) {notice(error.message);simStatus(false,'connection unavailable');$('send').disabled=true;}
  finally {setTimeout(refresh,10000);}
}
function simStatus(ready,label) {const dot=$('connection');dot.classList.toggle('offline',!ready);dot.title='SIM Status: '+label;dot.setAttribute('aria-label',dot.title+'. Open access logs.');}
refresh();
