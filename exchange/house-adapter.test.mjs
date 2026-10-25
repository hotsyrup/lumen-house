import test from 'node:test';
import assert from 'node:assert/strict';
import { withHouseExchange } from './house-adapter.mjs';
import { openStore } from './store.mjs';

const origin = 'http://127.0.0.1:8788';
const demoPolicy = Object.freeze({ mode: 'demo', realEnabled: false, treasuryEnabled: false, automaticPayouts: false, privateAccess: false, serviceCreditEnabled: false, barterExecutionEnabled: false });
const assets = { 'index.html': '<h1>Synthetic exchange review</h1>', 'ui.mjs': '/* local review */', 'style.css': 'body { color: white; }' };

function request(path, options = {}) {
  return new Request(origin + path, { ...options, headers: { host: '127.0.0.1:8788', ...options.headers } });
}

function fixture(overrides = {}) {
  const calls = [];
  const store = {
    policy: demoPolicy,
    list() { calls.push('list'); return []; },
    execute(command, key) { calls.push({ command, key }); return { synthetic: true }; },
    export(id) { calls.push({ export: id }); return { synthetic: true }; },
    delete(id) { calls.push({ delete: id }); },
    ...overrides,
  };
  const house = { fetch() { calls.push('house'); return new Response('Existing House', { status: 202 }); } };
  return { calls, store, house, adapter: withHouseExchange(house, { review: { origin, assets, store } }) };
}

function post(path = '/api/exchange/commands', body = '{}', headers = {}) {
  return request(path, { method: 'POST', body, duplex: 'half', headers: { origin, 'content-type': 'application/json', 'idempotency-key': 'synthetic-1', ...headers } });
}

function syntheticCommand() {
  return { action: 'propose', actor: 'requester', input: {
    mode: 'demo', agent: 'Synthetic agent', currentTask: 'Fictional review',
    needs: ['manifest verification'], missingResources: ['test cases'], offers: ['synthetic examples'],
    authority: 'proposal-only', terms: { kind: 'service-credit-proposal', description: 'Proposed synthetic file check', units: 1 },
    evaluation: { cost: 'Unknown', usefulness: 'Reduce ambiguity', verifiability: 'Synthetic assertions' },
    sharingConsent: true, provenance: { source: 'Synthetic test', rights: 'synthetic-demo-only' },
  } };
}

test('disabled composition delegates all requests without reading flags or exchange resources', async () => {
  const env = new Proxy({}, { get() { throw new Error('Environment must not be inspected'); } });
  const ctx = {};
  const seen = [];
  const house = {
    fetch(req, actualEnv, actualCtx) {
      assert.equal(this, house);
      assert.equal(actualEnv, env);
      assert.equal(actualCtx, ctx);
      assert.equal(req.bodyUsed, false);
      const result = new Response('Existing response bytes', { status: 418, headers: { 'x-existing-policy': 'preserved' } });
      seen.push({ req, result });
      return result;
    },
  };
  const adapter = withHouseExchange(house, { get store() { throw new Error('Unused exchange resource'); } });
  for (const path of ['/', '/exchange/', '/api/exchange/commands?existing=yes', '/owner/payments', '/api/interface-check/prepare']) {
    const req = new Request('https://house.example' + path, { method: 'POST', body: 'unaltered request bytes' });
    const result = adapter.fetch(req, env, ctx);
    assert.equal(result, seen.at(-1).result);
    assert.equal(seen.at(-1).req, req);
    assert.equal(result.status, 418);
    assert.equal(result.headers.get('x-existing-policy'), 'preserved');
    assert.equal(await result.text(), 'Existing response bytes');
    assert.equal(await req.text(), 'unaltered request bytes');
  }
  const enabledEnv = { EXCHANGE_ENABLED: 'true', EXCHANGE_REAL_ENABLED: true, TREASURY_ENABLED: true };
  const sentinel = new Response('Still existing House');
  assert.equal(withHouseExchange({ fetch: () => sentinel }).fetch(request('/api/exchange/policy'), enabledEnv, ctx), sentinel);
});

