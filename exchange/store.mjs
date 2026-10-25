// Local SQLite adapter only. Workers use the runtime-neutral core separately.
import { DatabaseSync } from 'node:sqlite';
import { createHash } from 'node:crypto';
import { emptyState, executeOperation, POLICY, ExchangeError } from './core.mjs';
export { POLICY, ExchangeError } from './core.mjs';

export function openStore(path = ':memory:') {
  const db = new DatabaseSync(path);
  const version = db.prepare('PRAGMA user_version').get().user_version;
  if (![0, 1].includes(version)) { db.close(); throw new ExchangeError('Unsupported exchange schema', 503); }
  db.exec(`PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
    CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY, body TEXT NOT NULL, expires TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, proposal_id TEXT NOT NULL, body TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS requests(key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, proposal_id TEXT NOT NULL, response TEXT NOT NULL);
    PRAGMA user_version=1;`);

  function load() {
    return {
      ...emptyState(),
      proposals: db.prepare('SELECT body FROM proposals ORDER BY rowid').all().map(row => JSON.parse(row.body)),
      events: db.prepare('SELECT proposal_id, body FROM events ORDER BY seq').all().map(row => ({proposalId: row.proposal_id, event: JSON.parse(row.body)})),
      requests: db.prepare('SELECT * FROM requests').all().map(row => ({key: row.key, fingerprint: row.fingerprint, proposalId: row.proposal_id, response: JSON.parse(row.response)})),
    };
  }

  function save(state) {
    db.exec('DELETE FROM proposals; DELETE FROM events; DELETE FROM requests;');
    const proposal = db.prepare('INSERT INTO proposals VALUES(?,?,?)');
    const event = db.prepare('INSERT INTO events(proposal_id,body) VALUES(?,?)');
    const request = db.prepare('INSERT INTO requests VALUES(?,?,?,?)');
    for (const row of state.proposals) proposal.run(row.id, JSON.stringify(row), row.expiresAt);
    for (const row of state.events) event.run(row.proposalId, JSON.stringify(row.event));
    for (const row of state.requests) request.run(row.key, row.fingerprint, row.proposalId, JSON.stringify(row.response));
  }

  function transact(operation, options) {
    db.exec('BEGIN IMMEDIATE');
    try {
      const before = load();
      const {state, result} = executeOperation(before, operation, {
        legacyFingerprint: command => createHash('sha256').update(JSON.stringify(command)).digest('hex'),
        ...options,
      });
      if (JSON.stringify(state) !== JSON.stringify(before)) save(state);
      db.exec('COMMIT');
      return result;
    } catch (error) { db.exec('ROLLBACK'); throw error; }
  }

  // Commit expiry before an operation that may reject, matching the original
  // demo's on-access retention behavior. No expired payload is returned.
  function access(operation) {
    transact({kind: 'purge'});
    return transact(operation);
  }

  return {
    policy: POLICY,
    execute(command, key) { return access({kind: 'command', command, key}); },
    list() { return access({kind: 'list'}); },
    export(id) { return access({kind: 'export', id}); },
    delete(id) { return access({kind: 'delete', id}); },
    purge(now) { return transact({kind: 'purge'}, {now}); },
    close() { db.close(); },
  };
}
