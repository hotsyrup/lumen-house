import { capabilities, matchCapabilities } from './capabilities.mjs';
import { withDisabledHouseExchange } from './production-adapter.mjs';
import { createHouseIntakeDraft } from './house-intake-draft.mjs';

const MAX_BODY_BYTES = 16000;
const TYPES = Object.freeze({
  'index.html': 'text/html; charset=utf-8',
  'ui.mjs': 'text/javascript; charset=utf-8',
  'style.css': 'text/css; charset=utf-8',
});
const DISABLED_AUTHORITY = Object.freeze({
  mode: 'demo',
  realEnabled: false,
  treasuryEnabled: false,
  automaticPayouts: false,
  privateAccess: false,
  serviceCreditEnabled: false,
  barterExecutionEnabled: false,
});

class ReviewError extends Error {
  constructor(message, status = 400) {
    super(message);
    this.status = status;
  }
}

function response(status, value, type = 'application/json') {
  return new Response(type === 'application/json' ? JSON.stringify(value) : value, {
    status,
    headers: {
      'Content-Type': type,
      'Cache-Control': 'no-store',
      'X-Content-Type-Options': 'nosniff',
      'Content-Security-Policy': "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
      'Referrer-Policy': 'no-referrer',
      'X-Robots-Tag': 'noindex, nofollow',
      'X-Lumen-Exchange-Mode': 'synthetic-review',
    },
  });
}

function validateReview(review) {
  const port = typeof review?.origin === 'string' && review.origin.match(/^http:\/\/127\.0\.0\.1:([1-9]\d{0,4})$/)?.[1];
  if (!port || Number(port) > 65535) {
    throw new TypeError('Exchange review requires an explicit loopback HTTP origin and port');
  }
  const { store, assets } = review;
  if (!store || !['execute', 'list', 'export', 'delete'].every(name => typeof store[name] === 'function')) {
    throw new TypeError('Exchange review requires a synthetic store');
  }
  const policy = store.policy;
  if (!policy || !Object.entries(DISABLED_AUTHORITY).every(([name, value]) => policy[name] === value)) {
    throw new TypeError('Exchange review cannot grant real authority');
  }
  if (!assets || !Object.keys(TYPES).every(name => typeof assets[name] === 'string')) {
    throw new TypeError('Exchange review requires explicit local UI assets');
  }
  // Capture only the expected assets; no filesystem, bindings, or provider access.
  const url = new URL(review.origin);
  return {
    origin: url.origin,
    host: url.host,
    store,
    policy: Object.freeze({ ...policy, ...DISABLED_AUTHORITY }),
    assets: Object.fromEntries(Object.keys(TYPES).map(name => [name, assets[name]])),
  };
}

async function jsonBody(request) {
  const contentType = request.headers.get('content-type') ?? '';
  if (!/^application\/json(?:;\s*charset=utf-8)?$/i.test(contentType)) {
    throw new ReviewError('JSON required', 415);
  }
  const declared = request.headers.get('content-length');
  if (declared !== null && (!/^\d+$/.test(declared) || Number(declared) > MAX_BODY_BYTES)) {
    throw new ReviewError('Request too large', 413);
  }
  if (request.signal.aborted) throw new ReviewError('Request aborted');
  const reader = request.body?.getReader();
  if (!reader) throw new ReviewError('Invalid JSON');
  let aborted = false;
  const cancel = () => { void reader.cancel().catch(() => {}); };
  const onAbort = () => { aborted = true; cancel(); };
  request.signal.addEventListener('abort', onAbort, { once: true });
  const chunks = [];
  let length = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (aborted || request.signal.aborted) throw new ReviewError('Request aborted');
      if (done) break;
      length += value.byteLength;
      if (length > MAX_BODY_BYTES) {
        cancel();
        throw new ReviewError('Request too large', 413);
      }
      chunks.push(value);
    }
    const bytes = new Uint8Array(length);
    let offset = 0;
    for (const chunk of chunks) {
      bytes.set(chunk, offset);
      offset += chunk.byteLength;
    }
    try {
      return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
    } catch {
      throw new ReviewError('Invalid JSON');
    }
  } catch (error) {
    if (error instanceof ReviewError) throw error;
    throw new ReviewError(aborted || request.signal.aborted ? 'Request aborted' : 'Invalid request body');
  } finally {
    request.signal.removeEventListener('abort', onAbort);
    reader.releaseLock();
  }
}