test('review composition preserves existing route handling, request identity, and return object', async () => {
  const { store, calls } = fixture();
  const env = { EXCHANGE_ENABLED: 'true' };
  const ctx = {};
  for (const path of ['/catalog.json', '/api/proposals', '/api/exchange-other', '/exchange-other', '/api/interface-check/order?id=1', '/owner/payments']) {
    const req = request(path, { method: 'PATCH', body: 'existing-body', headers: { origin: 'https://existing-client.example' } });
    const sentinel = new Response('existing', { status: 207 });
    const adapter = withHouseExchange({ fetch(actual, actualEnv, actualCtx) {
      assert.equal(actual, req);
      assert.equal(actualEnv, env);
      assert.equal(actualCtx, ctx);
      assert.equal(actual.bodyUsed, false);
      return sentinel;
    } }, { review: { origin, assets, store } });
    assert.equal(adapter.fetch(req, env, ctx), sentinel);
    assert.equal(await req.text(), 'existing-body');
  }
  assert.deepEqual(calls, []);
});

test('review configuration rejects public origins, ambiguous loopback aliases, and real authority', () => {
  const { store, house } = fixture();
  for (const unsafe of ['https://house.example', 'http://localhost:8788', 'http://127.1:8788', 'http://127.0.0.1', 'http://127.0.0.1:0', 'http://127.0.0.1:65536', 'http://127.0.0.1:8788/', 'http://127.0.0.1:8788?public=true']) {
    assert.throws(() => withHouseExchange(house, { review: { origin: unsafe, assets, store } }), /explicit loopback/);
  }
  for (const flag of ['realEnabled', 'treasuryEnabled', 'automaticPayouts', 'privateAccess', 'serviceCreditEnabled', 'barterExecutionEnabled']) {
    assert.throws(() => withHouseExchange(house, { review: { origin, assets, store: { ...store, policy: { ...demoPolicy, [flag]: true } } } }), /cannot grant real authority/);
  }
  assert.throws(() => withHouseExchange(house, { review: { origin, assets: {}, store } }), /explicit local UI assets/);
});

test('local UI, policy, and capabilities use guarded paths and defensive headers', async () => {
  const { adapter, calls } = fixture();
  for (const path of ['/exchange/', '/exchange/index.html', '/exchange/ui.mjs', '/exchange/style.css', '/api/exchange/policy', '/api/exchange/capabilities', '/api/exchange/proposals']) {
    const result = await adapter.fetch(request(path));
    assert.equal(result.status, 200, path);
    assert.equal(result.headers.get('cache-control'), 'no-store');
    assert.match(result.headers.get('content-security-policy'), /frame-ancestors 'none'/);
    assert.equal(result.headers.get('x-content-type-options'), 'nosniff');
    assert.equal(result.headers.get('referrer-policy'), 'no-referrer');
    assert.equal(result.headers.get('x-lumen-exchange-mode'), 'synthetic-review');
    assert.equal(result.headers.get('access-control-allow-origin'), null);
  }
  const policy = await (await adapter.fetch(request('/api/exchange/policy'))).json();
  assert.deepEqual(policy, demoPolicy);
  assert.deepEqual(calls, ['list']);
  for (const path of ['/exchange/store.mjs', '/exchange/.data/demo.sqlite', '/api/exchange/credentials', '/api/exchange/pay', '/api/exchange']) {
    assert.equal((await adapter.fetch(request(path))).status, 404, path);
  }
  assert.deepEqual(calls, ['list']);
});

test('Host, URL origin, cross-origin requests, and absent-origin mutations fail before storage', async () => {
  const { adapter, calls } = fixture();
  const attempts = [
    new Request('https://house.example/exchange/', { headers: { host: '127.0.0.1:8788' } }),
    request('/exchange/', { headers: { host: 'evil.example' } }),
    new Request(origin + '/exchange/'),
    request('/api/exchange/proposals', { headers: { origin: 'https://evil.example' } }),
    request('/api/exchange/commands', { method: 'POST', body: '{}' }),
    request('/api/exchange/proposals/synthetic', { method: 'DELETE' }),
  ];
  for (const req of attempts) assert.equal((await adapter.fetch(req)).status, 403);
  assert.deepEqual(calls, []);
});

