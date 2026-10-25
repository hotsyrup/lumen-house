// Synthetic, read-only viewer. No authentication, payment API, wallet or send path.
const $ = id => document.getElementById(id);
const element = (tag, value, className) => {
  const node = document.createElement(tag);
  if (value !== undefined && value !== null) node.textContent = String(value);
  if (className) node.className = className;
  return node;
};
const write = (id, value) => { $(id).textContent = String(value ?? 'Not recorded'); };
const entries = value => Array.isArray(value) ? value : value && typeof value === 'object' ? Object.values(value) : [];
const isAtomic = value => Number.isSafeInteger(value) && value >= 0;
const label = value => String(value ?? '').replaceAll('_', ' ').replaceAll('-', ' ');
const validDigest = value => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);

export function formatUsdc(value) {
  if (!isAtomic(value)) return 'Unknown';
  const fixed = (value / 1_000_000).toFixed(6);
  const [whole, fraction] = fixed.split('.');
  return `${whole}.${fraction.replace(/0+$/, '').padEnd(2, '0')}`;
}

function field(labelText, value, {wide = false, mono = false} = {}) {
  const wrapper = element('div', undefined, wide ? 'wide' : undefined);
  wrapper.append(element('dt', labelText), element('dd', value ?? 'Not recorded', mono ? 'mono' : undefined));
  return wrapper;
}

/** Render a request returned by the native request-only service.
 * Host authentication, source validation and current authorization stay outside
 * this renderer. It does not turn handoffAvailable into execution authority.
 */
