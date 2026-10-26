"""Portable transaction/binding tests; native-host E2E is a separate opt-in run."""
from contextlib import contextmanager
from dataclasses import dataclass
import datetime as dt
import json
import sqlite3
import threading
import unittest

from .core import ControlError
from .repository import DDL
from .recovery_fence import initialize_lineage
from .service import ExchangeService, PREFIX

NOW = 1800000000000


@dataclass(frozen=True)
class FixturePrincipal:
    id: str
    role: str
    credential_id: str


class FixtureRepository:
    def __init__(self):
        self.connection = sqlite3.connect(':memory:', isolation_level=None, check_same_thread=False)
        self.connection.execute('PRAGMA foreign_keys=ON')
        self.connection.execute('CREATE TABLE principals(id TEXT PRIMARY KEY,role TEXT,active INTEGER)')
        self.connection.execute("INSERT INTO principals VALUES('owner','owner',1),('other-owner','owner',1)")
        for statement in DDL:
            self.connection.execute(statement)
        initialize_lineage(self.connection)
        self.connection.execute('PRAGMA user_version=12')
        self.lock = threading.RLock()
        self.depth = 0

    @contextmanager
    def transaction(self):
        with self.lock:
            outer = self.depth == 0
            self.depth += 1
            if outer:
                self.connection.execute('BEGIN IMMEDIATE')
            try:
                yield self.connection
                if outer:
                    self.connection.execute('COMMIT')
            except BaseException:
                if outer:
                    self.connection.execute('ROLLBACK')
                raise
            finally:
                self.depth -= 1


class FixtureIdentity:
    def __init__(self, repository):
        self.repository = repository
        self.now = NOW
        self.revoked = set()
        self.calls = 0

    def _current(self, principal):
        self.calls += 1
        if not self.repository.connection.in_transaction:
            raise AssertionError('Identity validation escaped the state transaction')
        if type(principal) is not FixturePrincipal or principal.credential_id != principal.id + '-fixture' or principal.id in self.revoked:
            raise ControlError(401, 'fixture_identity_invalid')
        return principal, dt.datetime.fromtimestamp(self.now / 1000, dt.timezone.utc)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.repo = FixtureRepository()
        self.addCleanup(self.repo.connection.close)
        self.identity = FixtureIdentity(self.repo)
        self.profile_denied = set()

        def scope(principal, method, path):
            if principal.id in self.profile_denied:
                raise ControlError(403, 'fixture_profile_denied')

        self.service = ExchangeService(self.repo, self.identity, enabled=True, authorize_scope=scope)
        self.owner = FixturePrincipal('owner', 'owner', 'owner-fixture')
        self.a = FixturePrincipal('agent-a', 'buyer', 'agent-a-fixture')
        self.b = FixturePrincipal('agent-b', 'provider', 'agent-b-fixture')
        with self.repo.transaction():
            self.repo.connection.execute("INSERT INTO x_exchange_tenants VALUES('tenant','owner')")
            for p, role in [(self.owner, 'owner'), (self.a, 'agent'), (self.b, 'agent')]:
                scopes = ['exchange:read', 'exchange:write'] + (['exchange:admin'] if role == 'owner' else [])
                self.repo.connection.execute('INSERT INTO x_exchange_memberships VALUES(?,?,?,?,?,1,NULL)',
                                             ('tenant', p.id, role, p.role, json.dumps(scopes)))

    def command(self, principal, action, payload=None, key=None, record=None):
        command = {'action': action, 'input': payload or {}}
        if record:
            command.update(id=record['id'], expectedVersion=record['version'])
        return self.service.dispatch('POST', PREFIX + '/tenant/commands', {}, principal, command, key or action)[1]

    def policy(self):
        return self.service.dispatch('GET', PREFIX + '/tenant', {}, self.owner)[1]

    def configure(self):
        result = self.command(self.owner, 'configure', {
            'limits': {'retentionMs': 86400000, 'reservationMs': 600000, 'maxRecords': 10},
            'resources': {'fixture-review': 2},
            'grants': [{'subjectId': p.id, 'resources': ['fixture-review'], 'maxUnits': 2} for p in (self.a, self.b)]})
        self.command(self.owner, 'open')
        return result

    def test_default_off_does_not_query_state_or_identity(self):
        disabled = ExchangeService(self.repo, self.identity, authorize_scope=lambda *a: None)
        with self.assertRaises(ControlError) as error:
            disabled.dispatch('GET', PREFIX + '/tenant', {}, self.owner)
        self.assertEqual(error.exception.status, 404)
        self.assertEqual(self.identity.calls, 0)
        self.assertEqual(self.repo.connection.execute('SELECT COUNT(*) FROM x_exchange_state').fetchone()[0], 0)

    def test_empty_policy_is_stopped_and_nothing_is_granted(self):
        policy = self.policy()
        self.assertTrue(policy['stopped'])
        self.assertEqual(policy['resources'], {})
        self.assertEqual(policy['grants'], {})

    def test_forged_principal_tenant_membership_and_native_role_are_denied(self):
        for principal in [dict(id='owner', role='owner'), FixturePrincipal('owner', 'owner', 'agent-a-fixture'),
                          FixturePrincipal('other-owner', 'owner', 'other-owner-fixture'),
                          FixturePrincipal('agent-a', 'owner', 'agent-a-fixture')]:
            with self.assertRaises(ControlError):
                self.service.dispatch('GET', PREFIX + '/tenant/records', {}, principal)
        with self.assertRaises(ControlError):
            self.service.dispatch('GET', PREFIX + '/other-tenant/records', {}, self.owner)
        self.assertEqual(self.repo.connection.execute('SELECT COUNT(*) FROM x_exchange_state').fetchone()[0], 0)

    def test_current_identity_scope_membership_revocation_precede_replay(self):
        self.configure()
        for change in ['credential', 'profile', 'membership']:
            if change == 'credential':
                self.identity.revoked.add('owner')
            elif change == 'profile':
                self.profile_denied.add('owner')
            else:
                self.repo.connection.execute("UPDATE x_exchange_memberships SET active=0 WHERE principal_id='owner'")
            with self.assertRaises(ControlError):
                self.command(self.owner, 'open')
            self.identity.revoked.clear()
            self.profile_denied.clear()
        self.assertTrue(self.repo.connection.execute('SELECT COUNT(*) FROM x_exchange_state').fetchone()[0], 1)

    def test_optional_membership_security_deadline_is_checked_but_null_has_no_cutoff(self):
        self.identity.now += 100 * 86400000
        self.assertTrue(self.policy()['stopped'])
        self.repo.connection.execute("UPDATE x_exchange_memberships SET expires_at=? WHERE principal_id='owner'", (self.identity.now,))
        with self.assertRaises(ControlError):
            self.policy()

    def test_http_body_actor_flags_and_query_injection_cannot_grant_authority(self):
        self.configure()
        for command in [{'action': 'open', 'actor': 'owner'}, {'action': 'open', 'input': {'ownerApproved': True}}]:
            with self.assertRaises(ControlError):
                self.service.dispatch('POST', PREFIX + '/tenant/commands', {}, self.a, command, 'forged')
        with self.assertRaises(ControlError):
            self.service.dispatch('GET', PREFIX + '/tenant/records', {'tenant': 'elsewhere'}, self.owner)


if __name__ == '__main__':
    unittest.main()
