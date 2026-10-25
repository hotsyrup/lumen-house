import { createServer } from 'node:http';
import { readFileSync, mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join, resolve } from 'node:path';
import { Readable } from 'node:stream';
import { openStore } from './store.mjs';
import { withHouseExchange } from './house-adapter.mjs';

const directory = dirname(fileURLToPath(import.meta.url));
const assetNames = ['index.html', 'ui.mjs', 'style.css'];

export function createExchangeServer(store, {house} = {}) {
  const assets = Object.fromEntries(assetNames.map(name => [name, readFileSync(join(directory, name), 'utf8')]));
  if (!house) {
    assets['index.html'] = assets['index.html'].replace(
      '<a data-house-catalog href="/">← House catalog</a>',
      '<span>Standalone local demo</span>',
    );
  }
  return createServer(async (req, res) => {
    const origin = `http://127.0.0.1:${res.socket.localPort}`;
    try {
      if (req.headers.host !== new URL(origin).host) {
        res.writeHead(403, {'Content-Type': 'application/json', 'Cache-Control': 'no-store'});
        return res.end(JSON.stringify({error: 'Loopback Host required'}));
      }
      // Legacy addresses belong only to this local preview. The House adapter
      // never captures existing root assets or /api/proposals routes.
      const url = new URL(req.url, origin);
      if (!house) {
        if (['/', '/index.html', '/ui.mjs', '/style.css'].includes(url.pathname)) {
          url.pathname = '/exchange/' + (url.pathname === '/' ? '' : url.pathname.slice(1));
        } else if (/^\/api\/(policy|capabilities|proposals|commands)(\/|$)/.test(url.pathname)) {
          url.pathname = url.pathname.replace('/api/', '/api/exchange/');
        }
      }
      const fallback = house ?? {fetch: () => new Response(JSON.stringify({error: 'Not found'}), {
        status: 404, headers: {'Content-Type': 'application/json', 'Cache-Control': 'no-store'},
      })};
      const worker = withHouseExchange(fallback, {review: {store, assets, origin}});
      const headers = new Headers();
      for (const [key, value] of Object.entries(req.headers)) {
        if (Array.isArray(value)) for (const part of value) headers.append(key, part);
        else if (value !== undefined) headers.set(key, value);
      }
      const init = {method: req.method, headers};
      if (!['GET', 'HEAD'].includes(req.method)) {
        init.body = Readable.toWeb(req);
        init.duplex = 'half';
      }
      const response = await worker.fetch(new Request(url, init), {}, {});
      res.writeHead(response.status, Object.fromEntries(response.headers));
      res.end(Buffer.from(await response.arrayBuffer()));
    } catch {
      if (!res.headersSent) res.writeHead(500, {'Content-Type': 'application/json', 'Cache-Control': 'no-store'});
      res.end(JSON.stringify({error: 'Internal error'}));
    }
  });
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const data = join(directory, '.data');
  mkdirSync(data, {recursive: true, mode: 0o700});
  const store = openStore(join(data, 'demo.sqlite'));
  const server = createExchangeServer(store);
  server.listen(8788, '127.0.0.1', () => console.log('House exchange review: http://127.0.0.1:8788/exchange/ — synthetic local data only'));
  const cleanup = setInterval(() => store.purge(), 60000);
  cleanup.unref();
  for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, () => server.close(() => {
    clearInterval(cleanup);
    store.close();
    process.exit(0);
  }));
}
