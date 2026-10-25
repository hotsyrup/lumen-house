import { DatabaseSync } from 'node:sqlite';
import { createHash, randomUUID } from 'node:crypto';

export const POLICY = Object.freeze({ mode: 'demo', realEnabled: false, treasuryEnabled: false, automaticPayouts: false, privateAccess: false, maxOpen: 10, maxUnitsPerProposal: 5, totalReservedUnits: 10, retentionDays: 7 });
export class ExchangeError extends Error { constructor(message, status = 400) { super(message); this.status = status; } }
const requireThat = (condition, message, status) => { if (!condition) throw new ExchangeError(message, status); };
function text(value, name, max = 1000) { requireThat(typeof value === 'string' && value.trim().length > 0 && value.length <= max, `Invalid ${name}`); return value.trim(); }
function exact(object, keys) { requireThat(object && typeof object === 'object' && !Array.isArray(object), 'Expected object'); requireThat(Object.keys(object).every(k => keys.includes(k)), 'Unknown fields are not collected'); }
function strings(value, name) { requireThat(Array.isArray(value) && value.length > 0 && value.length <= 10, `Invalid ${name}`); return value.map(v => text(v, name, 300)); }
export function openStore(path = ':memory:') {
  const db = new DatabaseSync(path);
  db.exec(`PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
    CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY, body TEXT NOT NULL, expires TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, proposal_id TEXT NOT NULL, body TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS requests(key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, proposal_id TEXT NOT NULL, response TEXT NOT NULL);`);
  const get = id => { const row = db.prepare('SELECT body FROM proposals WHERE id=?').get(id); requireThat(row, 'Proposal not found', 404); return JSON.parse(row.body); };
  const list = () => db.prepare('SELECT body FROM proposals ORDER BY rowid DESC').all().map(r => JSON.parse(r.body));
  function purge(now = new Date().toISOString()) {
    db.exec('BEGIN IMMEDIATE');
    try { const ids = db.prepare('SELECT id FROM proposals WHERE expires <= ?').all(now); for (const {id} of ids) erase(id); db.exec('COMMIT'); return ids.length; } catch(e) { db.exec('ROLLBACK'); throw e; }
  }
  function erase(id) { for (const table of ['events', 'requests']) db.prepare(`DELETE FROM ${table} WHERE proposal_id=?`).run(id); db.prepare('DELETE FROM proposals WHERE id=?').run(id); }
  function execute(command, key) {
    purge();
    text(key, 'idempotency key', 100);
    exact(command, ['action', 'id', 'actor', 'input']);
    requireThat(['requester', 'provider', 'owner'].includes(command.actor), 'Demo actor required');
    const fingerprint = createHash('sha256').update(JSON.stringify(command)).digest('hex');
    db.exec('BEGIN IMMEDIATE');
    try {
      const prior = db.prepare('SELECT * FROM requests WHERE key=?').get(key);
      if(prior) { requireThat(prior.fingerprint === fingerprint, 'Idempotency key reused with different command', 409); db.exec('COMMIT'); return JSON.parse(prior.response); }
      const now = new Date().toISOString();
      const {action, actor} = command;
      let proposal;
      if(action === 'propose') {
        requireThat(actor === 'requester', 'Only demo requester can propose', 403);
        const input = command.input;
        exact(input, ['mode', 'agent', 'currentTask', 'needs', 'missingResources', 'offers', 'authority', 'terms', 'evaluation', 'sharingConsent', 'provenance']);
        requireThat(input.mode === 'demo', 'Real exchange is disabled', 403);
        requireThat(input.sharingConsent === true, 'Explicit consent required');
        requireThat(input.authority === 'proposal-only', 'No execution authority is granted', 403);
        exact(input.terms, ['kind', 'description', 'units']);
        requireThat(['barter', 'service-credit-proposal'].includes(input.terms.kind), 'Unsupported proposal kind');
        requireThat(Number.isInteger(input.terms.units) && input.terms.units >= 1 && input.terms.units <= POLICY.maxUnitsPerProposal, 'Proposal unit cap exceeded');
        exact(input.evaluation, ['cost', 'usefulness', 'verifiability']);
        exact(input.provenance, ['source', 'rights']);
        requireThat(input.provenance.rights === 'synthetic-demo-only', 'Only synthetic demo content allowed');
        requireThat(list().filter(p => !['completed','cancelled'].includes(p.state)).length < POLICY.maxOpen, 'Open proposal cap exceeded', 409);
        proposal = {id: randomUUID(), mode: 'demo', agent: text(input.agent, 'agent', 100), currentTask: text(input.currentTask, 'current task'), needs: strings(input.needs,'needs'), missingResources: strings(input.missingResources,'resources'), offers: strings(input.offers,'offers'), authority: 'proposal-only', terms: {kind: input.terms.kind, description: text(input.terms.description,'terms'), units: input.terms.units}, evaluation: Object.fromEntries(['cost','usefulness','verifiability'].map(k => [k,text(input.evaluation[k],k)])), sharingConsent: true, provenance: {source: text(input.provenance.source,'source'), rights: 'synthetic-demo-only'}, state: 'proposed', termsRevision: 1, reservedUnits: 0, createdAt: now, expiresAt: new Date(Date.now()+POLICY.retentionDays*86400000).toISOString()};
      } else {
        proposal = get(text(command.id, 'id', 100));
        const transitions = {counteroffer: ['proposed','counteroffered'], agree: ['proposed','counteroffered'], deliver: ['agreed'], accept: ['delivered'], complete: ['accepted'], cancel: ['proposed','counteroffered','agreed','delivered','accepted'], escalate: ['proposed','counteroffered','agreed','delivered','accepted']};
        requireThat(Object.hasOwn(transitions, action) && transitions[action].includes(proposal.state), 'Invalid state transition', 409);
        const roles = {counteroffer: ['provider','requester'], agree: ['owner'], deliver: ['provider'], accept: ['requester'], complete: ['owner'], cancel: ['owner','provider','requester'], escalate: ['owner','provider','requester']};
        requireThat(roles[action].includes(actor), 'Wrong demo actor for transition', 403);
        const input = command.input || {};
        if(action === 'counteroffer') { exact(input,['description']); proposal.terms.description = text(input.description,'counteroffer'); proposal.termsRevision += 1; proposal.state = 'counteroffered'; }
        if(action === 'agree') {
          exact(input,['requesterConfirmed','providerConfirmed','expectedTermsRevision']);
          requireThat(input.requesterConfirmed === true && input.providerConfirmed === true, 'Both parties must confirm current terms');
          requireThat(input.expectedTermsRevision === proposal.termsRevision, 'Terms changed; review current proposal before agreement', 409);
          const reserved = list().reduce((sum,p) => sum + p.reservedUnits,0);
          requireThat(reserved + proposal.terms.units <= POLICY.totalReservedUnits, 'Demo reservation cap exceeded', 409);
          proposal.reservedUnits = proposal.terms.units; proposal.state = 'agreed'; proposal.agreement = {termsRevision: proposal.termsRevision, terms: {...proposal.terms}, requesterConfirmed: true, providerConfirmed: true, at: now, recordedBy: actor};
        }
        if(action === 'deliver') { exact(input,['evidence']); proposal.evidence = text(input.evidence,'evidence'); proposal.state = 'delivered'; }
        if(action === 'accept') { exact(input,['assessment']); proposal.acceptance = text(input.assessment,'assessment'); proposal.state = 'accepted'; }
        if(action === 'complete' || action === 'cancel') { exact(input,['reason']); proposal.resolution = text(input.reason,'reason'); proposal.state = action === 'complete' ? 'completed' : 'cancelled'; proposal.reservedUnits = 0; }
        if(action === 'escalate') { exact(input,['reason']); proposal.escalation = {reason: text(input.reason,'reason'), at: now, status: 'owner-review-required'}; }
      }
      proposal.updatedAt = now;
      db.prepare('INSERT INTO proposals VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body').run(proposal.id,JSON.stringify(proposal),proposal.expiresAt);
      // Content-bearing audit and idempotency data expire/delete with the proposal.
      db.prepare('INSERT INTO events(proposal_id,body) VALUES(?,?)').run(proposal.id,JSON.stringify({action, actor, at: now, snapshot: proposal}));
      db.prepare('INSERT INTO requests VALUES(?,?,?,?)').run(key,fingerprint,proposal.id,JSON.stringify(proposal));
      db.exec('COMMIT'); return proposal;
    } catch(e) { db.exec('ROLLBACK'); throw e; }
  }
  return {execute, policy: POLICY, list() { purge(); return list(); }, export(id) { purge(); return {proposal: get(id), events: db.prepare('SELECT body FROM events WHERE proposal_id=? ORDER BY seq').all(id).map(r=>JSON.parse(r.body))}; }, delete(id) { db.exec('BEGIN IMMEDIATE'); try { get(id); erase(id); db.exec('COMMIT'); } catch(e) {db.exec('ROLLBACK');throw e;} }, purge, close() { db.close(); }};
}
