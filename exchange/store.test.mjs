import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { createHash } from 'node:crypto';
import { openStore, POLICY } from './store.mjs';

function input(overrides = {}) {
  return {mode: 'demo', agent: 'Synthetic requester', currentTask: 'Verify synthetic file delivery', needs: ['File integrity evidence'], missingResources: ['Manifest verification'], offers: ['Synthetic interface metadata review'], authority: 'proposal-only', terms: {kind: 'barter', description: 'Proposed review for manifest, not an actual commitment', units: 1}, evaluation: {cost: 'Not estimated', usefulness: 'Catch omitted synthetic files', verifiability: 'Compare SHA-256 and listed sizes'}, sharingConsent: true, provenance: {source: 'Synthetic test fixture', rights: 'synthetic-demo-only'}, ...overrides};
}
function harness(t, path) {
  const store = openStore(path);
  t.after(() => store.close());
  let n = 0;
  const send = (action, id, actor, payload) => store.execute({action, id, actor, input: payload}, `test-${++n}`);
  const propose = (overrides) => send('propose', undefined, 'requester', input(overrides));
  const agree = id => send('agree', id, 'owner', {requesterConfirmed: true, providerConfirmed: true, expectedTermsRevision: store.export(id).proposal.termsRevision});
  return {store, send, propose, agree};
}
function error(status, pattern) { return e => e.status === status && pattern.test(e.message); }

test('demo policy cannot enable treasury, payout, private access or real exchange', () => {
  assert.equal(POLICY.mode, 'demo');
  for (const key of ['realEnabled','treasuryEnabled','automaticPayouts','privateAccess']) assert.equal(POLICY[key], false);
  assert.ok(Object.isFrozen(POLICY));
});

test('full lifecycle keeps distinct evaluations, agreed terms, evidence, acceptance and audit', t => {
  const {store, send, propose, agree} = harness(t);
  const p = propose();
  assert.equal(p.state, 'proposed');
  assert.equal(p.reservedUnits, 0);
  assert.deepEqual(p.evaluation, input().evaluation);
  const counter = send('counteroffer',p.id,'provider',{description: 'Narrow scope to listed synthetic files'});
  assert.equal(counter.state, 'counteroffered');
  const agreement = agree(p.id);
  assert.equal(agreement.reservedUnits, 1);
  assert.equal(agreement.agreement.terms.description, counter.terms.description);
  assert.throws(() => send('complete',p.id,'owner',{reason:'Skip evidence'}), error(409,/transition/));
  assert.equal(send('deliver',p.id,'provider',{evidence:'Synthetic manifest: all 2 hashes match'}).state,'delivered');
  assert.equal(send('accept',p.id,'requester',{assessment:'Independently checked both synthetic hashes'}).state,'accepted');
  const completed = send('complete',p.id,'owner',{reason:'Both demo records checked'});
  assert.equal(completed.state,'completed');
  assert.equal(completed.reservedUnits,0);
  const exported = store.export(p.id);
  assert.deepEqual(exported.events.map(e=>e.action),['propose','counteroffer','agree','deliver','accept','complete']);
  assert.equal(exported.events[0].snapshot.terms.description,input().terms.description);
  assert.throws(() => send('cancel',p.id,'owner',{reason:'Already completed'}),error(409,/transition/));
});

test('roles and explicit two-party confirmation are required', t => {
  const {store, send, propose, agree} = harness(t);
  assert.throws(() => store.execute({action:'propose',actor:'owner',input:input()},'owner-proposal'),error(403,/requester/));
  const p = propose();
  assert.throws(() => send('agree',p.id,'requester',{requesterConfirmed:true,providerConfirmed:true}),error(403,/actor/));
  assert.throws(() => send('agree',p.id,'owner',{requesterConfirmed:true,providerConfirmed:false}),error(400,/Both/));
  agree(p.id);
  assert.throws(() => send('deliver',p.id,'requester',{evidence:'Synthetic evidence'}),error(403,/actor/));
  send('deliver',p.id,'provider',{evidence:'Synthetic evidence'});
  assert.throws(() => send('accept',p.id,'provider',{assessment:'Self acceptance'}),error(403,/actor/));
  assert.equal(store.export(p.id).events.length,3);
});