test('JSON, malformed UTF-8, declared size, and actual streamed size are bounded before execution', async () => {
  const { adapter, calls } = fixture();
  const attempts = [
    [post(undefined, '{}', { 'content-type': 'text/plain' }), 415],
    [post(undefined, '{'), 400],
    [post(undefined, '{}', { 'content-length': '16001' }), 413],
    [post(undefined, 'x'.repeat(16001)), 413],
    [post(undefined, new Uint8Array([123, 34, 120, 34, 58, 34, 195, 40, 34, 125])), 400],
  ];
  for (const [req, status] of attempts) assert.equal((await adapter.fetch(req)).status, status);
  let cancelled = false;
  const stream = new ReadableStream({
    start(controller) { controller.enqueue(new Uint8Array(16001)); },
    cancel() { cancelled = true; },
  });
  const streamed = post(undefined, stream);
  assert.equal((await adapter.fetch(streamed)).status, 413);
  assert.equal(cancelled, true);
  assert.deepEqual(calls, []);
});

test('split UTF-8 JSON reaches an asynchronous synthetic store unchanged', async () => {
  const command = { synthetic: 'café' };
  const bytes = new TextEncoder().encode(JSON.stringify(command));
  const split = bytes.indexOf(195) + 1;
  const body = new ReadableStream({ start(controller) { controller.enqueue(bytes.slice(0, split)); controller.enqueue(bytes.slice(split)); controller.close(); } });
  let seen;
  const { adapter } = fixture({ async execute(value, key) { seen = { value, key }; return { ok: true }; } });
  const result = await adapter.fetch(post(undefined, body));
  assert.equal(result.status, 200);
  assert.deepEqual(await result.json(), { ok: true });
  assert.deepEqual(seen, { value: command, key: 'synthetic-1' });
});

test('aborted and failed body streams return a bounded error without invoking storage', async () => {
  const { adapter, calls } = fixture();
  const controller = new AbortController();
  let cancelled = false;
  const body = new ReadableStream({ cancel() { cancelled = true; } });
  const req = request('/api/exchange/commands', { method: 'POST', body, duplex: 'half', signal: controller.signal, headers: { origin, 'content-type': 'application/json' } });
  const result = adapter.fetch(req);
  controller.abort();
  assert.equal((await result).status, 400);
  assert.equal(cancelled, true);
  const broken = new ReadableStream({ start(stream) { stream.error(new Error('private transport detail')); } });
  const response = await adapter.fetch(post(undefined, broken));
  assert.equal(response.status, 400);
  assert.equal((await response.json()).error, 'Invalid request body');
  assert.deepEqual(calls, []);
});

test('unknown internal errors do not disclose implementation details', async () => {
  const { adapter } = fixture({ list() { throw new Error('private implementation detail'); } });
  const response = await adapter.fetch(request('/api/exchange/proposals'));
  assert.equal(response.status, 500);
  assert.deepEqual(await response.json(), { error: 'Internal error' });
});

test('intake draft requires same-origin and explicit export header before reading storage', async () => {
  const { adapter, calls } = fixture();
  const path = '/api/exchange/proposals/synthetic/intake-draft';
  const attempts = [
    [request(path, { method: 'POST', headers: { 'x-exchange-local-export': 'confirmed' } }), 403],
    [request(path, { method: 'POST', headers: { origin: 'https://elsewhere.example', 'x-exchange-local-export': 'confirmed' } }), 403],
    [request(path, { method: 'POST', headers: { origin } }), 400],
    [request(path + '?localExportConfirmed=true', { method: 'POST', headers: { origin } }), 400],
    [request(path, { method: 'POST', headers: { origin, 'x-exchange-local-export': 'true' } }), 400],
    [request(path, { headers: { origin, 'x-exchange-local-export': 'confirmed' } }), 404],
  ];
  for (const [req, status] of attempts) assert.equal((await adapter.fetch(req)).status, status);
  assert.deepEqual(calls, []);
});

