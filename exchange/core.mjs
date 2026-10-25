// Runtime-neutral negotiation rules. Adapters own transactions and authentication.
export const POLICY = Object.freeze({
  mode: 'demo', realEnabled: false, treasuryEnabled: false,
  automaticPayouts: false, privateAccess: false,
  serviceCreditEnabled: false, barterExecutionEnabled: false, maxOpen: 10,
  maxUnitsPerProposal: 5, totalReservedUnits: 10, retentionDays: 7,
  maxRetainedProposals: 100, maxEventsPerProposal: 100,
});

export class ExchangeError extends Error {
  constructor(message, status = 400) { super(message); this.status = status; }
}

const requireThat = (condition, message, status) => {
  if (!condition) throw new ExchangeError(message, status);
};
const copy = value => JSON.parse(JSON.stringify(value));
const terminal = state => ['completed', 'cancelled'].includes(state);

function text(value, name, max = 1000) {
  requireThat(typeof value === 'string' && value.trim().length > 0 && value.length <= max, `Invalid ${name}`);
  return value.trim();
}

function exact(object, keys) {
  requireThat(object && typeof object === 'object' && !Array.isArray(object), 'Expected object');
  requireThat(Object.keys(object).every(key => keys.includes(key)), 'Unknown fields are not collected');
}

function strings(value, name) {
  requireThat(Array.isArray(value) && value.length > 0 && value.length <= 10, `Invalid ${name}`);
  return value.map(item => text(item, name, 300));
}

// Exact canonical intent avoids probabilistic hash collisions and field-order
// conflicts. This is content-bearing replay data, deleted with its proposal.
function fingerprint(command) {
  const sort = value => Array.isArray(value) ? value.map(sort) :
    value && typeof value === 'object' ? Object.fromEntries(Object.keys(value).sort().map(key => [key, sort(value[key])])) : value;
  try { return JSON.stringify(sort(JSON.parse(JSON.stringify(command)))); }
  catch { throw new ExchangeError('Command must contain JSON data'); }
}

export const emptyState = () => ({version: 1, proposals: [], events: [], requests: []});

function validateState(state) {
  requireThat(state && state.version === 1 && ['proposals', 'events', 'requests'].every(key => Array.isArray(state[key])), 'Unsupported exchange schema', 503);
  const ids = new Set();
  const validTime = value => typeof value === 'string' && Number.isFinite(Date.parse(value)) && new Date(value).toISOString() === value;
  const states = ['proposed', 'counteroffered', 'agreed', 'delivered', 'accepted', 'completed', 'cancelled'];
  for (const proposal of state.proposals) {
    requireThat(proposal && typeof proposal.id === 'string' && !ids.has(proposal.id) &&
      validTime(proposal.createdAt) && validTime(proposal.expiresAt) && validTime(proposal.updatedAt) &&
      proposal.expiresAt > proposal.createdAt && states.includes(proposal.state) &&
      proposal.mode === 'demo' && proposal.authority === 'proposal-only' && proposal.sharingConsent === true &&
      proposal.provenance?.rights === 'synthetic-demo-only' && Number.isInteger(proposal.termsRevision) && proposal.termsRevision >= 1 &&
      ['barter', 'service-credit-proposal'].includes(proposal.terms?.kind) &&
      Number.isInteger(proposal.terms?.units) && proposal.terms.units >= 1 && proposal.terms.units <= POLICY.maxUnitsPerProposal &&
      proposal.reservedUnits === (['agreed', 'delivered', 'accepted'].includes(proposal.state) ? proposal.terms.units : 0), 'Invalid exchange state', 503);
    ids.add(proposal.id);
  }
  requireThat(state.proposals.length <= POLICY.maxRetainedProposals &&
    state.proposals.filter(proposal => !terminal(proposal.state)).length <= POLICY.maxOpen &&
    state.proposals.reduce((total, proposal) => total + proposal.reservedUnits, 0) <= POLICY.totalReservedUnits, 'Invalid exchange state', 503);
  const keys = new Set();
  for (const row of state.requests) {
    requireThat(row && ids.has(row.proposalId) && typeof row.key === 'string' && row.key.length > 0 && !keys.has(row.key) &&
      typeof row.fingerprint === 'string' && row.response?.id === row.proposalId && row.response.mode === 'demo', 'Invalid exchange state', 503);
    keys.add(row.key);
  }
  requireThat(state.events.every(row => row && ids.has(row.proposalId) && row.event?.snapshot?.id === row.proposalId &&
    ['propose', 'counteroffer', 'agree', 'deliver', 'accept', 'complete', 'cancel', 'escalate'].includes(row.event.action) &&
    ['requester', 'provider', 'owner'].includes(row.event.actor) && validTime(row.event.at)) &&
    state.proposals.every(proposal => {
      const count = state.events.filter(row => row.proposalId === proposal.id).length;
      return count >= 1 && count <= POLICY.maxEventsPerProposal;
    }) && state.events.length === state.requests.length, 'Invalid exchange state', 503);
}