test('real mode, execution authority, absent consent, private rights and excess fields fail closed', t => {
  const {store, propose} = harness(t);
  const invalid = [
    [{mode:'real'},403,/disabled/],
    [{authority:'spend'},403,/authority/],
    [{sharingConsent:false},400,/consent/],
    [{provenance:{source:'private',rights:'resale'}},400,/synthetic/],
    [{privateToken:'do not collect'},400,/Unknown/],
    [{terms:{kind:'cash',description:'Payment',units:1}},400,/kind/],
    [{terms:{kind:'barter',description:'Demo',units:0}},400,/cap/],
    [{terms:{kind:'barter',description:'Demo',units:6}},400,/cap/],
    [{terms:{kind:'barter',description:'Demo',units:1.5}},400,/cap/],
    [{needs:[]},400,/needs/],
    [{agent:' '},400,/agent/],
    [{evaluation:{cost:'unknown',usefulness:'unknown',verifiability:''}},400,/verifiability/]
  ];
  for (const [overrides,status,pattern] of invalid) assert.throws(()=>propose(overrides),error(status,pattern));
  assert.deepEqual(store.list(),[]);
});

test('idempotency replays original response without additional audit or proposal', t => {
  const {store,send} = harness(t);
  const command = {action:'propose',actor:'requester',input:input()};
  const p = store.execute(command,'stable-key');
  send('counteroffer',p.id,'provider',{description:'Different synthetic terms'});
  assert.deepEqual(store.execute(command,'stable-key'),p);
  assert.equal(store.list().length,1);
  assert.equal(store.export(p.id).events.length,2);
  assert.throws(()=>store.execute({...command,input:input({agent:'Different synthetic agent'})},'stable-key'),error(409,/Idempotency/));
  assert.throws(()=>store.execute(command,''),error(400,/key/));
});

test('reservations are atomic, bounded, not duplicated, and released by cancellation', t => {
  const {store,send,propose,agree} = harness(t);
  const five = {terms:{kind:'service-credit-proposal',description:'Five synthetic capacity units, no balance',units:5}};
  const a = propose(five), b = propose(five), c = propose();
  const command = {action:'agree',id:a.id,actor:'owner',input:{requesterConfirmed:true,providerConfirmed:true,expectedTermsRevision:1}};
  const first = store.execute(command,'agreement-once');
  assert.deepEqual(store.execute(command,'agreement-once'),first);
  agree(b.id);
  assert.throws(()=>agree(c.id),error(409,/reservation cap/));
  assert.equal(store.export(c.id).proposal.state,'proposed');
  assert.equal(store.export(c.id).events.length,1);
  assert.equal(store.list().reduce((n,p)=>n+p.reservedUnits,0),10);
  send('cancel',a.id,'requester',{reason:'Withdraw synthetic request'});
  assert.equal(agree(c.id).state,'agreed');
  assert.equal(store.list().reduce((n,p)=>n+p.reservedUnits,0),6);
});

test('open proposal cap is bounded and closed records no longer occupy capacity', t => {
  const {propose,send,store} = harness(t);
  const proposals = Array.from({length:POLICY.maxOpen},()=>propose());
  assert.throws(()=>propose(),error(409,/Open proposal cap/));
  send('cancel',proposals[0].id,'owner',{reason:'Demo closed'});
  assert.equal(propose().state,'proposed');
  assert.equal(store.list().length,POLICY.maxOpen+1);
});

test('owner escalation records a review requirement without spending or transition', t => {
  const {store,send,propose} = harness(t);
  const p = propose();
  const result = send('escalate',p.id,'provider',{reason:'Need owner to verify scope'});
  assert.equal(result.state,'proposed');
  assert.equal(result.reservedUnits,0);
  assert.equal(result.escalation.status,'owner-review-required');
  assert.deepEqual(store.export(p.id).events.map(e=>e.action),['propose','escalate']);
});

test('SQLite reopen preserves state, reservations, audit and idempotent responses', t => {
  const dir = mkdtempSync(join(tmpdir(),'house-exchange-test-'));
  t.after(()=>rmSync(dir,{recursive:true,force:true}));
  const path = join(dir,'test.sqlite');
  let store = openStore(path);
  const command = {action:'propose',actor:'requester',input:input()};
  const p = store.execute(command,'durable-proposal');
  const agreement = {action:'agree',actor:'owner',id:p.id,input:{requesterConfirmed:true,providerConfirmed:true,expectedTermsRevision:1}};
  store.execute(agreement,'durable-agreement');
  store.close();
  store = openStore(path);
  try {
    assert.deepEqual(store.execute(command,'durable-proposal'),p);
    assert.equal(store.export(p.id).proposal.state,'agreed');
    assert.equal(store.export(p.id).proposal.reservedUnits,1);
    assert.equal(store.export(p.id).events.length,2);
    store.execute(agreement,'durable-agreement');
    assert.equal(store.export(p.id).events.length,2);
  } finally {store.close();}
});

