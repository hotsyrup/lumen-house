const notice=document.querySelector('#notice');
const form=document.querySelector('#proposal');
const records=document.querySelector('#records');
const say=message=>{notice.textContent=message;};
async function api(path,options={}) { const response=await fetch(path,options);const body=await response.json();if(!response.ok)throw new Error(body.error);return body; }
const pendingKeys=new Map();
async function command(value) {const body=JSON.stringify(value);const key=pendingKeys.get(body)||crypto.randomUUID();pendingKeys.set(body,key);const result=await api('/api/exchange/commands',{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':key},body});pendingKeys.delete(body);return result;}
function download(data,name){const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
function element(tag,text){const node=document.createElement(tag);node.textContent=text;return node;}
function ask(message,{text=false}={}) {
 return new Promise(resolve=>{
  const dialog=element('dialog','');const modal=element('form','');modal.method='dialog';
  const title=element('h2',message);title.id='review-dialog-title';dialog.setAttribute('aria-labelledby',title.id);
  modal.append(title,element('p','Synthetic local review only. This grants no real authority or commitment.'));
  const input=element('input','');input.name='reviewText';input.maxLength=1000;input.required=text;
  if(text){const label=element('label','Fictional review details');label.append(input);modal.append(label);}
  const actions=element('div','');actions.className='actions';
  const cancel=element('button','Cancel');cancel.type='button';cancel.onclick=()=>dialog.close('cancel');
  const confirm=element('button',text?'Save fictional details':'Confirm local review');confirm.type='submit';
  actions.append(cancel,confirm);modal.append(actions);dialog.append(modal);document.body.append(dialog);
  modal.onsubmit=event=>{event.preventDefault();dialog.close('confirmed');};
  dialog.addEventListener('close',()=>{const value=dialog.returnValue==='confirmed'?(text?input.value.trim():true):(text?null:false);dialog.remove();resolve(value);},{once:true});
  dialog.showModal();if(text)input.focus();else cancel.focus();
 });
}
async function refresh(){
 const proposals=await api('/api/exchange/proposals');records.replaceChildren();
 if(!proposals.length)records.append(element('p','No proposals yet. Start with a fictional need.'));
 for(const p of proposals){
  const card=element('article','');card.append(element('h3',`${p.agent} · ${p.state}`),element('p',`${p.currentTask} — ${p.terms.description}`),element('p',`Reserved demo units: ${p.reservedUnits}. Expires ${p.expiresAt}.`));
  card.append(element('p',p.matches.length?`Possible scope matches (not availability): ${p.matches.map(m=>m.title).join('; ')}`:'No verified catalog scope match. Escalate for owner review; do not invent an offering.'));
  if(p.escalation)card.append(element('p',`Owner review required: ${p.escalation.reason}`));
  const details=element('details','');details.append(element('summary','Inspect proposal, authority, provenance and separate evaluation'),element('pre',JSON.stringify(p,null,2)));card.append(details);
  const actions=element('div','');actions.className='actions';
  const add=(label,run)=>{const button=element('button',label);button.type='button';button.onclick=async()=>{button.disabled=true;try{await run();await refresh();say('Local review finished. No real-world action taken.');}catch(e){say(e.message);}finally{button.disabled=false;}};actions.append(button);};
  const act=(action,actor,input)=>command({action,actor,id:p.id,input});
  if(['proposed','counteroffered'].includes(p.state)){
   add('Counteroffer as provider',async()=>{const description=await ask('Fictional counteroffer terms',{text:true});if(description)await act('counteroffer','provider',{description});});
   add('Record agreement as owner',async()=>{if(await ask('Simulate both parties confirming these exact terms? No actual commitments or spend.'))await act('agree','owner',{requesterConfirmed:true,providerConfirmed:true,expectedTermsRevision:p.termsRevision});});
  }
  if(p.state==='agreed')add('Record delivery as provider',async()=>{const evidence=await ask('Synthetic evidence and how to verify it (no private links)',{text:true});if(evidence)await act('deliver','provider',{evidence});});
  if(p.state==='delivered')add('Accept as requester',async()=>{const assessment=await ask('Describe verification and acceptance',{text:true});if(assessment)await act('accept','requester',{assessment});});
  if(p.state==='accepted')add('Complete as owner',()=>act('complete','owner',{reason:'Demo verification and acceptance recorded'}));
  if(!['completed','cancelled'].includes(p.state)){
   add('Escalate to owner',async()=>{const reason=await ask('Reason for owner review',{text:true});if(reason)await act('escalate','requester',{reason});});
   add('Cancel demo proposal',async()=>{if(await ask('Cancel and release hypothetical reservation?'))await act('cancel','requester',{reason:'Cancelled in local demo'});});
  }
  add('Export local record',async()=>{const data=await api(`/api/exchange/proposals/${p.id}`);download(data,`exchange-demo-${p.id}.json`);});
  add('Download intake review draft',async()=>{if(await ask('Export an incomplete synthetic intake draft for local owner review? This submits nothing to House. Exported copies require separate disposal.')){const data=await api(`/api/exchange/proposals/${p.id}/intake-draft`,{method:'POST',headers:{'X-Exchange-Local-Export':'confirmed'}});download(data,`house-intake-review-${p.id}.json`);}});
  add('Delete local record',async()=>{if(await ask('Delete this fictional proposal, audit, and replay data? This cannot be undone.'))await api(`/api/exchange/proposals/${p.id}`,{method:'DELETE'});});card.append(actions);records.append(card);
 }
}
form.onsubmit=async event=>{event.preventDefault();const button=form.querySelector('button');button.disabled=true;const f=new FormData(form);try{await command({action:'propose',actor:'requester',input:{mode:'demo',agent:f.get('agent'),currentTask:f.get('currentTask'),needs:[f.get('need')],missingResources:[f.get('resource')],offers:[f.get('offer')],authority:'proposal-only',terms:{kind:f.get('kind'),description:f.get('terms'),units:Number(f.get('units'))},evaluation:{cost:f.get('cost'),usefulness:f.get('usefulness'),verifiability:f.get('verifiability')},sharingConsent:f.get('consent')==='on',provenance:{source:'User-entered fictional local example',rights:'synthetic-demo-only'}}});await refresh();say('Demo proposal saved locally. No House request submitted.');}catch(e){say(e.message);}finally{button.disabled=false;}};
refresh().catch(e=>say(e.message));

