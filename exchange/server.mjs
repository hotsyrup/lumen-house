import { createServer } from 'node:http';
import { readFileSync, mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join, resolve } from 'node:path';
import { openStore, ExchangeError } from './store.mjs';
import { capabilities, matchCapabilities } from './capabilities.mjs';

const directory = dirname(fileURLToPath(import.meta.url));
export function createExchangeServer(store) {
  return createServer(async (req,res)=>{
    const origin = `http://127.0.0.1:${res.socket.localPort}`;
    const send = (status,data,type='application/json') => {res.writeHead(status,{'Content-Type':type,'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Content-Security-Policy':"default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",'Referrer-Policy':'no-referrer'});res.end(type==='application/json'?JSON.stringify(data):data);};
    try {
      if(req.headers.host !== new URL(origin).host) throw new ExchangeError('Loopback Host required',403);
      if(req.headers.origin && req.headers.origin !== origin) throw new ExchangeError('Cross-origin requests disabled',403);
      const url = new URL(req.url,origin);
      if(req.method==='GET' && ['/','/index.html','/ui.mjs','/style.css'].includes(url.pathname)) {
        const name = url.pathname==='/'?'index.html':url.pathname.slice(1);
        return send(200,readFileSync(join(directory,name)),name.endsWith('.html')?'text/html; charset=utf-8':name.endsWith('.css')?'text/css':'text/javascript');
      }
      if(req.method==='GET' && url.pathname==='/api/policy') return send(200,store.policy);
      if(req.method==='GET' && url.pathname==='/api/capabilities') return send(200,capabilities);
      if(req.method==='GET' && url.pathname==='/api/proposals') return send(200,store.list().map(p=>({...p,matches:matchCapabilities(p.needs)})));
      const item = url.pathname.match(/^\/api\/proposals\/([a-zA-Z0-9-]+)$/);
      if(req.method==='GET' && item) return send(200,store.export(item[1]));
      if(['POST','DELETE'].includes(req.method)) {
        if(req.headers.origin!==origin) throw new ExchangeError('Same-origin confirmation required',403);
        if(req.method==='DELETE' && item) {store.delete(item[1]); return send(200,{deleted:true});}
        if(req.method==='POST' && url.pathname==='/api/commands') {
          if(req.headers['content-type']!=='application/json') throw new ExchangeError('JSON required',415);
          const chunks=[]; let bytes=0; for await(const chunk of req) {bytes+=chunk.length; if(bytes>16000) throw new ExchangeError('Request too large',413); chunks.push(chunk);}
          const body=Buffer.concat(chunks).toString('utf8');
          let command; try {command=JSON.parse(body);} catch {throw new ExchangeError('Invalid JSON');}
          return send(200,store.execute(command,req.headers['idempotency-key']));
        }
      }
      send(404,{error:'Not found'});
    } catch(error) {send(error.status || 500,{error:error.status?error.message:'Internal error'});}
  });
}
if(process.argv[1] && resolve(process.argv[1])===fileURLToPath(import.meta.url)) {
  const data = join(directory,'.data'); mkdirSync(data,{recursive:true,mode:0o700});
  const store=openStore(join(data,'demo.sqlite'));
  const server=createExchangeServer(store);
  server.listen(8788,'127.0.0.1',()=>console.log('Demo-only exchange: http://127.0.0.1:8788 — synthetic data only; no real execution'));
  const cleanup=setInterval(()=>store.purge(),60000); cleanup.unref();
  for(const signal of ['SIGINT','SIGTERM']) process.once(signal,()=>server.close(()=>{clearInterval(cleanup);store.close();process.exit(0);}));
}
