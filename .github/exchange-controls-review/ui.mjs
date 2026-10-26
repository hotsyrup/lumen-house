// Read-only local fixture viewer. These buttons never issue exchange commands.
const byId = id => document.getElementById(id);
const write = (id, value) => { byId(id).textContent = String(value ?? 'Not recorded'); };
const node = (tag, value, className) => {
  const item = document.createElement(tag);
  if (value !== undefined) item.textContent = String(value);
  if (className) item.className = className;
  return item;
};
const values = value => Array.isArray(value) ? value : value && typeof value === 'object' ? Object.values(value) : [];
const number = value => Number.isFinite(Number(value)) ? Number(value) : 0;
const human = value => String(value ?? '').replaceAll('_', ' ').replaceAll('-', ' ');
const present = value => value === true || (value !== null && value !== undefined && value !== false);

function proposalFor(step) {
  if (step.proposal && typeof step.proposal === 'object') return step.proposal;
  const result = step.result?.proposal ?? step.result?.record ?? step.result;
  const records = values(step.state?.records ?? step.state?.proposals);
  const id = step.proposalId ?? result?.id;
  return records.find(record => record.id === id) ?? records.at(-1) ?? (result?.terms ? result : {});
}

function assentText(proposal, side) {
  const subject = proposal[side + 'Id'];
  const assent = proposal.assents?.[side] ?? proposal.assents?.[subject];
  return present(assent) ? `Recorded · ${subject ?? side}` : 'Awaiting independent assent';
}

function renderResources(state, proposal) {
  const container = byId('resources');
  container.replaceChildren();
  const resources = state.resources ?? state.policy?.resources ?? {};
  const records = values(state.records ?? state.proposals);
  const entries = Array.isArray(resources) ? resources.map(item => [item.id ?? item.resourceId, item]) : Object.entries(resources);
  if (!entries.length) {
    container.append(node('p', 'No resources or positive limits are configured. Defaults grant nothing.', 'muted'));
    return;
  }
  for (const [id, resource] of entries) {
    const capacity = number(typeof resource === 'number' ? resource : resource.capacity ?? resource.limit ?? resource.total);
    const matching = records.filter(record => record.terms?.resourceId === id);
    const reserved = number(resource?.reserved ?? matching.reduce((sum, record) => sum + number(record.reservedUnits), 0));
    const consumed = number(resource?.consumed ?? resource?.used ?? state.consumed?.[id] ?? state.usage?.[id] ?? matching.reduce((sum, record) => sum + number(record.consumedUnits), 0));
    const item = node('div', undefined, 'resource');
    const heading = node('div', undefined, 'resource-heading');
    heading.append(node('strong', id), node('span', `${Math.max(0, capacity - reserved - consumed)} available / ${capacity} limit`));
    const bar = node('div', undefined, 'resource-bar');
    bar.setAttribute('role', 'img');
    bar.setAttribute('aria-label', `${id}: ${reserved} reserved, ${consumed} consumed, ${capacity} limit`);
    for (const [amount, className] of [[consumed, 'resource-used'], [reserved, 'resource-reserved']]) {
      const segment = node('span', undefined, className);
      segment.style.width = `${capacity ? Math.max(0, Math.min(100, amount / capacity * 100)) : 0}%`;
      bar.append(segment);
    }
    const legend = node('div', undefined, 'legend');
    legend.append(node('span', `${consumed} consumed`), node('span', `${reserved} reserved`));
    item.append(heading, bar, legend);
    container.append(item);
  }
}

