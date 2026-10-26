"""Opt-in real native localhost transport; disposable synthetic paid work only.

The installed native source and its fixture helper must be explicitly supplied.
No deployment store, real account, provider, wallet or external socket is used.
These checks establish local implementation behavior, never live readiness.
"""
from copy import deepcopy
import hashlib
import http.client
import importlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from ..controls import repository as exchange
from . import repository as payment
from .service import PaymentRequestService, PREFIX
from .source import NativePaidJobReader


@unittest.skipUnless(os.environ.get('LUMEN_HOUSE_NATIVE_SOURCE') and os.environ.get('LUMEN_HOUSE_NATIVE_FIXTURES'),
                     'explicit installed native source and synthetic fixture helpers required')
class NativeHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(os.environ['LUMEN_HOUSE_NATIVE_SOURCE']).resolve()
        helpers = Path(os.environ['LUMEN_HOUSE_NATIVE_FIXTURES']).resolve()
        sys.path[:0] = [str(source), str(helpers)]
        cls.native = importlib.import_module('fulfillment_workflow')
        cls.host = importlib.import_module('pilot_server')
        cls.Identity = importlib.import_module('pilot_identity').PilotIdentityStore
        cls.Fixture = importlib.import_module('test_fulfillment_workflow').WorkflowFixture
        cls.DomainError = importlib.import_module('sourcing').DomainError
        cls.FulfillmentHttp = importlib.import_module('fulfillment_http').FulfillmentHttp
        if Path(cls.native.__file__).resolve().parent != source or Path(cls.host.__file__).resolve().parent != source:
            raise ValueError('Exact native source precedence required')

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='payment-native-http-synthetic-')
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'synthetic.sqlite3'
        self.responses, self.events, self.outbound = [], [], []
        self.counter = 0
        original_connect = socket.socket.connect

        def loopback_only(sock, address):
            if not isinstance(address, tuple) or address[0] != '127.0.0.1':
                self.outbound.append(str(address))
                raise AssertionError('External sockets are forbidden in native HTTP fixtures')
            return original_connect(sock, address)

        guard = patch.object(socket.socket, 'connect', loopback_only)
        guard.start()
        self.addCleanup(guard.stop)
        self.addCleanup(self.write_evidence)

    def write_evidence(self):
        self.assertEqual(self.outbound, [])
        destination = os.environ.get('LUMEN_HOUSE_NATIVE_HTTP_EVIDENCE')
        if destination:
            path = Path(destination)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('a', encoding='utf-8') as handle:
                handle.write(json.dumps({'test': self.id(), 'responses': self.responses,
                                         'events': self.events, 'external_requests': 0,
                                         'production_changes': 0}, sort_keys=True) + '\n')

    def create_host(self, *, earned=True):
        self.f = self.Fixture(str(self.path), create=False)
        self.f.create_document['terms']['amount_atomic'] = 37_500
        self.f.engagement = self.f.workflow.create(self.f.owner, self.f.create_document, self.f.key())
        self.f.id = self.f.engagement['engagement_id']
        if earned:
            self.f.earn()
        else:
            self.f.accept_task()
            self.f.proof = self.f.submit()
        self.f.repo.close()
        intermediate = exchange.native_repository_class()(self.path, migrate=True)
        with intermediate.transaction():
            db = intermediate.connection
            db.execute('INSERT INTO x_exchange_tenants VALUES(?,?)', ('synthetic-tenant', self.f.owner.id))
            db.execute('INSERT INTO x_exchange_memberships VALUES(?,?,?,?,?,1,NULL)',
                       ('synthetic-tenant', self.f.owner.id, 'owner', 'owner', '["exchange:admin"]'))
        intermediate.close()
        self.repo = payment.native_repository_class()(self.path, migrate=True)
        self.addCleanup(self.repo.close)
        self.identity = self.Identity(self.repo, clock=lambda: self.f.now)
        self.tokens, self.principals = {}, {}
        for role in ('owner', 'buyer', 'provider', 'other_owner'):
            issued = self.identity.issue(self.f.actors[role].id, 86400)
            self.tokens[role] = issued['token']
            self.principals[role] = self.identity.authenticate('Bearer ' + issued['token'])
        self.reader = NativePaidJobReader(self.repo, allow_synthetic=True)
        self.service = PaymentRequestService(self.repo, self.identity, enabled=True,
            authorized_owner_id=self.f.owner.id, authorized_tenant_id='synthetic-tenant', source_reader=self.reader)
        # This is the actual installed host factory and canonical handler, with
        # only its clock/source class restricted to fictional paid fixtures.
        self.server = self.host.make_pilot_server(0, self.repo, rate_limit=10000,
            anonymous_rate_limit=10000, timeout=3, header_timeout=3,
            payment_requests_enabled=True, payment_requests_owner_id=self.f.owner.id,
            payment_requests_authorized_tenant_id='synthetic-tenant')
        self.server.identity, self.server.payment_requests = self.identity, self.service
        self.workflow = self.native.FulfillmentWorkflow(self.repo, self.identity,
            clock=lambda: self.f.now, evidence_class='synthetic',
            mock_actor_kinds={self.f.owner.id: 'human', self.f.buyer.id: 'human', self.f.provider.id: 'service'})
        self.server.fulfillment = self.FulfillmentHttp(self.workflow)
        self.origin = 'http://127.0.0.1:' + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(4)

    def request(self, method, suffix='', *, subject='owner', document=None, key=None,
                headers=None, extra_headers=(), path=None, raw_body=None):
        path = PREFIX + '/synthetic-tenant' + suffix if path is None else path
        body = raw_body if raw_body is not None else (json.dumps(document).encode() if document is not None else b'')
        if isinstance(body, str):
            body = body.encode()
        actual = {'Host': '127.0.0.1:' + str(self.server.server_port), 'Origin': self.origin,
                  'Connection': 'close'}
        if subject is not None:
            actual['Authorization'] = 'Bearer ' + self.tokens[subject]
        if method in ('POST', 'PUT'):
            actual.update({'Content-Type': 'application/json', 'Content-Length': str(len(body))})
        if key:
            actual['Idempotency-Key'] = key
        actual.update(headers or {})
        actual = {name: value for name, value in actual.items() if value is not None}
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for name, value in list(actual.items()) + list(extra_headers):
                connection.putheader(name, value)
            connection.endheaders(body)
            response = connection.getresponse()
            raw = response.read()
            headers = response.getheaders()
            try:
                result = json.loads(raw) if raw else None
            except (ValueError, UnicodeError):
                result = raw.decode('utf-8', errors='replace')
            self.responses.append({'method': method, 'path': path, 'subject_fixture_role': subject,
                'request_header_names': list(actual) + [name for name, _ in extra_headers],
                'request_body_sha256': hashlib.sha256(body).hexdigest(), 'status': response.status,
                'headers': headers, 'body': result})
            return response.status, dict(headers), result
        finally:
            connection.close()

    def command(self, action, value=None, *, request_id=None, key=None, status=200, **kwargs):
        self.counter += 1
        command = {'action': action, 'input': value or {}}
        if request_id:
            command['id'] = request_id
        response = self.request('POST', '/commands', document=command,
                                key=key or 'synthetic-http-' + str(self.counter), **kwargs)
        self.assertEqual(response[0], status, response)
        return response[2]

    def opened(self):
        self.command('configure', {'approvalReference': 'SYNTHETIC localhost review; no production policy adoption'})
        self.command('open')

    def prepared(self, **kwargs):
        return self.command('prepare', {'jobId': self.f.id}, **kwargs)

    def state_rows(self):
        with self.repo.lock, self.repo.transaction():
            return [tuple(row) for row in self.repo.connection.execute('SELECT * FROM p_payment_request_state ORDER BY tenant_id')]

    def financial_rows(self):
        with self.repo.lock, self.repo.transaction():
            return {name: [tuple(row) for row in self.repo.connection.execute('SELECT * FROM ' + name)]
                    for name in ('wf_authorities', 'wf_manifests', 'wf_attempts', 'wf_outcomes')}

    def test_native_get_post_exact_source_retry_and_no_financial_writes(self):
        self.create_host()
        before = self.financial_rows()
        self.assertTrue(self.request('GET')[2]['stopped'])
        self.prepared(status=503)
        self.opened()
        row = self.prepared(key='synthetic-same-key')
        self.assertEqual(row['amountAtomic'], 37_500)
        self.assertTrue(row['sourceCurrent'])
        self.assertFalse(row['executionEnabled'])
        self.assertFalse(row['handoffAvailable'])
        self.assertEqual(row['source']['artifactCheck']['status'], 'unavailable')
        self.assertFalse(row['source']['payeeVerification']['independentProof'])
        for key in ('synthetic-same-key', 'synthetic-new-key'):
            self.assertEqual(self.prepared(key=key)['requestHash'], row['requestHash'])
        listing = self.request('GET', '/records')
        self.assertEqual(listing[1]['Cache-Control'], 'no-store')
        self.assertEqual(len(listing[2]['records']), 1)
        self.assertEqual(self.request('GET', '/records/' + row['id'])[2]['requestHash'], row['requestHash'])
        self.assertEqual(self.request('GET')[2]['exposure']['heldPrincipalAtomic'], 37_500)
        self.assertEqual(self.financial_rows(), before)

    def test_native_acceptance_derives_one_draft_without_authorizing_payment(self):
        self.create_host(earned=False)
        self.opened()
        before = self.financial_rows()
        document = {**self.f.proof_fields(), 'decision': 'accept'}
        route = '/api/owner/fulfillment/' + self.f.id + '/delivery-review'
        for _ in range(2):
            response = self.request('POST', path=route, document=document, key='synthetic-accept-once')
            self.assertEqual(response[0], 200, response)
            self.assertTrue(response[2]['earned_obligation'])
            self.assertFalse(response[2]['payment_authorized'])
            self.assertEqual(response[2]['paymentRequest']['amountAtomic'], 37_500)
            self.assertFalse(response[2]['paymentRequest']['executionEnabled'])
        self.assertEqual(len(self.request('GET', '/records')[2]['records']), 1)
        self.assertEqual(self.financial_rows(), before)

    def test_native_identity_tenant_origin_query_body_and_header_guards(self):
        self.create_host()
        self.opened()
        before = self.state_rows()
        cases = [
            ('GET', '/records', {'subject': None}, 401),
            ('GET', '/records', {'subject': 'buyer'}, 403),
            ('GET', '/records', {'subject': 'provider'}, 403),
            ('GET', '/records', {'subject': 'other_owner'}, 403),
            ('GET', '/records', {'headers': {'Origin': 'https://foreign.invalid'}}, 403),
            ('GET', '/records', {'headers': {'Host': 'foreign.invalid'}}, 403),
            ('GET', '/records', {'headers': {'Sec-Fetch-Site': 'cross-site'}}, 403),
            ('GET', '/records?tenant=other', {}, 422),
            ('GET', '/records?x=1&x=2', {}, 422),
            ('GET', '/records', {'headers': {'X-Owner': self.f.owner.id}}, 400),
            ('GET', '/records', {'extra_headers': [('Authorization', 'Bearer synthetic-duplicate')]}, 400),
            ('POST', '/commands', {'raw_body': '{'}, 400),
            ('POST', '/commands', {'raw_body': '{}', 'headers': {'Content-Type': 'text/plain'}}, 415),
            ('POST', '/commands', {'raw_body': '{"action":"open","action":"stop"}'}, 400),
            ('POST', '/commands', {'raw_body': '{"value":NaN}'}, 400),
            ('POST', '/commands', {'document': {'action': 'prepare', 'input': {'jobId': self.f.id, 'amountAtomic': 1}}}, 400),
            ('POST', '/commands', {'document': {'action': 'open', 'actor': 'owner', 'input': {}}}, 400),
            ('POST', '/commands', {'document': {'action': 'open', 'input': {}}, 'extra_headers': [('Idempotency-Key', 'one'), ('Idempotency-Key', 'two')]}, 400),
        ]
        for method, suffix, kwargs, expected in cases:
            with self.subTest(method=method, suffix=suffix, expected=expected):
                response = self.request(method, suffix, **kwargs)
                self.assertEqual(response[0], expected, response)
        response = self.request('GET', path=PREFIX + '/other-tenant/records')
        self.assertEqual(response[0], 403, response)
        self.assertEqual(before, self.state_rows())

    def test_source_pause_keeps_historical_request_held_and_hides_ready_claim(self):
        self.create_host()
        self.opened()
        row = self.prepared()
        before = self.state_rows()
        self.workflow.control(self.f.owner, self.f.id, {'action': 'pause'}, self.f.key())
        historical = self.request('GET', '/records/' + row['id'])[2]
        self.assertFalse(historical['sourceCurrent'])
        self.assertEqual(historical['sourceHold'], 'current_source_requires_owner_reconciliation')
        self.assertEqual(historical['principalHoldAtomic'], 37_500)
        self.assertEqual(historical['requestHash'], row['requestHash'])
        self.prepared(status=409)
        self.assertEqual(before, self.state_rows())

    def test_no_sign_send_confirm_or_settlement_route_or_command(self):
        self.create_host()
        self.opened()
        before, financial = self.state_rows(), self.financial_rows()
        for action in ('sign', 'send', 'broadcast', 'confirm', 'settle', 'release', 'fund', 'authorize'):
            for method in ('GET', 'POST'):
                response = self.request(method, '/' + action, document={} if method == 'POST' else None)
                self.assertEqual(response[0], 404, response)
            self.command(action, status=400)
        self.assertEqual(self.state_rows(), before)
        self.assertEqual(self.financial_rows(), financial)

    def paused_upload(self, revoke):
        self.create_host()
        self.opened()
        before, financial = self.state_rows(), self.financial_rows()
        entered = threading.Event()
        native_handler = self.server.RequestHandlerClass
        events = self.events
        DomainError = self.DomainError

        class ObservedNativeHandler(native_handler):
            # Observation only; real native authentication and parsing execute.
            def _authenticate(handler):
                try:
                    principal = super()._authenticate()
                except DomainError as error:
                    events.append({'event': 'authentication-rejected', 'code': error.code})
                    raise
                events.append({'event': 'authentication-succeeded', 'subject': principal.id})
                return principal

            def _body(handler):
                events.append({'event': 'native-body-read-started'})
                entered.set()
                document = super()._body()
                events.append({'event': 'native-body-read-completed'})
                return document

        self.server.RequestHandlerClass = ObservedNativeHandler
        body = json.dumps({'action': 'prepare', 'input': {'jobId': self.f.id}}).encode()
        head = ('POST ' + PREFIX + '/synthetic-tenant/commands HTTP/1.1\r\n'
                'Host: 127.0.0.1:' + str(self.server.server_port) + '\r\n'
                'Authorization: Bearer ' + self.tokens['owner'] + '\r\n'
                'Content-Type: application/json\r\nContent-Length: ' + str(len(body)) + '\r\n'
                'Idempotency-Key: synthetic-paused-body\r\nConnection: close\r\n\r\n').encode()
        sock = socket.create_connection(('127.0.0.1', self.server.server_port), timeout=5)
        try:
            sock.sendall(head + body[:1])
            self.assertTrue(entered.wait(2), 'Native body parser not reached')
            self.assertEqual(events[0]['event'], 'authentication-succeeded')
            self.assertNotIn('native-body-read-completed', [event['event'] for event in events])
            if revoke == 'credential':
                self.assertTrue(self.identity.revoke_id(self.principals['owner'].credential_id)['revoked'])
            else:
                with self.repo.lock, self.repo.transaction():
                    self.repo.connection.execute('UPDATE x_exchange_memberships SET active=0 WHERE tenant_id=? AND principal_id=?',
                                                ('synthetic-tenant', self.f.owner.id))
            events.append({'event': 'exact-fixture-' + revoke + '-revoked'})
            self.assertEqual(before, self.state_rows())
            sock.sendall(body[1:])
            sock.shutdown(socket.SHUT_WR)
            response = http.client.HTTPResponse(sock, method='POST')
            response.begin()
            raw = response.read()
            self.responses.append({'method': 'POST', 'path': PREFIX + '/synthetic-tenant/commands',
                'request_body_sha256': hashlib.sha256(body).hexdigest(), 'bytes_before_revocation': 1,
                'status': response.status, 'headers': response.getheaders(), 'body': json.loads(raw)})
        finally:
            sock.close()
        self.assertEqual(response.status, 401 if revoke == 'credential' else 403)
        self.assertEqual(events[:4], [{'event': 'authentication-succeeded', 'subject': self.f.owner.id},
            {'event': 'native-body-read-started'}, {'event': 'exact-fixture-' + revoke + '-revoked'},
            {'event': 'native-body-read-completed'}])
        self.assertEqual(events[-1]['event'], 'authentication-rejected' if revoke == 'credential' else 'authentication-succeeded')
        self.assertEqual(before, self.state_rows())
        self.assertEqual(financial, self.financial_rows())
        self.assertEqual(self.request('GET', '/records')[0], 401 if revoke == 'credential' else 403)

    def test_exact_owner_credential_revoked_during_native_body_upload(self):
        self.paused_upload('credential')

    def test_exact_tenant_membership_revoked_during_native_body_upload(self):
        self.paused_upload('membership')


if __name__ == '__main__':
    unittest.main()