function erase(state, id) {
  state.proposals = state.proposals.filter(proposal => proposal.id !== id);
  state.events = state.events.filter(row => row.proposalId !== id);
  state.requests = state.requests.filter(row => row.proposalId !== id);
}

function get(state, id) {
  const proposal = state.proposals.find(item => item.id === text(id, 'id', 100));
  requireThat(proposal, 'Proposal not found', 404);
  return proposal;
}

function commandResult(state, command, key, {now, id, legacyFingerprint}) {
  text(key, 'idempotency key', 100);
  exact(command, ['action', 'id', 'actor', 'input']);
  requireThat(['requester', 'provider', 'owner'].includes(command.actor), 'Demo actor required');
  const intent = fingerprint(command);
  const prior = state.requests.find(row => row.key === key);
  if (prior) {
    // SQLite rows written by the original prototype used serialized SHA-256.
    requireThat(prior.fingerprint === intent ||
      (typeof legacyFingerprint === 'function' && prior.fingerprint === legacyFingerprint(command)), 'Idempotency key reused with different command', 409);
    return copy(prior.response);
  }

  const {action, actor} = command;
  let proposal;
  if (action === 'propose') {
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
    requireThat(state.proposals.filter(item => !terminal(item.state)).length < POLICY.maxOpen, 'Open proposal cap exceeded', 409);
    requireThat(state.proposals.length < POLICY.maxRetainedProposals, 'Retained proposal cap exceeded; delete or expire records first', 409);
    const proposalId = text(typeof id === 'function' ? id() : id || globalThis.crypto.randomUUID(), 'generated id', 100);
    requireThat(!state.proposals.some(item => item.id === proposalId), 'Generated id collision', 409);
    proposal = {
      id: proposalId, mode: 'demo', agent: text(input.agent, 'agent', 100),
      currentTask: text(input.currentTask, 'current task'), needs: strings(input.needs, 'needs'),
      missingResources: strings(input.missingResources, 'resources'), offers: strings(input.offers, 'offers'),
      authority: 'proposal-only',
      terms: {kind: input.terms.kind, description: text(input.terms.description, 'terms'), units: input.terms.units},
      evaluation: Object.fromEntries(['cost', 'usefulness', 'verifiability'].map(key => [key, text(input.evaluation[key], key)])),
      sharingConsent: true, provenance: {source: text(input.provenance.source, 'source'), rights: 'synthetic-demo-only'},
      state: 'proposed', termsRevision: 1, reservedUnits: 0, createdAt: now,
      expiresAt: new Date(Date.parse(now) + POLICY.retentionDays * 86400000).toISOString(),
    };
    state.proposals.push(proposal);
  } else {
    proposal = get(state, command.id);
    const transitions = {
      counteroffer: ['proposed', 'counteroffered'], agree: ['proposed', 'counteroffered'],
      deliver: ['agreed'], accept: ['delivered'], complete: ['accepted'],
      cancel: ['proposed', 'counteroffered', 'agreed', 'delivered', 'accepted'],
      escalate: ['proposed', 'counteroffered', 'agreed', 'delivered', 'accepted'],
    };
    requireThat(Object.hasOwn(transitions, action) && transitions[action].includes(proposal.state), 'Invalid state transition', 409);
    const roles = {
      counteroffer: ['provider', 'requester'], agree: ['owner'], deliver: ['provider'],
      accept: ['requester'], complete: ['owner'], cancel: ['owner', 'provider', 'requester'],
      escalate: ['owner', 'provider', 'requester'],
    };
    requireThat(roles[action].includes(actor), 'Wrong demo actor for transition', 403);
    const eventCount = state.events.filter(row => row.proposalId === proposal.id).length;
    // Reserve a final event for cancellation so a full history cannot trap units.
    requireThat(eventCount < POLICY.maxEventsPerProposal - (action === 'cancel' ? 0 : 1), 'Proposal event cap exceeded; cancel or delete the demo record', 409);
    const input = command.input || {};
    if (action === 'counteroffer') {
      exact(input, ['description']);
      proposal.terms.description = text(input.description, 'counteroffer');
      proposal.termsRevision += 1;
      proposal.state = 'counteroffered';
    }
    if (action === 'agree') {
      exact(input, ['requesterConfirmed', 'providerConfirmed', 'expectedTermsRevision']);
      requireThat(input.requesterConfirmed === true && input.providerConfirmed === true, 'Both parties must confirm current terms');
      requireThat(input.expectedTermsRevision === proposal.termsRevision, 'Terms changed; review current proposal before agreement', 409);
      const reserved = state.proposals.reduce((sum, item) => sum + item.reservedUnits, 0);
      requireThat(reserved + proposal.terms.units <= POLICY.totalReservedUnits, 'Demo reservation cap exceeded', 409);
      proposal.reservedUnits = proposal.terms.units;
      proposal.state = 'agreed';
      proposal.agreement = {termsRevision: proposal.termsRevision, terms: {...proposal.terms}, requesterConfirmed: true, providerConfirmed: true, at: now, recordedBy: actor};
    }
    if (action === 'deliver') {
      exact(input, ['evidence']); proposal.evidence = text(input.evidence, 'evidence'); proposal.state = 'delivered';
    }
    if (action === 'accept') {
      exact(input, ['assessment']); proposal.acceptance = text(input.assessment, 'assessment'); proposal.state = 'accepted';
    }
    if (action === 'complete' || action === 'cancel') {
      exact(input, ['reason']); proposal.resolution = text(input.reason, 'reason');
      proposal.state = action === 'complete' ? 'completed' : 'cancelled'; proposal.reservedUnits = 0;
    }
    if (action === 'escalate') {
      exact(input, ['reason']); proposal.escalation = {reason: text(input.reason, 'reason'), at: now, status: 'owner-review-required'};
    }
  }
  proposal.updatedAt = now;
  state.events.push({proposalId: proposal.id, event: {action, actor, at: now, snapshot: copy(proposal)}});
  state.requests.push({key, fingerprint: intent, proposalId: proposal.id, response: copy(proposal)});
  return copy(proposal);
}