function renderStep(trace, index) {
  const step = trace.steps[index];
  const state = step.state ?? {};
  const proposal = proposalFor(step);
  document.querySelectorAll('.step').forEach((button, i) => {
    if (i === index) button.setAttribute('aria-current', 'step');
    else button.removeAttribute('aria-current');
  });
  write('step-actor', `STEP ${String(index + 1).padStart(2, '0')} / ${human(step.actor ?? 'recorded system action').toUpperCase()}`);
  write('selected-label', step.label ?? `Recorded step ${index + 1}`);
  write('proposal-status', human(proposal.status ?? proposal.state ?? (state.stopped ? 'stopped' : 'configured')));
  write('selected-description', step.description ?? proposal.terms?.description ?? 'Owner-defined limits and current identity checks remain separate requirements for every operation.');
  write('requester-assent', assentText(proposal, 'requester'));
  write('provider-assent', assentText(proposal, 'provider'));
  const approved = proposal.approved ?? proposal.approval ?? proposal.ownerApproval ?? proposal.agreement;
  const completed = (proposal.status ?? proposal.state) === 'completed';
  write('owner-decision', completed ? 'Delivery accepted by owner' : present(approved) || ['agreed', 'delivered', 'disputed'].includes(proposal.status ?? proposal.state) ? 'Exact terms approved' : 'Awaiting owner approval');
  write('terms-revision', proposal.termsRevision ?? 'No proposal yet');
  write('policy-revision', proposal.policyRevision ?? state.policyRevision ?? 'No policy yet');
  write('terms-hash', proposal.termsHash ?? 'No assent binding recorded');
  const delivery = proposal.evidence ?? proposal.delivery;
  write('evidence-status', delivery ? `Revision ${proposal.deliveryRevision ?? delivery.deliveryRevision ?? delivery.revision ?? 1}` : 'AWAITING DELIVERY');
  write('evidence-description', delivery?.description ?? delivery?.evidence?.description ?? 'A claimed delivery is not acceptance. The provider must record bounded evidence before the owner can accept it.');
  write('evidence-hash', delivery?.sha256 ?? delivery?.evidence?.sha256 ?? proposal.deliveryHash ?? 'No delivery evidence recorded');
  const acceptance = proposal.acceptance ?? proposal.ownerAcceptance;
  write('acceptance', acceptance ? acceptance.assessment ?? acceptance.reason ?? 'Recorded against the exact evidence and terms' : 'No owner acceptance recorded');
  write('stop-status', state.stopped === false ? 'Synthetic policy open in this trace only' : 'Stop switch closed · further exchange blocked');
  const retention = state.policy?.limits?.retentionMs ?? state.policy?.retentionMs ?? state.limits?.retentionMs;
  write('retention-status', retention ? `Bounded content retention · ${number(retention) / 3600000} hours` : 'Retention must be explicitly configured');
  byId('raw-state').textContent = JSON.stringify({state, result: step.result ?? null}, null, 2);
  renderResources(state, proposal);
}

function render(trace) {
  if (!trace || !Array.isArray(trace.steps) || trace.steps.length === 0 || trace.steps.length > 200) throw new Error('A bounded generated trace is required.');
  write('step-count', `${trace.steps.length} events`);
  const steps = byId('steps');
  trace.steps.forEach((step, index) => {
    const button = node('button', undefined, 'step');
    button.type = 'button';
    const label = node('span');
    label.append(node('strong', step.label ?? `Step ${index + 1}`), node('small', human(step.actor ?? 'system')));
    button.append(node('span', String(index + 1).padStart(2, '0'), 'step-index'), label);
    button.addEventListener('click', () => renderStep(trace, index));
    steps.append(button);
  });
  const checks = Array.isArray(trace.checks) ? trace.checks.slice(0, 100) : [];
  write('check-count', `${checks.filter(check => check.passed === true).length} / ${checks.length} passed`);
  checks.forEach(check => {
    const item = node('li');
    const passed = check.passed === true;
    const icon = node('span', passed ? '✓' : '!', `check-icon${passed ? '' : ' failed'}`);
    icon.setAttribute('aria-label', passed ? 'Passed' : 'Failed or unverified');
    item.append(icon, node('span', check.name ?? 'Unnamed check'));
    byId('checks').append(item);
  });
  const limitations = Array.isArray(trace.limitations) && trace.limitations.length ? trace.limitations.slice(0, 30) : [
    'Synthetic trace checks do not verify deployed identity, storage, configuration, or current live behavior.',
    'Provisioning, real participant grants, offered resources, and launch require separate owner approval.',
    'Payments, wallet execution, and automatic resource granting remain disabled.',
  ];
  limitations.forEach(value => byId('limitations').append(node('li', value)));
  byId('review').hidden = false;
  byId('load-status').hidden = true;
  renderStep(trace, 0);
}

fetch('./trace.json', {cache: 'no-store', credentials: 'omit', redirect: 'error'})
  .then(response => { if (!response.ok) throw new Error('Trace unavailable.'); return response.json(); })
  .then(render)
  .catch(() => write('load-status', 'Generated trace unavailable. Run the local synthetic trace generator before review. Production remains disabled.'));
