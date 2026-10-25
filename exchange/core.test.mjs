import test from 'node:test';
import assert from 'node:assert/strict';
import {emptyState, executeOperation, executeCommand, POLICY} from './core.mjs';

const NOW = '2026-10-25T12:00:00.000Z';
const input = () => ({
  mode: 'demo', agent: 'Synthetic agent', currentTask: 'Review a fictional handoff',
  needs: ['Manifest check'], missingResources: ['Synthetic file list'], offers: ['Test case review'],
  authority: 'proposal-only', terms: {kind: 'barter', description: 'Fictional review exchange', units: 1},
  evaluation: {cost: 'Unknown', usefulness: 'Check completeness', verifiability: 'Compare synthetic hashes'},
  sharingConsent: true, provenance: {source: 'Fictional test fixture', rights: 'synthetic-demo-only'},
});
const propose = () => ({action: 'propose', actor: 'requester', input: input()});
const rejects = status => error => error.status === status;

function harness() {
  let state = emptyState(), sequence = 0;
  return {
    get state() { return structuredClone(state); },
    send(command, key = `command-${++sequence}`) {
      const outcome = executeCommand(state, command, key, {now: NOW, id: () => `proposal-${sequence}`});
      state = outcome.state;
      return outcome.result;
    },
    op(operation, now = NOW) {
      const outcome = executeOperation(state, operation, {now});
      state = outcome.state;
      return outcome.result;
    },
  };
}

test('portable core preserves separate lifecycle evidence and returns detached results', () => {
  const h = harness();
  const p = h.send(propose());
  p.agent = 'Mutating the result is not a write';
  h.send({action: 'counteroffer', id: p.id, actor: 'provider', input: {description: 'Only fictional listed files'}});
  h.send({action: 'agree', id: p.id, actor: 'owner', input: {requesterConfirmed: true, providerConfirmed: true, expectedTermsRevision: 2}});
  h.send({action: 'deliver', id: p.id, actor: 'provider', input: {evidence: 'Synthetic two-file hash comparison'}});
  h.send({action: 'accept', id: p.id, actor: 'requester', input: {assessment: 'Both synthetic checks match'}});
  const result = h.send({action: 'complete', id: p.id, actor: 'owner', input: {reason: 'Fictional criteria met'}});
  assert.equal(result.state, 'completed');
  assert.equal(result.reservedUnits, 0);
  assert.equal(result.agent, 'Synthetic agent');
  const exported = h.op({kind: 'export', id: p.id});
  assert.deepEqual(exported.events.map(event => event.action), ['propose', 'counteroffer', 'agree', 'deliver', 'accept', 'complete']);
  assert.equal(exported.events[0].snapshot.terms.description, input().terms.description);
  assert.equal(exported.proposal.agreement.termsRevision, 2);
  assert.match(exported.proposal.evidence, /comparison/);
  assert.match(exported.proposal.acceptance, /match/);
});

test('portable retries ignore object field order while rejecting changed intent', () => {
  const state = emptyState(), command = propose();
  const first = executeCommand(state, command, 'same', {now: NOW, id: 'synthetic-id'});
  const reordered = {input: Object.fromEntries(Object.entries(command.input).reverse()), actor: command.actor, action: command.action};
  const second = executeCommand(first.state, reordered, 'same', {now: NOW});
  assert.deepEqual(second.result, first.result);
  assert.deepEqual(second.state, first.state);
  assert.throws(() => executeCommand(first.state, {...command, input: {...command.input, agent: 'Changed intent'}}, 'same', {now: NOW}), rejects(409));
});

test('failed transitions leave the supplied transaction snapshot unchanged', () => {
  const first = executeCommand(emptyState(), propose(), 'new', {now: NOW, id: 'synthetic-id'});
  const before = structuredClone(first.state);
  assert.throws(() => executeCommand(first.state, {action: 'deliver', actor: 'provider', id: 'synthetic-id', input: {evidence: 'Too early'}}, 'bad', {now: NOW}), rejects(409));
  assert.deepEqual(first.state, before);
  assert.equal(first.state.events.length, 1);
  assert.equal(first.state.requests.length, 1);
});

test('expiry and deletion remove content, audit and canonical retry intent together', () => {
  const first = executeCommand(emptyState(), propose(), 'new', {now: NOW, id: 'synthetic-id'});
  const deleted = executeOperation(first.state, {kind: 'delete', id: 'synthetic-id'}, {now: NOW});
  assert.deepEqual(deleted.state, emptyState());
  assert.deepEqual(executeOperation(first.state, {kind: 'purge'}, {now: '2026-11-01T12:00:00.000Z'}), {state: emptyState(), result: 1});
  assert.throws(() => executeOperation(first.state, {kind: 'export', id: 'synthetic-id'}, {now: '2026-11-01T12:00:00.000Z'}), rejects(404));
});

test('unknown schema and corrupt reservations fail closed without resetting records', () => {
  const first = executeCommand(emptyState(), propose(), 'new', {now: NOW, id: 'synthetic-id'});
  for (const corrupt of [
    {...first.state, version: 2},
    {...first.state, requests: []},
    {...first.state, proposals: [{...first.result, reservedUnits: 999}]},
    {...first.state, proposals: [{...first.result, state: 'paid'}]},
  ]) {
    const before = structuredClone(corrupt);
    assert.throws(() => executeOperation(corrupt, {kind: 'list'}, {now: NOW}), rejects(503));
    assert.deepEqual(corrupt, before);
  }
});

test('finite history cap leaves a cancellation slot and releases agreed units', () => {
  const h = harness(), p = h.send(propose());
  h.send({action: 'agree', id: p.id, actor: 'owner', input: {requesterConfirmed: true, providerConfirmed: true, expectedTermsRevision: 1}});
  for (let index = 2; index < POLICY.maxEventsPerProposal - 1; index++) {
    h.send({action: 'escalate', id: p.id, actor: 'requester', input: {reason: `Synthetic review ${index}`}});
  }
  assert.equal(h.state.events.length, POLICY.maxEventsPerProposal - 1);
  assert.throws(() => h.send({action: 'escalate', id: p.id, actor: 'owner', input: {reason: 'Over history cap'}}), rejects(409));
  const cancelled = h.send({action: 'cancel', id: p.id, actor: 'owner', input: {reason: 'Release bounded synthetic capacity'}});
  assert.equal(cancelled.reservedUnits, 0);
  assert.equal(h.state.events.length, POLICY.maxEventsPerProposal);
});

test('retained-record cap bounds closed demo history until export and deletion', () => {
  const h = harness();
  let first;
  for (let index = 0; index < POLICY.maxRetainedProposals; index++) {
    const p = h.send(propose());
    first ??= p;
    h.send({action: 'cancel', id: p.id, actor: 'requester', input: {reason: 'Synthetic closed example'}});
  }
  assert.throws(() => h.send(propose()), error => error.status === 409 && /Retained/.test(error.message));
  assert.equal(h.op({kind: 'export', id: first.id}).events.length, 2);
  h.op({kind: 'delete', id: first.id});
  assert.equal(h.send(propose()).state, 'proposed');
});

test('service-credit and barter descriptions grant no execution policy', () => {
  assert.equal(POLICY.serviceCreditEnabled, false);
  assert.equal(POLICY.barterExecutionEnabled, false);
  assert.equal(POLICY.realEnabled, false);
  const h = harness(), command = propose();
  command.input.terms.kind = 'service-credit-proposal';
  const proposal = h.send(command);
  assert.equal(proposal.authority, 'proposal-only');
  assert.equal(proposal.reservedUnits, 0);
});