async function reviewFetch(request, review) {
  try {
    const url = new URL(request.url);
    if (url.origin !== review.origin || request.headers.get('host') !== review.host) {
      throw new ReviewError('Loopback Host required', 403);
    }
    const origin = request.headers.get('origin');
    if (origin !== null && origin !== review.origin) {
      throw new ReviewError('Cross-origin requests disabled', 403);
    }
    const path = url.pathname;
    if (request.method === 'GET' && path.startsWith('/exchange/')) {
      const name = path === '/exchange/' ? 'index.html' : path.slice('/exchange/'.length);
      if (Object.hasOwn(TYPES, name)) return response(200, review.assets[name], TYPES[name]);
    }
    if (request.method === 'GET' && path === '/api/exchange/policy') return response(200, review.policy);
    if (request.method === 'GET' && path === '/api/exchange/capabilities') return response(200, capabilities);
    if (request.method === 'GET' && path === '/api/exchange/proposals') {
      const proposals = await review.store.list();
      return response(200, proposals.map(proposal => ({ ...proposal, matches: matchCapabilities(proposal.needs) })));
    }
    const item = path.match(/^\/api\/exchange\/proposals\/([a-zA-Z0-9-]{1,100})$/);
    const intakeDraft = path.match(/^\/api\/exchange\/proposals\/([a-zA-Z0-9-]{1,100})\/intake-draft$/);
    if (request.method === 'GET' && item) return response(200, await review.store.export(item[1]));
    if (['POST', 'DELETE'].includes(request.method)) {
      if (origin !== review.origin) throw new ReviewError('Same-origin confirmation required', 403);
      if (request.method === 'DELETE' && item) {
        await review.store.delete(item[1]);
        return response(200, { deleted: true });
      }
      if (request.method === 'POST' && intakeDraft) {
        if (request.headers.get('x-exchange-local-export') !== 'confirmed') {
          throw new ReviewError('Explicit local export confirmation required');
        }
        // Export enforces source expiry first. The projection returns only an
        // incomplete local draft; it never writes native records or submits it.
        const exported = await review.store.export(intakeDraft[1]);
        return response(200, createHouseIntakeDraft(exported.proposal, { localExportConfirmed: true }));
      }
      if (request.method === 'POST' && path === '/api/exchange/commands') {
        const command = await jsonBody(request);
        return response(200, await review.store.execute(command, request.headers.get('idempotency-key')));
      }
    }
    return response(404, { error: 'Not found' });
  } catch (error) {
    const status = Number.isInteger(error.status) && error.status >= 400 && error.status < 500 ? error.status : 500;
    return response(status, { error: status === 500 ? 'Internal error' : error.message });
  }
}

/**
 * A disabled-by-default composition seam, with an optional loopback-only review.
 * No environment flag enables it. Real records, identity, authority, and payments
 * require a separately reviewed adapter on the existing House record service.
 */
export function withHouseExchange(house, { review } = {}) {
  if (!house || typeof house.fetch !== 'function') throw new TypeError('House fetch implementation required');
  if (review == null) {
    return withDisabledHouseExchange(house);
  }
  const local = validateReview(review);
  return {
    fetch(request, env, ctx) {
      const path = new URL(request.url).pathname;
      const exchangePath = path === '/exchange' || path.startsWith('/exchange/') ||
        path === '/api/exchange' || path.startsWith('/api/exchange/');
      return exchangePath ? reviewFetch(request, local) : house.fetch(request, env, ctx);
    },
  };
}