test('intake draft is an offline incomplete projection without writes or native submission', async t => {
  const store = openStore();
  t.after(() => store.close());
  const proposal = store.execute(syntheticCommand(), 'intake-draft-source');
  const before = store.export(proposal.id);
  let existingRequests = 0;
  const adapter = withHouseExchange({ fetch() { existingRequests++; return new Response('existing'); } }, {
    review: { origin, assets, store: {
      ...store,
      execute() { throw new Error('Unexpected state write'); },
      delete() { throw new Error('Unexpected state write'); },
    } },
  });
  const result = await adapter.fetch(request(`/api/exchange/proposals/${proposal.id}/intake-draft`, {
    method: 'POST', headers: { origin, 'x-exchange-local-export': 'confirmed' },
  }));
  assert.equal(result.status, 200);
  const draft = await result.json();
  assert.equal(draft.status, 'incomplete-offline-owner-review');
  assert.equal(draft.submissionEnabled, false);
  assert.equal(draft.target, null);
  assert.equal(draft.recordDraft.brief.budget, null);
  assert.equal(draft.recordDraft.brief.deliverable, null);
  assert.deepEqual(draft.recordDraft.brief.acceptance_criteria, []);
  assert.equal(draft.exchangeContext.evaluation.cost, 'Unknown');
  assert.equal(draft.exchangeContext.consent.nativeSubmission, false);
  assert.equal(draft.exchangeContext.retention.sourceExpiresAt, proposal.expiresAt);
  for (const flag of Object.values(draft.execution)) assert.equal(flag, false);
  assert.equal(JSON.stringify(draft).includes(proposal.id), false);
  assert.deepEqual(store.export(proposal.id), before);
  assert.equal(existingRequests, 0);
});

test('intake draft access purges expired source and returns 404 instead of projecting it', async t => {
  const store = openStore();
  t.after(() => store.close());
  const proposal = store.execute(syntheticCommand(), 'expired-intake-draft');
  const adapter = withHouseExchange({ fetch() { throw new Error('Unexpected backend request'); } }, { review: { origin, assets, store } });
  t.mock.method(Date, 'now', () => Date.parse(proposal.expiresAt) + 1);
  const result = await adapter.fetch(request(`/api/exchange/proposals/${proposal.id}/intake-draft`, {
    method: 'POST', headers: { origin, 'x-exchange-local-export': 'confirmed' },
  }));
  assert.equal(result.status, 404);
  assert.deepEqual(await result.json(), { error: 'Proposal not found' });
  assert.deepEqual(store.list(), []);
});

test('integrated review retains lifecycle, replay, export, and logical deletion in a synthetic store', async t => {
  const store = openStore();
  t.after(() => store.close());
  let existingRequests = 0;
  const adapter = withHouseExchange({ fetch() { existingRequests++; return new Response('existing'); } }, { review: { origin, assets, store } });
  const command = { action: 'propose', actor: 'requester', input: { mode: 'demo', agent: 'Synthetic agent', currentTask: 'Fictional review', needs: ['manifest verification'], missingResources: ['test cases'], offers: ['synthetic examples'], authority: 'proposal-only', terms: { kind: 'barter', description: 'Proposed synthetic file check', units: 1 }, evaluation: { cost: 'Unknown', usefulness: 'Reduce ambiguity', verifiability: 'Synthetic assertions' }, sharingConsent: true, provenance: { source: 'Synthetic test', rights: 'synthetic-demo-only' } } };
  const first = await (await adapter.fetch(post(undefined, JSON.stringify(command)))).json();
  const replay = await (await adapter.fetch(post(undefined, JSON.stringify(command)))).json();
  assert.equal(replay.id, first.id);
  assert.equal(first.mode, 'demo');
  const proposals = await (await adapter.fetch(request('/api/exchange/proposals'))).json();
  assert.equal(proposals.length, 1);
  assert.equal(proposals[0].matches[0].availability, 'must-reverify');
  const exported = await (await adapter.fetch(request('/api/exchange/proposals/' + first.id))).json();
  assert.equal(exported.proposal.provenance.rights, 'synthetic-demo-only');
  assert.equal(exported.events.length, 1);
  const deleted = await adapter.fetch(request('/api/exchange/proposals/' + first.id, { method: 'DELETE', headers: { origin } }));
  assert.equal(deleted.status, 200);
  assert.equal((await adapter.fetch(request('/api/exchange/proposals/' + first.id))).status, 404);
  assert.equal(existingRequests, 0);
});