export function renderPaymentRequest(container, request) {
  container.replaceChildren();
  if (!request || typeof request !== 'object' || !request.source) {
    const empty = element('article', undefined, 'empty-request');
    empty.append(element('h3', 'No payment request prepared.'), element('p', 'A current owner must review an accepted paid job before preparation can reserve its exact principal. Non-cash work does not become payable through this panel.'));
    container.append(empty);
    return;
  }
  const source = request.source;
  const card = element('article', undefined, 'request-card');
  const top = element('div', undefined, 'request-top');
  const amount = element('div');
  const amountText = element('p', formatUsdc(request.amountAtomic), 'request-amount');
  amountText.append(element('span', source.currency ?? 'USDC'));
  amount.append(element('p', 'EXACT ACCEPTED PRINCIPAL', 'eyebrow'), amountText,
    element('p', 'Held for this earned obligation · no payment sent', 'amount-note'));
  top.append(amount, element('span', 'AWAITING OWNER WALLET SETUP', 'draft-tag'));
  const content = element('div', undefined, 'request-content');
  const fields = element('dl', undefined, 'request-fields');
  fields.append(field('Native job', request.jobId ?? source.jobId), field('Worker identity', source.workerId),
    field('Owner / payer', request.ownerId ?? source.ownerId), field('Source network / asset', `${source.network ?? 'unknown'} / ${source.currency ?? 'unknown'} · ${source.decimals ?? 'unknown'} decimals`));
  const payee = field('Owner-attested payee', source.payee, {wide: true, mono: true});
  payee.append(element('p', 'Owner attestation only. Wallet ownership is not independently verified.', 'payee-note'));
  fields.append(payee);
  content.append(fields);
  const flags = element('div', undefined, 'source-strip');
  flags.append(element('span', 'Accepted paid record'), element('span', 'Record bindings checked'), element('span', 'Separate owner acceptance'));
  content.append(flags);
  content.append(element('p', 'Record bindings checked; artifact bytes unavailable. A stored digest and size do not verify the delivered content.', 'artifact-note'));
  const bindings = element('dl', undefined, 'binding-list');
  for (const [title, hash] of [['Paid terms SHA-256', source.termsHash], ['Terms document hash', source.termsDocumentHash], ['Recorded evidence hash', source.evidenceHash], ['Acceptance SHA-256', source.acceptanceHash]]) {
    const row = element('div', undefined, 'binding-row');
    row.append(element('dt', title), element('dd', validDigest(hash) ? hash : 'Binding unavailable', 'mono'));
    bindings.append(row);
  }
  const evidenceRecord = element('div', undefined, 'binding-row');
  evidenceRecord.append(element('dt', 'Native evidence record'), element('dd', `${source.evidenceId ?? 'unavailable'} · ${isAtomic(source.evidenceSize) ? source.evidenceSize : 'unknown'} bytes recorded · ${source.recordEvidenceClass ?? 'unavailable'} record`));
  const termsVersion = element('div', undefined, 'binding-row');
  termsVersion.append(element('dt', 'Paid terms version'), element('dd', source.termsVersion ?? 'Unavailable'));
  bindings.prepend(termsVersion);
  bindings.append(evidenceRecord);
  const day = request.dayWindow;
  const policyDay = element('div', undefined, 'binding-row');
  policyDay.append(element('dt', 'Origin policy day'), element('dd', day?.date ? `${day.date} · ${day.timezone ?? 'America/Los_Angeles'}` : 'Policy-day snapshot unavailable'));
  bindings.append(policyDay);
  content.append(bindings);
  const fullDigest = request.requestHash ?? request.requestDigest ?? request.digest;
  const digest = validDigest(fullDigest) ? fullDigest : request.sourceHash;
  const digestTitle = validDigest(fullDigest) ? 'Request binding SHA-256' : 'Source binding SHA-256';
  const digestBox = element('div', undefined, 'source-digest');
  const digestText = element('div');
  digestText.append(element('label', digestTitle), element('code', validDigest(digest) ? digest : 'Binding unavailable'));
  const copy = element('button', 'Copy digest', 'copy-button');
  copy.type = 'button';
  copy.disabled = !validDigest(digest);
  const copyStatus = element('p', '', 'copy-status');
  copyStatus.setAttribute('role', 'status');
  copyStatus.setAttribute('aria-live', 'polite');
  copy.addEventListener('click', async () => {
    try {
      if (!validDigest(digest) || !navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
      await navigator.clipboard.writeText(digest);
      copyStatus.textContent = 'Digest copied. This is not a payment instruction or wallet link.';
    } catch {
      copyStatus.textContent = 'Clipboard unavailable. Select the displayed digest to copy it manually.';
    }
  });
  digestBox.append(digestText, copy);
  content.append(digestBox, copyStatus);
  const wallet = element('div', undefined, 'wallet-boundary');
  wallet.append(element('h3', 'Ready for owner review; wallet handoff unavailable'),
    element('p', 'This native host has no verified mobile wallet integration. An actual payment requires your final cryptographic wallet confirmation of the exact payer, payee, asset, network, amount, and disclosed fees.'));
  const unavailable = element('button', undefined, 'wallet-button');
  unavailable.type = 'button';
  unavailable.disabled = true;
  unavailable.append(element('span', '↗'), document.createTextNode('Wallet confirmation unavailable'));
  unavailable.setAttribute('aria-describedby', 'wallet-fee-disclosure');
  const fees = element('p', undefined, 'fee-disclosure');
  fees.id = 'wallet-fee-disclosure';
  fees.append(element('span', 'Fees: unknown · unapproved'), element('span', 'Separate from worker principal'));
  wallet.append(unavailable, fees);
  card.append(top, content, wallet);
  container.append(card);
}

function requestFor(step) {
  if (step.request?.source) return step.request;
  const result = step.result?.request ?? step.result;
  if (result?.source) return result;
  const records = entries(step.state?.records);
  return records.find(row => row.id === step.requestId) ?? records.at(-1);
}

function meter(title, total, limit, held, settled) {
  const item = element('div', undefined, 'exposure-meter');
  const names = element('div', undefined, 'meter-labels');
  names.append(element('strong', title), element('span', isAtomic(total) ? `${formatUsdc(total)} / ${formatUsdc(limit)} USDC` : 'Policy snapshot unavailable'));
  const track = element('div', undefined, 'meter-track');
  track.setAttribute('role', 'img');
  track.setAttribute('aria-label', isAtomic(total) ? `${title}: ${formatUsdc(total)} USDC exposure out of ${formatUsdc(limit)}` : `${title}: current exposure unverified`);
  for (const [amount, className] of [[settled, 'meter-settled'], [held, 'meter-hold']]) {
    const segment = element('span', undefined, className);
    segment.style.width = `${isAtomic(amount) ? Math.min(100, amount / limit * 100) : 0}%`;
    track.append(segment);
  }
  const legend = element('div', undefined, 'meter-legend');
  legend.append(element('span', `${formatUsdc(held)} held`), element('span', `${formatUsdc(settled)} settled`));
  item.append(names, track, legend);
  return item;
}

function renderExposure(step) {
  const state = step.state ?? {};
  const policy = step.policy ?? step.result?.policy ?? (step.result?.exposure ? step.result : null);
  const exposure = policy?.exposure ?? step.exposure;
  const records = entries(state.records);
  const held = exposure?.heldPrincipalAtomic ?? records.reduce((sum, row) => sum + (isAtomic(row.principalHoldAtomic) ? row.principalHoldAtomic : 0), 0);
  const settled = exposure?.settledPrincipalAtomic ?? state.settledPrincipalAtomic;
  const lifetime = exposure?.lifetimeExposureAtomic ?? (isAtomic(settled) ? settled + held : null);
  const daily = exposure?.dailyExposureAtomic;
  const settledToday = exposure?.settledTodayAtomic;
  const dayLabel = exposure?.dayWindow?.date ? `LA policy day · ${exposure.dayWindow.date}` : 'Current LA policy day';
  $('exposure-meters').replaceChildren(meter(dayLabel, daily, 1_000_000, held, settledToday), meter('Lifetime principal', lifetime, 5_000_000, held, settled));
}

function selectStep(trace, index) {
  const step = trace.steps[index];
  document.querySelectorAll('.trace-step').forEach((button, position) => {
    if (position === index) button.setAttribute('aria-current', 'step');
    else button.removeAttribute('aria-current');
  });
  write('step-number', `RECORDED STEP ${String(index + 1).padStart(2, '0')} / ${label(step.actor ?? 'synthetic owner').toUpperCase()}`);
  write('step-title', step.label ?? `Recorded step ${index + 1}`);
  write('step-description', step.description ?? 'This snapshot records request preparation only. It does not confer signing or execution authority.');
  write('stop-indicator', step.state?.stopped === false ? 'SYNTHETIC PREPARATION OPEN' : 'PREPARATION STOPPED');
  renderPaymentRequest($('request-container'), requestFor(step));
  renderExposure(step);
  $('raw-state').textContent = JSON.stringify({state: step.state ?? null, result: step.result ?? null, policy: step.policy ?? null}, null, 2);
}

function renderTrace(trace) {
  if (!trace || !Array.isArray(trace.steps) || trace.steps.length < 1 || trace.steps.length > 200) throw new Error('A bounded generated trace is required');
  write('step-count', `${trace.steps.length} steps`);
  trace.steps.forEach((step, index) => {
    const button = element('button', undefined, 'trace-step');
    button.type = 'button';
    const text = element('span');
    text.append(element('strong', step.label ?? `Step ${index + 1}`), element('small', label(step.actor ?? 'synthetic owner')));
    button.append(element('span', String(index + 1).padStart(2, '0'), 'trace-index'), text);
    button.addEventListener('click', () => selectStep(trace, index));
    $('steps').append(button);
  });
  const checks = Array.isArray(trace.checks) ? trace.checks.slice(0, 100) : [];
  write('check-count', `${checks.filter(check => check.passed === true).length} / ${checks.length} passed`);
  checks.forEach(check => {
    const item = element('li');
    const mark = element('span', check.passed === true ? '✓' : '!', `check-mark${check.passed === true ? '' : ' failed'}`);
    mark.setAttribute('aria-label', check.passed === true ? 'Passed' : 'Failed or unverified');
    item.append(mark, element('span', check.name ?? 'Unnamed check'));
    $('checks').append(item);
  });
  const defaults = ['Current native identity, paid terms, owner acceptance, source bindings, and immutable principal holds must be verified in the host transaction.', 'Payee identity is owner-attested. Independent proof, canonical token/network validation, fees, and a secure wallet handoff remain separate readiness gates.', 'No production policy adoption, provisioning, funding, signing, sending, or deployment is established by this synthetic review.'];
  (Array.isArray(trace.limitations) && trace.limitations.length ? trace.limitations.slice(0, 30) : defaults).forEach(text => $('limitations').append(element('li', text)));
  $('review').hidden = false;
  $('load-status').hidden = true;
  selectStep(trace, 0);
}

if (typeof document !== 'undefined' && $('request-container')) {
  fetch('./trace.json', {credentials: 'omit', cache: 'no-store', redirect: 'error'})
    .then(async response => {
      if (!response.ok) throw new Error('Trace unavailable');
      const text = await response.text();
      if (text.length > 4_000_000) throw new Error('Trace exceeds local review bound');
      return JSON.parse(text);
    })
    .then(renderTrace)
    .catch(() => write('load-status', 'Synthetic trace unavailable. Generate the local request trace before review. Production preparation and payment execution remain disabled.'));
}
