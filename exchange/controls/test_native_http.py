"""Opt-in E2E against installed native identity, handler and durable repository.

All records and deterministic bearer fixtures live in TemporaryDirectory. This
does not open a deployment store, contact a provider, or test live readiness.
"""
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import hashlib
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest

from .core import ControlError
from .http import with_exchange_handler
from .repository import native_repository_class
from .service import ExchangeService, PREFIX


@unittest.skipUnless(os.environ.get('LUMEN_HOUSE_NATIVE_SOURCE'), 'installed native source required; live readiness remains unverified')
class NativeHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(Path(os.environ['LUMEN_HOUSE_NATIVE_SOURCE']).resolve()))
        import fulfillment_workflow
        import pilot_server
        import pilot_identity
        import server
        cls.native, cls.host, cls.identity_module, cls.server_module = fulfillment_workflow, pilot_server, pilot_identity, server
        cls.Repository = native_repository_class()

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='exchange-http-fixture-')
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'synthetic.sqlite3'
        base = self.native.FulfillmentRepository(self.path)
        base.close()
        self.repo = self.Repository(self.path, migrate=True)
        self.addCleanup(self.repo.close)
        self.identity = self.identity_module.PilotIdentityStore(self.repo)
        self.tokens = {'fixture-owner': 'A' * 43, 'fixture-requester': 'B' * 43,
                       'fixture-provider': 'C' * 43, 'fixture-outsider': 'D' * 43}
        expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat().replace('+00:00', 'Z')
        with self.repo.transaction():
            db = self.repo.connection
            for subject, token in self.tokens.items():
                role = 'owner' if subject == 'fixture-owner' else 'provider' if subject == 'fixture-provider' else 'buyer'
                if role == 'provider':
                    db.execute('INSERT INTO r_providers VALUES(?,1,?)', (subject, '2026-01-01T00:00:00Z'))
                else:
                    db.execute('INSERT INTO principals VALUES(?,?,1,?)', (subject, role, '2026-01-01T00:00:00Z'))
                table = 'r_credentials' if role == 'provider' else 'credentials'
                db.execute('INSERT INTO ' + table + '(id,principal_id,token_hash,created_at,expires_at) VALUES(?,?,?,?,?)',
                           (subject + '-credential', subject, hashlib.sha256(token.encode()).hexdigest(), '2026-01-01T00:00:00Z', expiry))
            db.execute("INSERT INTO x_exchange_tenants VALUES('fixture-tenant','fixture-owner')")
            for subject in self.tokens:
                native_role = 'owner' if subject == 'fixture-owner' else 'provider' if subject == 'fixture-provider' else 'buyer'
                role = 'owner' if native_role == 'owner' else 'agent'
                scopes = ['exchange:read', 'exchange:write'] + (['exchange:admin'] if role == 'owner' else [])
                db.execute('INSERT INTO x_exchange_memberships VALUES(?,?,?,?,?,1,NULL)',
                           ('fixture-tenant', subject, role, native_role, json.dumps(scopes)))
        self.service = ExchangeService(self.repo, self.identity, enabled=True)
        # Real existing handler and transport guards, synthetic host composition.
        # The production host remains unmodified and unmounted.
        self.server = self.server_module.LocalServer(('127.0.0.1', 0), with_exchange_handler(self.host.PilotHandler))
        self.server.repository, self.server.identity, self.server.exchange = self.repo, self.identity, self.service
        self.server.public_origin = None
        self.server.limits = self.server_module.RateLimits(10000, 10000)
        self.server.request_timeout = self.server.header_timeout = 2
        self.origin = 'http://127.0.0.1:' + str(self.server.server_port)
        self.server.allowed_hosts = {'127.0.0.1:' + str(self.server.server_port)}
        self.server.allowed_origins = {self.origin}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.counter = 0

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, method, suffix, *, subject='fixture-owner', document=None, key=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=4)
        actual = {'Authorization': 'Bearer ' + self.tokens[subject], 'Origin': self.origin}
        if document is not None:
            actual['Content-Type'] = 'application/json'
        if key:
            actual['Idempotency-Key'] = key
        actual.update(headers or {})
        connection.request(method, PREFIX + '/fixture-tenant' + suffix,
                           None if document is None else json.dumps(document), actual)
        response = connection.getresponse()
        raw = response.read()
        result = response.status, dict(response.getheaders()), json.loads(raw) if raw else None
        connection.close()
        return result

    def command(self, action, *, subject='fixture-owner', value=None, record=None, key=None, status=200):
        self.counter += 1
        command = {'action': action, 'input': value or {}}
        if record:
            command.update(id=record['id'], expectedVersion=record['version'])
        response = self.request('POST', '/commands', subject=subject, document=command, key=key or 'fixture-' + str(self.counter))
        self.assertEqual(response[0], status, response)
        return response[2]

    def configure(self, capacity=2):
        self.command('configure', value={
            'limits': {'retentionMs': 86400000, 'reservationMs': 600000, 'maxRecords': 20},
            'resources': {'fixture-review': capacity},
            'grants': [{'subjectId': subject, 'resources': ['fixture-review'], 'maxUnits': 10}
                       for subject in self.tokens if subject != 'fixture-owner']})
        self.command('open')

    def proposed(self, key=None):
        return self.command('propose', subject='fixture-requester', key=key, value={
            'providerId': 'fixture-provider', 'sharingConsent': True,
            'terms': {'description': 'Synthetic non-cash review', 'deliverable': 'Fictional report',
                      'acceptance': 'Owner independently verifies fixture', 'cancellation': 'Review disputed delivery',
                      'resourceId': 'fixture-review', 'units': 1}})

    def assented(self):
        record = self.proposed()
        for subject in ('fixture-requester', 'fixture-provider'):
            record = self.command('assent', subject=subject, record=record,
                                  value={'termsRevision': record['termsRevision'], 'termsHash': record['termsHash'], 'sharingConsent': True})
        return record

    def approve(self, record, **kwargs):
        return self.command('approve', record=record, value={'termsRevision': record['termsRevision'], 'termsHash': record['termsHash']}, **kwargs)

    def test_real_native_http_lifecycle_and_evidence_object_access(self):
        self.configure()
        record = self.approve(self.assented())
        record = self.command('deliver', subject='fixture-provider', record=record,
                              value={'evidence': {'sha256': 'a' * 64, 'description': 'Synthetic manifest claim'}})
        self.assertEqual(self.request('GET', '/records/' + record['id'], subject='fixture-outsider')[0], 404)
        self.command('accept', subject='fixture-provider', record=record, value={
            'deliveryRevision': 1, 'deliveryHash': record['evidence']['deliveryHash'], 'assessment': 'Self acceptance'}, status=403)
        accepted = self.command('accept', record=record, key='accept-once', value={
            'deliveryRevision': 1, 'deliveryHash': record['evidence']['deliveryHash'], 'assessment': 'Owner checked synthetic fixture'})
        self.assertEqual(accepted['status'], 'completed')
        self.assertEqual(self.request('GET', '/records')[2]['records'][0]['status'], 'completed')
        self.assertEqual(self.request('GET', '')[2]['resources']['fixture-review']['used'], 1)
        # Fresh native revocation also blocks a previously authorized get.
        with self.repo.transaction():
            self.repo.connection.execute("UPDATE r_credentials SET revoked_at='2026-01-01T00:00:00Z'")
        response = self.request('GET', '/records/' + record['id'], subject='fixture-provider')
        self.assertEqual(response[0], 401)
        self.assertNotIn('Synthetic manifest', json.dumps(response[2]))

    def test_parallel_approval_cannot_overspend_native_shared_capacity(self):
        self.configure(capacity=1)
        left, right = self.assented(), self.assented()
        principal = self.identity.authenticate('Bearer ' + self.tokens['fixture-owner'])

        def approve(record):
            command = {'action': 'approve', 'id': record['id'], 'expectedVersion': record['version'],
                       'input': {'termsRevision': record['termsRevision'], 'termsHash': record['termsHash']}}
            try:
                self.service.dispatch('POST', PREFIX + '/fixture-tenant/commands', {}, principal, command, record['id'])
                return 200
            except ControlError as error:
                return error.status

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = sorted(pool.map(approve, [left, right]))
        self.assertEqual(outcomes, [200, 409])
        self.assertEqual(self.request('GET', '')[2]['resources']['fixture-review']['reserved'], 1)

    def test_transport_forgery_cross_origin_duplicates_and_disabled_mount(self):
        self.configure()
        response = self.request('POST', '/commands', document={'action': 'open', 'actor': 'owner'}, key='forged')
        self.assertEqual(response[0], 400)
        self.assertEqual(self.request('GET', '/records', headers={'Origin': 'https://foreign.example'})[0], 403)
        self.assertEqual(self.request('GET', '/records?tenant=other')[0], 422)
        self.assertEqual(self.request('GET', '/records', headers={'X-Demo-Buyer': 'fixture-owner'})[0], 400)
        self.assertEqual(self.request('GET', '/records')[1]['Cache-Control'], 'no-store')
        self.service.enabled = False
        # Exact previous role policy is preserved: /api/v1 requires buyer.
        self.assertEqual(self.request('GET', '/records')[0], 403)
        self.assertEqual(self.request('GET', '/records', subject='fixture-requester')[0], 404)
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=4)
        connection.request('GET', '/health')
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        response.read(); connection.close()

    def test_parallel_acceptance_consumes_once_and_cancel_race_is_serialized(self):
        self.configure(capacity=2)

        def delivered():
            record = self.approve(self.assented())
            return self.command('deliver', subject='fixture-provider', record=record,
                                value={'evidence': {'sha256': 'b' * 64, 'description': 'Fictional race fixture'}})

        def dispatch(subject, command, key):
            principal = self.identity.authenticate('Bearer ' + self.tokens[subject])
            try:
                self.service.dispatch('POST', PREFIX + '/fixture-tenant/commands', {}, principal, command, key)
                return 200
            except ControlError as error:
                return error.status

        def acceptance(record):
            return {'action': 'accept', 'id': record['id'], 'expectedVersion': record['version'], 'input': {
                'deliveryRevision': 1, 'deliveryHash': record['evidence']['deliveryHash'], 'assessment': 'Synthetic owner verification'}}

        record = delivered()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(dispatch, 'fixture-owner', acceptance(record), 'accept-race-' + str(i)) for i in range(2)]
            self.assertEqual(sorted(f.result() for f in futures), [200, 409])
        self.assertEqual(self.request('GET', '')[2]['resources']['fixture-review']['used'], 1)

        record = delivered()
        cancellation = {'action': 'cancel', 'id': record['id'], 'expectedVersion': record['version'], 'input': {'reason': 'Synthetic concurrent cancellation'}}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(dispatch, 'fixture-owner', acceptance(record), 'accept-cancel-race'),
                       pool.submit(dispatch, 'fixture-requester', cancellation, 'cancel-accept-race')]
            self.assertEqual(sorted(f.result() for f in futures), [200, 409])
        final = self.request('GET', '/records/' + record['id'])[2]
        resource = self.request('GET', '')[2]['resources']['fixture-review']
        self.assertIn(final['status'], ('completed', 'disputed'))
        self.assertEqual(resource['used'] + resource['reserved'], 2)
        self.assertEqual(final['reservedUnits'], 1 if final['status'] == 'disputed' else 0)

    def test_failed_commit_rolls_back_proposal_audit_retry_and_reservation(self):
        self.configure(capacity=1)
        record = self.assented()
        with self.repo.transaction():
            before = self.repo.connection.execute('SELECT body_json,version FROM x_exchange_state').fetchone()
        original = self.service.tables.save_state

        def interrupted(*args):
            original(*args)
            raise RuntimeError('Synthetic interruption before commit')

        self.service.tables.save_state = interrupted
        principal = self.identity.authenticate('Bearer ' + self.tokens['fixture-owner'])
        command = {'action': 'approve', 'id': record['id'], 'expectedVersion': record['version'],
                   'input': {'termsRevision': record['termsRevision'], 'termsHash': record['termsHash']}}
        try:
            with self.assertRaises(RuntimeError):
                self.service.dispatch('POST', PREFIX + '/fixture-tenant/commands', {}, principal, command, 'interrupted-approval')
        finally:
            self.service.tables.save_state = original
        with self.repo.transaction():
            after = self.repo.connection.execute('SELECT body_json,version FROM x_exchange_state').fetchone()
        self.assertEqual(tuple(after), tuple(before))
        result = self.service.dispatch('POST', PREFIX + '/fixture-tenant/commands', {}, principal, command, 'interrupted-approval')[1]
        self.assertEqual(result['status'], 'agreed')
        self.assertEqual(self.request('GET', '')[2]['resources']['fixture-review']['reserved'], 1)

    def test_participant_revocation_invalidates_new_owner_approval(self):
        self.configure(capacity=1)
        record = self.assented()
        with self.repo.transaction():
            self.repo.connection.execute("UPDATE x_exchange_memberships SET active=0 WHERE principal_id='fixture-provider'")
        self.approve(record, status=403)
        self.assertEqual(self.request('GET', '')[2]['resources']['fixture-review']['reserved'], 0)
        with self.repo.transaction():
            self.repo.connection.execute("UPDATE x_exchange_memberships SET active=1 WHERE principal_id='fixture-provider'")
            self.repo.connection.execute("UPDATE r_providers SET active=0 WHERE id='fixture-provider'")
        self.approve(record, status=403)
        self.assertEqual(self.request('GET', '')[2]['resources']['fixture-review']['reserved'], 0)


if __name__ == '__main__':
    unittest.main()
