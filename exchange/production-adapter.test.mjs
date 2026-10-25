import test from 'node:test';
import assert from 'node:assert/strict';
import {withDisabledHouseExchange} from './production-adapter.mjs';

test('production composition forwards exact arguments, receiver and return value without reading authority', () => {
  const denied = new Proxy({}, {get() { throw new Error('Must not inspect authority, bindings or storage'); }});
  const request = new Request('https://house.example/api/exchange/commands?unchanged=1', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{"existing":"body"}',
  });
  const response = new Response('Existing House result', {status: 409, headers: {'X-Existing-Policy': 'unchanged'}});
  const house = {fetch(seenRequest, seenEnv, seenCtx) {
    assert.equal(this, house);
    assert.equal(seenRequest, request);
    assert.equal(seenEnv, denied);
    assert.equal(seenCtx, denied);
    assert.equal(request.bodyUsed, false);
    return response;
  }};
  const production = withDisabledHouseExchange(house);
  assert.equal(production.fetch(request, denied, denied), response);
  assert.equal(request.bodyUsed, false);
});

test('production composition delegates exchange-looking and existing paths for all methods', async () => {
  for (const path of ['/', '/exchange/', '/exchange/ui.mjs', '/api/exchange/policy',
    '/api/exchange/commands', '/api/proposals', '/api/existing-payment']) {
    for (const method of ['GET', 'HEAD', 'POST', 'DELETE']) {
      const init = {method, headers: {'X-Existing-Client': 'preserve'}};
      if (!['GET', 'HEAD'].includes(method)) init.body = 'Synthetic existing request';
      const request = new Request('https://house.example' + path, init);
      let calls = 0;
      const house = {async fetch(seen) {
        calls++;
        assert.equal(seen, request);
        assert.equal(seen.headers.get('X-Existing-Client'), 'preserve');
        return new Response(method === 'HEAD' ? null : await seen.text(), {status: 404});
      }};
      const response = await withDisabledHouseExchange(house).fetch(request, {}, {});
      assert.equal(calls, 1);
      assert.equal(response.status, 404);
      assert.equal(await response.text(), init.body ?? '');
    }
  }
});

test('production composition preserves promise identity and original errors', async () => {
  const pending = Promise.resolve(new Response('Existing asynchronous response'));
  const request = new Request('https://house.example/');
  assert.equal(withDisabledHouseExchange({fetch: () => pending}).fetch(request), pending);
  const failure = new Error('Existing House failure');
  assert.throws(() => withDisabledHouseExchange({fetch() {throw failure;}}).fetch(request), error => error === failure);
  const rejected = Promise.reject(failure);
  const result = withDisabledHouseExchange({fetch: () => rejected}).fetch(request);
  assert.equal(result, rejected);
  await assert.rejects(result, error => error === failure);
});

test('production composition requires an existing fetch implementation', () => {
  for (const house of [null, undefined, {}, {fetch: false}]) {
    assert.throws(() => withDisabledHouseExchange(house), /House fetch implementation required/);
  }
});