// A successful result contains all writes, including expiry, for one atomic commit.
// The input state is never mutated, including when validation rejects an operation.
export function executeOperation(inputState, operation, options = {}) {
  validateState(inputState);
  const state = copy(inputState);
  const clock = new Date(options.now ?? Date.now());
  requireThat(Number.isFinite(clock.getTime()), 'Invalid operation time');
  const now = clock.toISOString();
  const expired = state.proposals.filter(proposal => proposal.expiresAt <= now).map(proposal => proposal.id);
  for (const id of expired) erase(state, id);
  let result;
  switch (operation?.kind) {
    case 'purge': result = expired.length; break;
    case 'list': result = copy([...state.proposals].reverse()); break;
    case 'export': {
      const proposal = get(state, operation.id);
      result = {proposal: copy(proposal), events: state.events.filter(row => row.proposalId === proposal.id).map(row => copy(row.event))};
      break;
    }
    case 'delete': get(state, operation.id); erase(state, operation.id); result = undefined; break;
    case 'command': result = commandResult(state, operation.command, operation.key, {...options, now}); break;
    default: throw new ExchangeError('Unknown exchange operation');
  }
  return {state, result};
}

export function executeCommand(state, command, key, options) {
  return executeOperation(state, {kind: 'command', command, key}, options);
}
