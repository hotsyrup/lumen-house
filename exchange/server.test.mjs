import test from 'node:test';
import assert from 'node:assert/strict';
import { createExchangeServer } from './server.mjs';
import { openStore } from './store.mjs';
import { matchCapabilities } from './capabilities.mjs';
async function setup(t, options) {const store=openStore();const server=createExchangeServer(store,options);await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));t.after(async()=>{await new Promise(resolve=>server.close(resolve));store.close();});return `http://127.0.0.1:${server.address().port}`;}

test('standalone review does not label its own root as the House catalog', async t => {
 const origin=await setup(t);
 for(const path of ['/', '/exchange/']) {
  const response=await fetch(origin+path);
  assert.equal(response.status,200);
  const html=await response.text();
  assert.match(html,/Local demo only/);
  assert.match(html,/Standalone local demo/);
  assert.doesNotMatch(html,/<a\b[^>]*href=["']\/["'][^>]*>[^<]*House catalog/i);
 }
});

test('injected House retains catalog navigation and existing HTTP requests and responses', async t => {
 const seen=[];
 const house={async fetch(request) {
  const url=new URL(request.url);
  const body=await request.text();
  seen.push({path:url.pathname+url.search,method:request.method,body,client:request.headers.get('x-existing-client')});
  return new Response(url.pathname==='/'?'<h1>Existing House catalog</h1>':`Existing response: ${body}`,{
   status:url.pathname==='/'?203:207,
   headers:{'Content-Type':url.pathname==='/'?'text/html':'text/plain','X-Existing-Policy':'preserved'},
  });
 }};
 const origin=await setup(t,{house});
 const review=await fetch(origin+'/exchange/');
 assert.equal(review.status,200);
 assert.match(await review.text(),/<a\b[^>]*href=["']\/["'][^>]*>[^<]*House catalog/i);
 assert.deepEqual(seen,[]);
 const catalog=await fetch(origin+'/?catalog=owned');
 assert.equal(catalog.status,203);
 assert.equal(catalog.headers.get('x-existing-policy'),'preserved');
 assert.equal(await catalog.text(),'<h1>Existing House catalog</h1>');
 const existing=await fetch(origin+'/api/proposals?keep=query',{
  method:'POST',headers:{'Content-Type':'application/octet-stream','X-Existing-Client':'same value',origin:'https://existing-client.example'},
  body:'Existing HTTP body café',
 });
 assert.equal(existing.status,207);
 assert.equal(existing.headers.get('x-existing-policy'),'preserved');
 assert.equal(await existing.text(),'Existing response: Existing HTTP body café');
 assert.deepEqual(seen,[
  {path:'/?catalog=owned',method:'GET',body:'',client:null},
  {path:'/api/proposals?keep=query',method:'POST',body:'Existing HTTP body café',client:'same value'},
 ]);
});
test('local API serves demo policy and UI with defensive headers',async t=>{
 const origin=await setup(t);let r=await fetch(origin);assert.equal(r.status,200);assert.match(r.headers.get('content-security-policy'),/frame-ancestors 'none'/);assert.match(await r.text(),/Local demo only/);
 r=await fetch(origin+'/api/policy');assert.equal((await r.json()).realEnabled,false);
 for(const path of ['/ui.mjs','/style.css','/api/capabilities','/api/proposals'])assert.equal((await fetch(origin+path)).status,200);
 assert.equal((await fetch(origin+'/../store.mjs')).status,404);
});
test('cross origin, DNS rebinding and absent-origin mutations are denied',async t=>{
 const origin=await setup(t);
 for(const headers of [{origin:'https://evil.example'},{host:'evil.example'},{}]){
  const r=await fetch(origin+'/api/commands',{method:'POST',headers,body:'{}'});assert.equal(r.status,403);
 }
});
test('bad JSON, unsupported content and oversized payloads fail closed',async t=>{
 const origin=await setup(t);
 let r=await fetch(origin+'/api/commands',{method:'POST',headers:{origin,'content-type':'text/plain'},body:'{}'});assert.equal(r.status,415);
 r=await fetch(origin+'/api/commands',{method:'POST',headers:{origin,'content-type':'application/json'},body:'{'});assert.equal(r.status,400);
 r=await fetch(origin+'/api/commands',{method:'POST',headers:{origin,'content-type':'application/json'},body:'x'.repeat(16001)});assert.equal(r.status,413);
});
test('scope matching does not create inventory, pricing or human availability',()=>{
 assert.equal(matchCapabilities(['Buy guaranteed distribution']).length,0);
 assert.equal(matchCapabilities(['Review SHA-256 manifest'])[0].availability,'must-reverify');
 assert.equal(matchCapabilities(['human support'])[0].availability,'written-quote-required');
});
test('prototype property action is rejected rather than crashing',()=>{
 const store=openStore();try{assert.throws(()=>store.execute({action:'toString',actor:'owner',id:'missing'},'test'),e=>e.status===404);}finally{store.close();}
});

test('chunked UTF-8 is decoded only after complete byte collection', async t => {
 const {request}=await import('node:http'); const origin=await setup(t);
 const command={action:'propose',actor:'requester',input:{mode:'demo',agent:'Synthetic café agent',currentTask:'Fictional task',needs:['manifest'],missingResources:['check'],offers:['test'],authority:'proposal-only',terms:{kind:'barter',description:'Fictional exchange',units:1},evaluation:{cost:'Unknown',usefulness:'Demo',verifiability:'Test'},sharingConsent:true,provenance:{source:'Synthetic test',rights:'synthetic-demo-only'}}};
 const buffer=Buffer.from(JSON.stringify(command)); const split=buffer.indexOf(Buffer.from('é'))+1;
 const result=await new Promise((resolve,reject)=>{const req=request(origin+'/api/commands',{method:'POST',headers:{origin,'content-type':'application/json','idempotency-key':'unicode'}},res=>{let body='';res.on('data',chunk=>body+=chunk);res.on('end',()=>resolve({status:res.statusCode,body:JSON.parse(body)}));});req.on('error',reject);req.write(buffer.subarray(0,split));setTimeout(()=>req.end(buffer.subarray(split)),10);});
 assert.equal(result.status,200);assert.equal(result.body.agent,'Synthetic café agent');
});
