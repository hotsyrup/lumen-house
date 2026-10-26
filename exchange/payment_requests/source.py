"""Read-only native paid-job projection. No financial workflow is invoked."""
from datetime import datetime, timezone
import json
import re

from .core import ControlError, digest


def _row(connection, sql, values):
    cursor = connection.execute(sql, values)
    row = cursor.fetchone()
    return None if row is None else dict(zip((field[0] for field in cursor.description), row))


def _current_until(value, now, code):
    try:
        validity = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if validity.utcoffset().total_seconds() != 0 or now >= int(validity.timestamp() * 1000):
            raise ValueError()
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise ControlError(409, code) from None


class NativePaidJobReader:
    def __init__(self, repository, *, allow_synthetic=False):
        if type(allow_synthetic) is not bool:
            raise ValueError('Explicit fixture provenance required')
        self.repository, self.allow_synthetic = repository, allow_synthetic

    def read(self, tenant_id, job_id, *, owner_id, now):
        db = self.repository.connection
        if not db.in_transaction:
            raise ControlError(503, 'native_transaction_required')
        if not isinstance(job_id, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,100}', job_id) is None:
            raise ControlError(400, 'native_job_id_required')
        tenant = _row(db, 'SELECT owner_id FROM x_exchange_tenants WHERE tenant_id=?', (tenant_id,))
        job = _row(db, 'SELECT * FROM wf_engagements WHERE id=?', (job_id,))
        if tenant is None or job is None or tenant['owner_id'] != owner_id or job['owner_id'] != owner_id:
            raise ControlError(404, 'native_paid_job_not_found')
        if job['campaign_state'] != 'active':
            raise ControlError(409, 'native_job_requires_owner_reconciliation')
        provenance = (job['source_kind'], job['evidence_class'])
        if provenance != ('real', 'live') and not (self.allow_synthetic and provenance == ('synthetic', 'synthetic')):
            raise ControlError(409, 'genuine_paid_job_required')
        terms = _row(db, 'SELECT * FROM wf_terms WHERE engagement_id=? AND version=?', (job_id, job['version']))
        if terms is None:
            raise ControlError(409, 'exact_paid_terms_required')
        binding = json.loads(terms['document_json'])
        if (digest(binding) != terms['binding_sha256'] or binding.get('engagement_id') != job_id
                or binding.get('owner_id') != owner_id or binding.get('provider_id') != job['provider_id']
                or binding.get('buyer_id') != job['buyer_id'] or binding.get('terms_version') != terms['version']
                or (binding.get('currency'), binding.get('network'), binding.get('asset')) != ('USDC', 'base', 'usdc')):
            raise ControlError(409, 'native_paid_terms_integrity_failed')
        _current_until(binding.get('valid_until'), now, 'exact_paid_terms_not_current')
        assents = [_row(db, 'SELECT * FROM wf_assents WHERE engagement_id=? AND version=? AND actor_role=?',
                        (job_id, job['version'], role)) for role in ('owner', 'buyer', 'provider')]
        for role, assent in zip(('owner', 'buyer', 'provider'), assents):
            if (assent is None or assent['actor_id'] != job[role + '_id']
                    or assent['binding_sha256'] != terms['binding_sha256']):
                raise ControlError(409, 'independent_paid_terms_assents_required')
        review = _row(db, "SELECT * FROM wf_reviews WHERE engagement_id=? AND decision='accept'", (job_id,))
        evidence = None if review is None else _row(db, 'SELECT * FROM wf_evidence WHERE id=? AND engagement_id=? AND version=?',
                                                 (review['evidence_id'], job_id, job['version']))
        if (review is None or evidence is None or review['owner_id'] != owner_id
                or evidence['provider_id'] != job['provider_id'] or evidence['completion'] != 'complete'
                or review['evidence_sha256'] != evidence['evidence_sha256']
                or evidence['binding_sha256'] != terms['binding_sha256']):
            raise ControlError(409, 'exact_owner_delivery_acceptance_required')
        # Any original authority or intent, including revoked/failed/unknown
        # history, blocks a fresh draft. This model cannot reconcile or replace it.
        # This lane cannot manufacture a replacement payment or reconcile one.
        for table in ('wf_authorities', 'wf_manifests', 'wf_execution_approvals', 'wf_attempts'):
            if db.execute('SELECT 1 FROM ' + table + ' WHERE engagement_id=? LIMIT 1', (job_id,)).fetchone():
                raise ControlError(409, 'original_payment_reconciliation_required')
        identity = _row(db, 'SELECT * FROM wf_identity WHERE id=?', (binding.get('provider_verification_id'),))
        active = _row(db, 'SELECT active FROM r_providers WHERE id=?', (job['provider_id'],))
        if (identity is None or active is None or active['active'] != 1 or identity['subject_id'] != job['provider_id']
                or identity['subject_role'] != 'provider' or identity['owner_id'] != owner_id
                or identity['recipient'] != binding['recipient'] or identity['recipient_sha256'] != digest(binding['recipient'])):
            raise ControlError(409, 'exact_worker_payee_binding_required')
        _current_until(identity['valid_until'], now, 'worker_payee_attestation_not_current')
        buyer_check = _row(db, 'SELECT * FROM wf_identity WHERE id=?', (binding.get('buyer_verification_id'),))
        buyer = _row(db, 'SELECT role,active FROM principals WHERE id=?', (job['buyer_id'],))
        if (buyer_check is None or buyer is None or buyer['role'] != 'buyer' or buyer['active'] != 1
                or buyer_check['subject_id'] != job['buyer_id'] or buyer_check['subject_role'] != 'buyer'
                or buyer_check['owner_id'] != owner_id):
            raise ControlError(409, 'exact_buyer_binding_required')
        _current_until(buyer_check['valid_until'], now, 'buyer_attestation_not_current')
        # Native attestation is not an independent wallet-ownership proof. Hash
        # and size are retained evidence metadata, not artifact-byte verification.
        return {'tenantId': tenant_id, 'jobId': job_id, 'ownerId': owner_id,
                'workerId': job['provider_id'], 'payee': binding['recipient'],
                'termsVersion': terms['version'], 'termsHash': terms['binding_sha256'],
                'termsDocumentHash': binding['terms_document_sha256'],
                'evidenceId': evidence['id'], 'evidenceHash': evidence['evidence_sha256'],
                'evidenceSize': evidence['evidence_size'], 'acceptanceId': review['id'], 'acceptanceHash': digest(review),
                'amountAtomic': binding['amount_atomic'], 'currency': 'USDC', 'network': 'base', 'asset': 'usdc', 'decimals': 6,
                'sourceKind': 'native-paid-fulfillment', 'status': 'accepted', 'recordEvidenceClass': job['evidence_class'],
                'artifactCheck': {'status': 'unavailable', 'reason': 'artifact_bytes_not_retained'},
                'payeeVerification': {'basis': 'owner_attestation', 'bindingHash': digest(identity), 'independentProof': False}}
