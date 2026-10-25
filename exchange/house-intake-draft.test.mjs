import test from 'node:test';
import assert from 'node:assert/strict';
import { createHouseIntakeDraft } from './house-intake-draft.mjs';

function proposal() {
  return {
    id: 'not-exported-native-id', mode: 'demo', agent: 'Fictional review agent',
    currentTask: 'Prepare a synthetic deliverable', needs: ['Manifest verification'],
    missingResources: ['Offline checker'], offers: ['Fictional test cases'],
    authority: 'proposal-only', sharingConsent: true,
    terms: { kind: 'service-credit-proposal', description: 'Discuss one fictional check', units: 5 },
    evaluation: { cost: 'Unknown', usefulness: 'Reduce ambiguity', verifiability: 'Recompute hashes' },
    provenance: { source: 'Synthetic user example', rights: 'synthetic-demo-only' },
    createdAt: '2026-10-25T00:00:00.000Z', expiresAt: '2026-11-01T00:00:00.000Z',
  };
}
const draft = value => createHouseIntakeDraft(value, {localExportConfirmed: true});

test('offline draft uses supported specific mode while preserving unknown requirements', () => {
  const result = draft(proposal());
  assert.equal(result.status, 'incomplete-offline-owner-review');
  assert.equal(result.recordDraft.mode, 'specific');
  assert.equal(typeof result.recordDraft.mode, 'string');
  assert.equal(result.recordDraft.smoke_test, true);
  assert.deepEqual(result.recordDraft.brief, {
    budget: null, deliverable: null, interests: ['Manifest verification'],
    acceptance_criteria: [], desired_by: null, exclusions: [], licensing_needs: null,
  });
  assert.ok(result.missingRequirements.includes('native identity and scoped authorization'));
  assert.equal(JSON.stringify(result).includes('"units"'), false);
});

test('source consent does not substitute for the explicit local export action', () => {
  assert.throws(() => createHouseIntakeDraft(proposal()), /Explicit local export/);
  assert.throws(() => createHouseIntakeDraft(proposal(), {localExportConfirmed: 'true'}), /Explicit local export/);
  for (const change of [{mode: 'real'}, {authority: 'execute'}, {sharingConsent: false},
    {provenance: {source: 'Example', rights: 'public'}}]) {
    assert.throws(() => draft({...proposal(), ...change}));
  }
});

test('projection minimizes IDs, audit, evidence pointers and unrelated credential fields', () => {
  const source = {...proposal(), credential: 'private-fixture-sentinel',
    evidence: 'https://private.invalid/customer', events: [{private: 'fixture'}],
    agreement: {requesterConfirmed: true}, reservedUnits: 5};
  const before = structuredClone(source);
  const result = draft(source);
  assert.deepEqual(source, before);
  const encoded = JSON.stringify(result);
  for (const omitted of ['private-fixture-sentinel', 'private.invalid', 'not-exported-native-id',
    'events', 'agreement', 'requesterConfirmed', 'reservedUnits']) assert.equal(encoded.includes(omitted), false);
  assert.equal(result.exchangeContext.provenance.rights, 'synthetic-demo-only');
  result.exchangeContext.offers.push('Changed output');
  assert.deepEqual(source.offers, before.offers);
});

test('selected facts reject external pointers and common credential forms', () => {
  for (const pointer of ['https://private.invalid/customer', 'http://127.0.0.1/private',
    'file:///tmp/private', 'data:text/plain,fixture', 'www.example.invalid',
    'someone@example.invalid', 'Bearer fictional_token', 'api_key=fictional_token']) {
    assert.throws(() => draft({...proposal(), offers: [pointer]}), /without URLs, contacts or credentials/);
  }
});

test('submission and every execution channel stay independently disabled', () => {
  const result = draft({...proposal(), realEnabled: true, target: 'https://private.invalid',
    treasuryEnabled: true, automaticPayouts: true, privateAccess: true});
  assert.equal(result.submissionEnabled, false);
  assert.equal(result.target, null);
  assert.deepEqual(result.execution, {
    realEnabled: false, treasuryEnabled: false, automaticPayouts: false,
    privateAccess: false, serviceCreditEnabled: false, barterExecutionEnabled: false,
    humanTimeCommitted: false,
  });
  assert.equal(result.exchangeContext.consent.nativeSubmission, false);
  assert.equal(result.exchangeContext.consent.publication, false);
  const inspect = value => {
    assert.notEqual(typeof value, 'function');
    if (value && typeof value === 'object') Object.values(value).forEach(inspect);
  };
  inspect(result);
});

test('retention cannot widen and exported copies do not claim automatic erasure', () => {
  const result = draft(proposal());
  assert.equal(result.exchangeContext.retention.sourceExpiresAt, proposal().expiresAt);
  assert.equal(result.exchangeContext.retention.exportedCopyAutoDeletion, false);
  assert.match(result.exchangeContext.retention.deletion, /logical deletion/);
  assert.throws(() => draft({...proposal(), expiresAt: '2026-11-02T00:00:00.000Z'}), /cannot extend/);
  assert.throws(() => draft({...proposal(), expiresAt: '2026-10-24T00:00:00.000Z'}), /cannot extend/);
  assert.throws(() => draft({...proposal(), expiresAt: 'tomorrow'}), /Invalid review expiry/);
});
