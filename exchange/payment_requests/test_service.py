"""Offline boundary tests; fixture identity is never a production identity source."""
from copy import deepcopy
import json
from types import SimpleNamespace
import threading
import unittest

from ..controls.test_service import FixtureRepository, FixtureIdentity, FixturePrincipal
from ..controls.core import ControlError as IdentityFixtureError
from .core import ControlError
from .http import with_payment_request_handler
from .repository import DDL
from .service import PaymentRequestService, PREFIX
from .test_core import source


class Reader:
    def __init__(self, repository):
        self.repository, self.changed = repository, False

    def read(self, tenant_id, job_id, *, owner_id, now):
        assert self.repository.connection.in_transaction
        return source(job_id, tenantId=tenant_id, ownerId=owner_id,
                      evidenceHash=('f' if self.changed else 'c') * 64)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.repo = FixtureRepository()
        self.addCleanup(self.repo.connection.close)
        for statement in DDL:
            self.repo.connection.execute(statement)
        self.repo.connection.execute('PRAGMA user_version=13')
        self.identity = FixtureIdentity(self.repo)
        self.owner = FixturePrincipal('owner', 'owner', 'owner-fixture')
        self.reader = Reader(self.repo)
        self.service = self.make_service('tenant-a')
        with self.repo.transaction():
            for tenant in ('tenant-a', 'tenant-b'):
                self.repo.connection.execute('INSERT INTO x_exchange_tenants VALUES(?,?)', (tenant, self.owner.id))
                self.repo.connection.execute('INSERT INTO x_exchange_memberships VALUES(?,?,?,?,?,1,NULL)',
                    (tenant, self.owner.id, 'owner', 'owner', '["exchange:admin"]'))

    def make_service(self, tenant):
        return PaymentRequestService(self.repo, self.identity, enabled=True,
            authorized_owner_id='owner', authorized_tenant_id=tenant,
            authorize_scope=lambda *args: None, source_reader=self.reader)

    def command(self, action, value=None, *, key=None, principal=None, service=None, tenant='tenant-a', **extra):
        return (service or self.service).dispatch('POST', PREFIX + '/' + tenant + '/commands', {}, principal or self.owner,
            {'action': action, 'input': value or {}, **extra}, key or action)[1]

    def opened(self):
        self.command('configure', {'approvalReference': 'Synthetic approval reference'})
        self.command('open')

    def assert_code(self, code, run):
        with self.assertRaises((ControlError, IdentityFixtureError)) as caught:
            run()
        self.assertEqual(caught.exception.code, code)

    def test_default_disabled_no_identity_state_or_schema_dependency(self):
        disabled = PaymentRequestService(self.repo, self.identity, authorize_scope=lambda *args: None)
        self.assert_code('payment_requests_disabled', lambda: disabled.dispatch('GET', PREFIX + '/tenant-a', {}, self.owner))
        self.assertEqual(self.identity.calls, 0)
        self.assertEqual(self.repo.connection.execute('SELECT COUNT(*) FROM p_payment_request_state').fetchone()[0], 0)

    def test_enabled_requires_explicit_owner_and_tenant(self):
        for values in ({}, {'authorized_owner_id': 'owner'}, {'authorized_tenant_id': 'tenant-a'}):
            with self.assertRaises(ValueError):
                PaymentRequestService(self.repo, self.identity, enabled=True, **values)

    def test_forged_or_wrong_native_owner_and_revoked_credentials(self):
        self.opened()
        for actor in ({'id': 'owner', 'role': 'owner'}, FixturePrincipal('other-owner', 'owner', 'other-owner-fixture'),
                      FixturePrincipal('owner', 'buyer', 'owner-fixture')):
            with self.assertRaises((ControlError, IdentityFixtureError)):
                self.command('prepare', {'jobId': 'job-a'}, principal=actor)
        self.identity.revoked.add('owner')
        self.assert_code('fixture_identity_invalid', lambda: self.command('prepare', {'jobId': 'job-a'}))

    def test_two_owner_tenants_cannot_duplicate_job_or_multiply_budget(self):
        self.opened()
        first = self.command('prepare', {'jobId': 'job-a'})
        self.assert_code('exact_payment_request_tenant_required',
            lambda: self.command('prepare', {'jobId': 'job-a'}, tenant='tenant-b'))
        other = self.make_service('tenant-b')
        self.assert_code('payment_request_payer_already_bound',
            lambda: self.command('configure', {'approvalReference': 'Second binding'}, service=other, tenant='tenant-b'))
        self.assertEqual(self.repo.connection.execute('SELECT COUNT(*) FROM p_payment_request_state').fetchone()[0], 1)
        self.assertEqual(first['principalHoldAtomic'], 100000)

    def test_revoked_membership_and_cloud_scope_remain_denied(self):
        self.opened()
        self.repo.connection.execute('UPDATE x_exchange_memberships SET active=0')
        self.assert_code('exact_tenant_owner_binding_required', lambda: self.command('prepare', {'jobId': 'job-a'}))
        self.repo.connection.execute('UPDATE x_exchange_memberships SET active=1')
        def denied(*args):
            raise ControlError(403, 'existing_profile_denied')
        self.service.authorize_scope = denied
        self.assert_code('existing_profile_denied', lambda: self.command('prepare', {'jobId': 'job-a'}))

    def test_body_cannot_supply_source_payee_amount_or_principal(self):
        self.opened()
        for key in ('source', 'payee', 'amountAtomic', 'principal'):
            self.assert_code('trusted_job_reference_only',
                lambda key=key: self.command('prepare', {'jobId': 'job-a', key: 'forged'}))
        with self.assertRaises(ControlError):
            self.command('prepare', {'jobId': 'job-a'}, principalId='owner')

    def test_parallel_preparation_reserves_at_most_daily_ceiling(self):
        self.opened()
        results, errors = [], []
        def prepare(index):
            try:
                results.append(self.command('prepare', {'jobId': 'job-' + str(index)}, key='parallel-' + str(index)))
            except ControlError as error:
                errors.append(error.code)
        threads = [threading.Thread(target=prepare, args=(i,)) for i in range(25)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 10)
        self.assertEqual(len(errors), 15)
        with self.repo.transaction():
            state, version = self.service.tables.load_state('tenant-a')
        self.assertEqual(sum(row['principalHoldAtomic'] for row in state['records'].values()), 1000000)

    def test_duplicate_and_changed_source_keep_original_hold(self):
        self.opened()
        first = self.service.prepare_for_accepted_job(self.owner, 'job-a')
        again = self.service.prepare_for_accepted_job(self.owner, 'job-a')
        self.assertEqual(first, again)
        self.reader.changed = True
        with self.assertRaises(ControlError):
            self.service.prepare_for_accepted_job(self.owner, 'job-a')
        view = self.service.dispatch('GET', PREFIX + '/tenant-a/records/' + first['id'], {}, self.owner)[1]
        self.assertFalse(view['sourceCurrent'])
        self.assertEqual(view['principalHoldAtomic'], 100000)

    def test_no_send_confirm_delete_or_settle_route(self):
        self.opened()
        for verb in ('confirm', 'send', 'settle', 'delete', 'release', 'approve'):
            with self.assertRaises(ControlError):
                self.command(verb)
            with self.assertRaises(ControlError):
                self.service.dispatch('POST', PREFIX + '/tenant-a/' + verb, {}, self.owner, {})

    def test_enabled_acceptance_hook_prepares_and_stopped_hook_preserves_acceptance(self):
        class Base:
            command, headers = 'POST', {}
            server = SimpleNamespace(payment_requests=self.service)
            def _api(self, *args):
                return 200, {'decision': 'accept', 'earned_obligation': True, 'payment_authorized': False}, False
        handler = with_payment_request_handler(Base)()
        path = '/api/owner/fulfillment/job-a/delivery-review'
        result = handler._api(path, {}, self.owner, {'decision': 'accept'})[1]
        self.assertTrue(result['earned_obligation'])
        self.assertEqual(result['paymentRequest']['status'], 'held')
        self.opened()
        result = handler._api(path, {}, self.owner, {'decision': 'accept'})[1]
        self.assertEqual(result['paymentRequest']['principalHoldAtomic'], 100000)
        self.assertFalse(result['payment_authorized'])

    def test_derivative_auth_or_store_failure_preserves_committed_native_acceptance(self):
        def unavailable(*args):
            raise RuntimeError('private error detail must not appear')
        self.service.prepare_for_accepted_job = unavailable
        class Base:
            command, headers = 'POST', {}
            server = SimpleNamespace(payment_requests=self.service)
            def _api(self, *args):
                return 200, {'decision': 'accept', 'earned_obligation': True, 'payment_authorized': False}, False
        handler = with_payment_request_handler(Base)()
        status, result, replay = handler._api('/api/owner/fulfillment/job-a/delivery-review', {}, self.owner, {})
        self.assertEqual(status, 200)
        self.assertTrue(result['earned_obligation'])
        self.assertEqual(result['paymentRequest']['reason'], 'request_preparation_unavailable')
        self.assertNotIn('private', json.dumps(result))

    def test_disabled_handler_delegates_native_response_exactly(self):
        expected = (404, {'exact': 'native fixture'}, False)
        class Base:
            command, headers = 'POST', {}
            server = SimpleNamespace(payment_requests=None)
            def _api(self, *args):
                return expected
        handler = with_payment_request_handler(Base)()
        for path in (PREFIX + '/tenant-a/commands', '/api/owner/fulfillment/job-a/delivery-review'):
            self.assertIs(handler._api(path, {}, self.owner, {}), expected)
