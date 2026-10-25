import { ExchangeError, POLICY } from './core.mjs';

const requireThat = (condition, message) => {
  if (!condition) throw new ExchangeError(message);
};

function reviewText(value, label, maximum = 1000) {
  requireThat(typeof value === 'string' && value.trim().length > 0 && value.length <= maximum,
    `Invalid review ${label}`);
  // The review draft needs fictional structured facts, not external pointers or
  // credential material. These checks reject common forms, not arbitrary secrets.
  requireThat(!/[a-z][a-z0-9+.-]*:\/\/|\b(?:file|data|javascript):|\bwww\.|[\w.+-]+@[\w.-]+\.[a-z]{2,}/i.test(value)
    && !/\bBearer\s+\S+|\b(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*\S+/i.test(value),
  `Review ${label} must contain synthetic facts without URLs, contacts or credentials`);
  return value.trim();
}

function reviewList(value, label) {
  requireThat(Array.isArray(value) && value.length >= 1 && value.length <= 10, `Invalid review ${label}`);
  return value.map(item => reviewText(item, label, 300));
}

function timestamp(value, label) {
  requireThat(typeof value === 'string' && Number.isFinite(Date.parse(value))
    && new Date(value).toISOString() === value, `Invalid review ${label}`);
  return value;
}

/**
 * Build an incomplete, offline owner-review draft from a synthetic proposal.
 * The caller must first enforce source expiry and require an explicit local
 * export action. This pure function does not open storage or submit anything.
 */
export function createHouseIntakeDraft(proposal, { localExportConfirmed = false } = {}) {
  requireThat(localExportConfirmed === true, 'Explicit local export confirmation required');
  requireThat(proposal && typeof proposal === 'object' && !Array.isArray(proposal), 'Synthetic proposal required');
  requireThat(proposal.mode === 'demo' && proposal.authority === 'proposal-only', 'Only proposal-only demo review is supported');
  requireThat(proposal.sharingConsent === true && proposal.provenance?.rights === 'synthetic-demo-only',
    'Synthetic provenance and local sharing consent required');
  requireThat(['barter', 'service-credit-proposal'].includes(proposal.terms?.kind), 'Unsupported review proposal kind');

  const createdAt = timestamp(proposal.createdAt, 'creation time');
  const expiresAt = timestamp(proposal.expiresAt, 'expiry');
  requireThat(Date.parse(expiresAt) > Date.parse(createdAt)
    && Date.parse(expiresAt) - Date.parse(createdAt) <= POLICY.retentionDays * 86400000,
  'Review draft cannot extend source retention');

  const needs = reviewList(proposal.needs, 'needs');
  return {
    status: 'incomplete-offline-owner-review',
    submissionEnabled: false,
    target: null,
    // "specific" is the supported intake mode for a concrete need. Its brief
    // remains incomplete; current task and verifiability are not silently
    // converted into a deliverable or independently accepted criteria.
    recordDraft: {
      mode: 'specific',
      smoke_test: true,
      brief: {
        budget: null,
        deliverable: null,
        interests: needs,
        acceptance_criteria: [],
        desired_by: null,
        exclusions: [],
        licensing_needs: null,
      },
    },
    missingRequirements: ['concrete deliverable', 'independently agreed acceptance criteria',
      'budget or reviewed nonmonetary terms', 'timing', 'rights and licensing',
      'native identity and scoped authorization', 'exchange storage and consent mapping'],
    exchangeContext: {
      agent: reviewText(proposal.agent, 'agent', 100),
      currentTask: reviewText(proposal.currentTask, 'current task'),
      missingResources: reviewList(proposal.missingResources, 'missing resources'),
      offers: reviewList(proposal.offers, 'offers'),
      authority: 'proposal-only',
      proposedTerms: {
        kind: proposal.terms.kind,
        description: reviewText(proposal.terms.description, 'terms'),
      },
      evaluation: Object.fromEntries(['cost', 'usefulness', 'verifiability'].map(key =>
        [key, reviewText(proposal.evaluation?.[key], key)])),
      provenance: {
        source: reviewText(proposal.provenance.source, 'provenance'),
        rights: 'synthetic-demo-only',
      },
      consent: {
        granted: true,
        scope: 'local-storage-and-explicit-local-export-only',
        publication: false,
        resale: false,
        training: false,
        nativeSubmission: false,
      },
      retention: {
        sourceCreatedAt: createdAt,
        sourceExpiresAt: expiresAt,
        maximumSourceDays: POLICY.retentionDays,
        exportedCopyAutoDeletion: false,
        deletion: 'logical deletion only; exported copies require separate disposal',
      },
    },
    execution: {
      realEnabled: false,
      treasuryEnabled: false,
      automaticPayouts: false,
      privateAccess: false,
      serviceCreditEnabled: false,
      barterExecutionEnabled: false,
      humanTimeCommitted: false,
    },
  };
}