test('delete removes proposal, content audit and idempotency data', t => {
  const dir = mkdtempSync(join(tmpdir(),'house-exchange-delete-'));
  t.after(()=>rmSync(dir,{recursive:true,force:true}));
  const path = join(dir,'test.sqlite');
  const store = openStore(path);
  const p = store.execute({action:'propose',actor:'requester',input:input()},'delete-me');
  store.delete(p.id);
  assert.throws(()=>store.export(p.id),error(404,/not found/));
  assert.deepEqual(store.list(),[]);
  assert.throws(()=>store.delete(p.id),error(404,/not found/));
  store.close();
  const db = new DatabaseSync(path);
  try { for(const table of ['proposals','events','requests']) assert.equal(db.prepare(`SELECT COUNT(*) AS n FROM ${table}`).get().n,0); }
  finally {db.close();}
});

test('retention purge removes expired data and frees reservations', t => {
  const {store,propose,agree} = harness(t);
  const p = propose();
  agree(p.id);
  assert.equal(store.purge('2000-01-01T00:00:00.000Z'),0);
  assert.equal(store.purge('2100-01-01T00:00:00.000Z'),1);
  assert.deepEqual(store.list(),[]);
  assert.throws(()=>store.export(p.id),error(404,/not found/));
});

 test('stale viewed terms cannot be agreed, and inherited action names are rejected', t => {
  const {store, propose, send} = harness(t); const p=propose();
  send('counteroffer',p.id,'provider',{description:'Changed synthetic terms'});
  assert.throws(()=>send('agree',p.id,'owner',{requesterConfirmed:true,providerConfirmed:true,expectedTermsRevision:p.termsRevision}),error(409,/Terms changed/));
  assert.equal(store.export(p.id).proposal.reservedUnits,0);
  assert.throws(()=>send('toString',p.id,'owner',{}),error(409,/transition/));
 });


test('legacy SHA-256 replay rows survive the portable-core upgrade', t => {
  const dir = mkdtempSync(join(tmpdir(), 'house-exchange-legacy-'));
  t.after(() => rmSync(dir, {recursive: true, force: true}));
  const path = join(dir, 'test.sqlite');
  const command = {action: 'propose', actor: 'requester', input: input()};
  let store = openStore(path);
  const proposal = store.execute(command, 'legacy-key');
  store.close();
  const db = new DatabaseSync(path);
  db.prepare('UPDATE requests SET fingerprint=? WHERE key=?').run(createHash('sha256').update(JSON.stringify(command)).digest('hex'), 'legacy-key');
  db.exec('PRAGMA user_version=0');
  db.close();
  store = openStore(path);
  try {
    assert.deepEqual(store.execute(command, 'legacy-key'), proposal);
    assert.equal(store.export(proposal.id).events.length, 1);
    assert.throws(() => store.execute({...command, input: input({agent: 'Changed intent'})}, 'legacy-key'), error(409, /Idempotency/));
  } finally {store.close();}
});

test('unknown SQLite schema version is rejected before changing its data', t => {
  const dir = mkdtempSync(join(tmpdir(), 'house-exchange-version-'));
  t.after(() => rmSync(dir, {recursive: true, force: true}));
  const path = join(dir, 'test.sqlite');
  let db = new DatabaseSync(path);
  db.exec('CREATE TABLE preserved(value TEXT); INSERT INTO preserved VALUES(\'synthetic preserved data\'); PRAGMA user_version=2;');
  db.close();
  assert.throws(() => openStore(path), error(503, /schema/));
  db = new DatabaseSync(path);
  try {
    assert.equal(db.prepare('PRAGMA user_version').get().user_version, 2);
    assert.equal(db.prepare('SELECT value FROM preserved').get().value, 'synthetic preserved data');
    assert.equal(db.prepare("SELECT COUNT(*) AS n FROM sqlite_master WHERE name='proposals'").get().n, 0);
  } finally {db.close();}
});
