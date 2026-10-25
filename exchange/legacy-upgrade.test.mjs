import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {mkdtempSync, readFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {DatabaseSync} from 'node:sqlite';
import {openStore as openLegacyStore} from './fixtures/legacy-store-537d18c.mjs';
import {openStore, POLICY} from './store.mjs';

// Exact original writer from hotsyrup/lumen-house commit
// 537d18c348b8386d2db326202d13774b102e551b, exchange/store.mjs.
// Tests create real version-0 SQLite rows, including their original SHA-256
// replay fingerprints. No current-core state objects substitute for old data.
const LEGACY_BLOB = 'a9a3e086244c91c649e8627f52cb56fea1c19989';
const fixtureInput = () => ({
  mode: 'demo', agent: 'Synthetic legacy agent', currentTask: 'Review a fictional handoff',
  needs: ['Synthetic manifest check'], missingResources: ['Fictional checker'], offers: ['Synthetic test cases'],
  authority: 'proposal-only', terms: {kind: 'barter', description: 'Fictional review only', units: 1},
  evaluation: {cost: 'Unknown', usefulness: 'Exercise legacy recovery', verifiability: 'Offline synthetic assertions'},
  sharingConsent: true, provenance: {source: 'Synthetic legacy fixture', rights: 'synthetic-demo-only'},
});
const proposedCommand = () => ({action: 'propose', actor: 'requester', input: fixtureInput()});
const denied = pattern => error => error.status === 409 && pattern.test(error.message);

function database(t, {events = 1, retained = 1, closed = false, agreed = false} = {}) {
  const directory = mkdtempSync(join(tmpdir(), 'exchange-real-legacy-'));
  t.after(() => rmSync(directory, {recursive: true, force: true}));
  const path = join(directory, 'demo.sqlite');
  const legacy = openLegacyStore(path);
  const records = [];
  try {
    for (let index = 0; index < retained; index++) {
      const command = proposedCommand(), key = 'legacy-propose-' + index;
      const proposal = legacy.execute(command, key);
      records.push({proposal, command, key});
      if (closed) {
        legacy.execute({action: 'cancel', actor: 'owner', id: proposal.id,
          input: {reason: 'Synthetic historical closure'}}, 'legacy-close-' + index);
      }
    }
    const first = records[0];
    let count = closed ? 2 : 1;
    if (agreed) {
      legacy.execute({action: 'agree', actor: 'owner', id: first.proposal.id,
        input: {requesterConfirmed: true, providerConfirmed: true, expectedTermsRevision: 1}}, 'legacy-agree');
      count++;
    }
    for (; count < events; count++) {
      legacy.execute({action: 'escalate', actor: 'requester', id: first.proposal.id,
        input: {reason: 'Synthetic legacy history ' + count}}, 'legacy-event-' + count);
    }
    for (const record of records) record.exported = legacy.export(record.proposal.id);
  } finally {
    legacy.close();
  }
  assert.equal(schemaVersion(path), 0, 'Fixture must come from the original, unversioned writer');
  return {path, records};
}

function schemaVersion(path) {
  const db = new DatabaseSync(path);
  try { return db.prepare('PRAGMA user_version').get().user_version; }
  finally { db.close(); }
}

function counts(path) {
  const db = new DatabaseSync(path);
  try {
    return Object.fromEntries(['proposals', 'events', 'requests'].map(table =>
      [table, db.prepare('SELECT COUNT(*) AS n FROM ' + table).get().n]));
  } finally { db.close(); }
}

function withStore(path, run) {
  const store = openStore(path);
  try { return run(store); }
  finally { store.close(); }
}

function newEscalation(id) {
  return {action: 'escalate', actor: 'owner', id, input: {reason: 'Must not grow legacy history'}};
}

test('legacy fixture is byte-identical to the pinned original writer', () => {
  const bytes = readFileSync(new URL('./fixtures/legacy-store-537d18c.mjs', import.meta.url));
  const blob = createHash('sha1').update('blob ' + bytes.length + '\0').update(bytes).digest('hex');
  assert.equal(blob, LEGACY_BLOB);
});

test('over-cap legacy events retain full export and replay across upgrade and restart without new growth', t => {
  const {path, records: [record]} = database(t, {events: POLICY.maxEventsPerProposal + 1, agreed: true});
  const before = counts(path);
  assert.equal(before.events, 101);
  assert.equal(record.exported.proposal.reservedUnits, 1);
  // A denied first command stamps version 1. Reopening that already-upgraded
  // database must not leave it locked out of read/export/deletion operations.
  withStore(path, store => {
    assert.throws(() => store.execute(newEscalation(record.proposal.id), 'denied-first-access'), denied(/cap/i));
  });
  assert.equal(schemaVersion(path), 1);
  assert.deepEqual(counts(path), before);
  for (let reopen = 0; reopen < 2; reopen++) {
    withStore(path, store => {
      assert.equal(store.list().length, 1);
      assert.deepEqual(store.export(record.proposal.id), record.exported, 'No legacy audit is truncated');
      assert.deepEqual(store.execute(record.command, record.key), record.proposal, 'Original SHA-256 retry survives');
      assert.throws(() => store.execute({...record.command, input: {...record.command.input, agent: 'Changed fictional intent'}},
        record.key), denied(/Idempotency/));
      assert.throws(() => store.execute(proposedCommand(), 'new-growth-' + reopen), denied(/cap/i));
      assert.throws(() => store.execute(newEscalation(record.proposal.id), 'more-events-' + reopen), denied(/cap/i));
      assert.equal(store.list()[0].reservedUnits, 1);
    });
    assert.deepEqual(counts(path), before, 'Denied growth and retries write no new history');
  }
  withStore(path, store => store.delete(record.proposal.id));
  assert.deepEqual(counts(path), {proposals: 0, events: 0, requests: 0});
  withStore(path, store => {
    assert.deepEqual(store.list(), []);
    assert.equal(store.execute(proposedCommand(), 'after-delete').reservedUnits, 0);
  });
});

test('legacy event history at the cap remains exportable but cannot exceed it', t => {
  const {path, records: [record]} = database(t, {events: POLICY.maxEventsPerProposal});
  withStore(path, store => {
    assert.deepEqual(store.export(record.proposal.id), record.exported);
    assert.throws(() => store.execute(newEscalation(record.proposal.id), 'at-event-cap'), denied(/event cap/i));
    assert.equal(store.export(record.proposal.id).events.length, 100);
  });
  withStore(path, store => {
    assert.equal(store.export(record.proposal.id).events.length, 100);
    store.delete(record.proposal.id);
  });
  assert.deepEqual(counts(path), {proposals: 0, events: 0, requests: 0});
});

test('expiry purges over-cap legacy event history and reservations after restart', t => {
  const {path, records: [record]} = database(t, {events: POLICY.maxEventsPerProposal + 1, agreed: true});
  withStore(path, store => assert.deepEqual(store.export(record.proposal.id), record.exported));
  withStore(path, store => assert.equal(store.purge('2100-01-01T00:00:00.000Z'), 1));
  assert.deepEqual(counts(path), {proposals: 0, events: 0, requests: 0});
  withStore(path, store => {
    assert.deepEqual(store.list(), []);
    assert.throws(() => store.export(record.proposal.id), error => error.status === 404);
    assert.equal(store.execute(proposedCommand(), 'after-event-expiry').state, 'proposed');
  });
});

test('legacy retained-record overflow preserves full reads and retries until deletion restores admission', t => {
  const {path, records} = database(t, {retained: POLICY.maxRetainedProposals + 1, closed: true});
  const before = counts(path);
  assert.deepEqual(before, {proposals: 101, events: 202, requests: 202});
  withStore(path, store => {
    assert.throws(() => store.execute(proposedCommand(), 'denied-upgrade-growth'), denied(/cap/i));
  });
  assert.equal(schemaVersion(path), 1);
  withStore(path, store => {
    assert.equal(store.list().length, 101);
    for (const record of [records[0], records[100]]) {
      assert.deepEqual(store.export(record.proposal.id), record.exported);
      assert.deepEqual(store.execute(record.command, record.key), record.proposal);
    }
    assert.throws(() => store.execute(proposedCommand(), 'over-retained-cap'), denied(/cap/i));
  });
  assert.deepEqual(counts(path), before);
  withStore(path, store => store.delete(records[0].proposal.id));
  withStore(path, store => {
    assert.equal(store.list().length, 100);
    assert.throws(() => store.execute(proposedCommand(), 'at-retained-cap'), denied(/Retained/i));
    assert.deepEqual(store.export(records[100].proposal.id), records[100].exported);
    store.delete(records[1].proposal.id);
    assert.equal(store.execute(proposedCommand(), 'capacity-restored').state, 'proposed');
  });
  withStore(path, store => assert.equal(store.list().length, 100));
});

test('expiry removes every legacy over-cap retained record including audit and replay after upgrade', t => {
  const {path, records} = database(t, {retained: POLICY.maxRetainedProposals + 1, closed: true});
  withStore(path, store => assert.equal(store.list().length, 101));
  withStore(path, store => assert.equal(store.purge('2100-01-01T00:00:00.000Z'), 101));
  assert.deepEqual(counts(path), {proposals: 0, events: 0, requests: 0});
  withStore(path, store => {
    assert.deepEqual(store.list(), []);
    assert.throws(() => store.export(records[0].proposal.id), error => error.status === 404);
    assert.equal(store.execute(proposedCommand(), 'after-retained-expiry').state, 'proposed');
  });
});

test('legacy compatibility does not let structural reservation corruption bypass validation or expiry', t => {
  const {path, records: [record]} = database(t);
  const db = new DatabaseSync(path);
  try {
    const corrupt = {...record.exported.proposal, reservedUnits: 999};
    db.prepare('UPDATE proposals SET body=? WHERE id=?').run(JSON.stringify(corrupt), corrupt.id);
  } finally { db.close(); }
  const before = counts(path);
  withStore(path, store => {
    for (const action of [() => store.list(), () => store.export(record.proposal.id),
      () => store.delete(record.proposal.id), () => store.purge('2100-01-01T00:00:00.000Z')]) {
      assert.throws(action, error => error.status === 503 && /Invalid exchange state/.test(error.message));
    }
  });
  assert.deepEqual(counts(path), before);
});
