"""SELECT-only paid-job projection against explicitly supplied private native code.

All stores, attestations, budgets and work are temporary synthetic fixtures.
Passing these tests is not a wallet handoff or evidence of live readiness.
"""
import datetime as dt
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest

from ..controls import repository as exchange
from .core import ControlError
from . import repository as payment
from .service import PaymentRequestService, PREFIX
from .source import NativePaidJobReader


@unittest.skipUnless(os.environ.get('LUMEN_HOUSE_NATIVE_SOURCE') and os.environ.get('LUMEN_HOUSE_NATIVE_FIXTURES'),
                     'explicit native source and fixture helper directories required')
class NativeSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(os.environ['LUMEN_HOUSE_NATIVE_SOURCE']).resolve()
        helpers = Path(os.environ['LUMEN_HOUSE_NATIVE_FIXTURES']).resolve()
        sys.path[:0] = [str(source), str(helpers)]
        cls.native = importlib.import_module('fulfillment_workflow')
        if Path(cls.native.__file__).resolve().parent != source:
            raise ValueError('native source precedence mismatch')
        cls.Fixture = importlib.import_module('test_fulfillment_workflow').WorkflowFixture
        cls.Identity = importlib.import_module('pilot_identity').PilotIdentityStore
        cls.DomainError = importlib.import_module('sourcing').DomainError

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='payment-source-synthetic-')
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'synthetic.sqlite3'
        self.f = self.Fixture(str(self.path), create=False)
        self.addCleanup(lambda: self.f.repo.close())
        # The fixture's normal1.25USDC agreement exceeds the pilot cap. This
        # explicit fictional paid contract is0.10, independently assented below.
        self.f.create_document['terms']['amount_atomic'] = 100000
        self.f.engagement = self.f.workflow.create(self.f.owner, self.f.create_document, self.f.key())
        self.f.id = self.f.engagement['engagement_id']

    def upgrade(self, *, earned=True):
        if earned:
            self.f.earn()
        self.f.repo.close()
        repository = exchange.native_repository_class()(self.path, migrate=True)
        with repository.transaction():
            repository.connection.execute('INSERT INTO x_exchange_tenants VALUES(?,?)', ('tenant-a', self.f.owner.id))
            repository.connection.execute('INSERT INTO x_exchange_memberships VALUES(?,?,?,?,?,1,NULL)',
                ('tenant-a', self.f.owner.id, 'owner', 'owner', '["exchange:admin"]'))
        repository.close()
        self.repo = payment.native_repository_class()(self.path, migrate=True)
        self.addCleanup(self.repo.close)
        self.reader = NativePaidJobReader(self.repo, allow_synthetic=True)
        self.identity = self.Identity(self.repo, clock=lambda: self.f.now)
        self.service = PaymentRequestService(self.repo, self.identity, enabled=True,
            authorized_owner_id=self.f.owner.id, authorized_tenant_id='tenant-a', source_reader=self.reader)
        self.now = int(self.f.now.timestamp() * 1000)

    def read(self, *, owner=None, now=None, tenant='tenant-a', reader=None):
        with self.repo.lock, self.repo.transaction():
            return (reader or self.reader).read(tenant, self.f.id, owner_id=owner or self.f.owner.id,
                                               now=self.now if now is None else now)

    def command(self, action, value=None, key=None):
        return self.service.dispatch('POST', PREFIX + '/tenant-a/commands', {}, self.f.owner,
                                     {'action': action, 'input': value or {}}, key or action)[1]

    def opened(self):
        self.command('configure', {'approvalReference': 'Synthetic exact ceiling approval'})
        self.command('open')

    def snapshot_native(self):
        db = self.repo.connection
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name!='p_payment_request_state'")]
        return {table: sorted([tuple(row) for row in db.execute('SELECT * FROM "' + table + '"')], key=repr) for table in tables}

    def test_projection_uses_exact_native_assents_acceptance_and_attested_payee(self):
        self.upgrade()
        before = self.snapshot_native()
        projected = self.read()
        self.assertEqual((projected['ownerId'], projected['workerId'], projected['payee']),
                         (self.f.owner.id, self.f.provider.id, self.f.recipient))
        self.assertEqual(projected['amountAtomic'], 100000)
        self.assertEqual(projected['evidenceHash'], self.f.proof['evidence_sha256'])
        self.assertEqual(projected['acceptanceId'], self.f.review['delivery_acceptance_id'])
        self.assertEqual(projected['artifactCheck']['status'], 'unavailable')
        self.assertFalse(projected['payeeVerification']['independentProof'])
        self.assertEqual(before, self.snapshot_native())

    def test_default_reader_rejects_fictional_provenance(self):
        self.upgrade()
        with self.assertRaises(ControlError) as raised:
            self.read(reader=NativePaidJobReader(self.repo))
        self.assertEqual(raised.exception.code, 'genuine_paid_job_required')

    def test_owner_tenant_and_nonexistent_job_mismatch(self):
        self.upgrade()
        for values in ({'owner': self.f.actors['other_owner'].id}, {'tenant': 'other-tenant'}):
            with self.assertRaises(ControlError) as raised:
                self.read(**values)
            self.assertEqual(raised.exception.status, 404)
        with self.repo.transaction(), self.assertRaises(ControlError):
            self.reader.read('tenant-a', 'missing', owner_id=self.f.owner.id, now=self.now)

    def test_missing_independent_assents_cannot_be_forged_by_owner_review(self):
        # Native review can preserve factual acceptance; the request lane still
        # requires all three separate exact assent records before a draft.
        self.f.workflow.assent(self.f.owner, self.f.id, self.f.binding(), self.f.key())
        self.upgrade(earned=False)
        with self.assertRaises(ControlError) as raised:
            self.read()
        self.assertEqual(raised.exception.code, 'independent_paid_terms_assents_required')

    def test_unaccepted_delivery_cannot_prepare(self):
        self.f.accept_task()
        self.f.proof = self.f.submit()
        self.upgrade(earned=False)
        self.opened()
        with self.assertRaises(ControlError) as raised:
            self.service.prepare_for_accepted_job(self.f.owner, self.f.id)
        self.assertEqual(raised.exception.code, 'exact_owner_delivery_acceptance_required')

    def test_expired_exact_terms_hold_without_releasing_obligation(self):
        self.upgrade()
        with self.assertRaises(ControlError) as raised:
            self.read(now=self.now + 3600000)
        self.assertEqual(raised.exception.code, 'exact_paid_terms_not_current')

    def test_original_native_authority_blocks_parallel_request_before_attempt(self):
        self.f.earn()
        self.f.authorize()
        self.upgrade(earned=False)
        self.assertEqual(self.repo.connection.execute('SELECT COUNT(*) FROM wf_attempts').fetchone()[0], 0)
        with self.assertRaises(ControlError) as raised:
            self.read()
        self.assertEqual(raised.exception.code, 'original_payment_reconciliation_required')

    def test_all_native_financial_security_and_intake_rows_unchanged_by_full_request_lifecycle(self):
        self.upgrade()
        before = self.snapshot_native()
        self.opened()
        first = self.service.prepare_for_accepted_job(self.f.owner, self.f.id)
        second = self.service.prepare_for_accepted_job(self.f.owner, self.f.id)
        self.assertEqual(first, second)
        self.service.dispatch('POST', PREFIX + '/tenant-a/commands', {}, self.f.owner,
            {'action': 'report_outcome', 'id': first['id'], 'input': {'outcome': 'unknown', 'reason': 'Synthetic outcome unavailable'}}, 'unknown')
        self.command('stop', {'reason': 'Synthetic stop'})
        self.assertEqual(before, self.snapshot_native())
        self.assertFalse(first['executionEnabled'])
        self.assertFalse(first['handoffAvailable'])

    def test_parallel_same_native_job_has_one_retained_request_and_one_hold(self):
        self.upgrade()
        self.opened()
        results, errors = [], []
        def prepare():
            try:
                results.append(self.service.prepare_for_accepted_job(self.f.owner, self.f.id))
            except Exception as error:
                errors.append(type(error).__name__)
        threads = [threading.Thread(target=prepare) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 12)
        self.assertEqual(len({row['id'] for row in results}), 1)
        with self.repo.transaction():
            state, version = payment.PaymentTables(self.repo).load_state('tenant-a')
        self.assertEqual(len(state['records']), 1)
        self.assertEqual(sum(row['principalHoldAtomic'] for row in state['records'].values()), 100000)

    def test_revoked_upload_owner_credential_does_not_create_request(self):
        self.upgrade()
        self.opened()
        with self.repo.transaction():
            self.repo.connection.execute('UPDATE credentials SET revoked_at=? WHERE id=?', (self.f.stamp(), self.f.owner.credential_id))
        before = self.repo.connection.execute('SELECT body_json,version FROM p_payment_request_state').fetchall()
        with self.assertRaises(self.DomainError):
            self.service.prepare_for_accepted_job(self.f.owner, self.f.id)
        self.assertEqual(before, self.repo.connection.execute('SELECT body_json,version FROM p_payment_request_state').fetchall())
