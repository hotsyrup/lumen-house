"""Native identity and exchange state share the host repository transaction.

No credentials, membership rows, resource offers, financial records or remote
requests are created by this adapter. The existing host authenticates bearers.
"""
import json
import re

from .core import ControlError, apply_operation, empty_state
from .repository import ExchangeTables

PREFIX = '/api/v1/exchange'
IDENTIFIER = r'[A-Za-z0-9_-]{1,100}'
ROUTE = re.compile(re.escape(PREFIX) + r'/(' + IDENTIFIER + r')(?:/(commands|records)(?:/(' + IDENTIFIER + r'))?)?')
SCOPES = {'exchange:read', 'exchange:write', 'exchange:admin'}


def exchange_path(path):
    return isinstance(path, str) and (path == PREFIX or path.startswith(PREFIX + '/'))


class ExchangeService:
    def __init__(self, repository, identity, *, enabled=False, authorize_scope=None):
        if type(enabled) is not bool:
            raise ValueError('Explicit boolean exchange gate required')
        if getattr(identity, 'repository', None) is not repository:
            raise ValueError('Identity must share the exact exchange repository')
        if authorize_scope is None:
            # Reuse the existing profile restrictions; never broaden them here.
            from pilot_cloud_scope import authorize_cloud_command
            authorize_scope = authorize_cloud_command
        self.repository = repository
        self.identity = identity
        self.enabled = enabled
        self.authorize_scope = authorize_scope
        self.tables = ExchangeTables(repository) if enabled else None

    def _binding(self, current, tenant_id, now):
        tenant = self.tables.tenant(tenant_id)
        member = self.tables.membership(tenant_id, current.id)
        if (tenant is None or member is None or member['active'] != 1
                or member['native_role'] != current.role
                or member['expires_at'] is not None and now >= member['expires_at']):
            raise ControlError(403, 'exchange_membership_required')
        scopes = member['scopes']
        if (not isinstance(scopes, list) or not scopes or len(scopes) != len(set(scopes))
                or any(scope not in SCOPES for scope in scopes)):
            raise ControlError(503, 'exchange_membership_invalid')
        if member['role'] == 'owner':
            if current.role != 'owner' or current.id != tenant['owner_id']:
                raise ControlError(403, 'exchange_owner_binding_required')
        elif member['role'] != 'agent' or current.role not in ('buyer', 'provider'):
            raise ControlError(403, 'exchange_role_binding_required')
        return {'tenantId': tenant_id, 'subjectId': current.id,
                'role': member['role'], 'scopes': list(scopes)}

    def _participant_available(self, tenant_id, subject_id, now):
        if not isinstance(subject_id, str) or re.fullmatch(IDENTIFIER, subject_id) is None:
            raise ControlError(400, 'exchange_participant_invalid')
        member = self.tables.membership(tenant_id, subject_id)
        if (member is None or member['role'] != 'agent' or member['active'] != 1
                or member['expires_at'] is not None and now >= member['expires_at']
                or 'exchange:write' not in member['scopes']):
            raise ControlError(403, 'exchange_participant_unavailable')
        # Assent belongs to the stable principal, not a bearer token. Rotation
        # does not undo an assent; deactivation/membership revocation does stop
        # a new agreement. No peer credential is read or manufactured here.
        if member['native_role'] == 'provider':
            row = self.repository.connection.execute('SELECT active FROM r_providers WHERE id=?', (subject_id,)).fetchone()
            available = row is not None and row[0] == 1
        else:
            row = self.repository.connection.execute('SELECT role,active FROM principals WHERE id=?', (subject_id,)).fetchone()
            available = row is not None and row[0] == member['native_role'] == 'buyer' and row[1] == 1
        if not available:
            raise ControlError(403, 'exchange_participant_unavailable')

    def dispatch(self, method, path, params, principal, document=None, key=None):
        if not self.enabled:
            raise ControlError(404, 'exchange_disabled')
        match = ROUTE.fullmatch(path) if isinstance(path, str) else None
        if match is None or type(params) is not dict or params:
            raise ControlError(404 if match is None else 400, 'exchange_route_invalid')
        tenant_id, action, record_id = match.groups()
        if method == 'POST' and action == 'commands' and record_id is None:
            if type(document) is not dict:
                raise ControlError(400, 'exchange_document_required')
            try:
                raw = json.dumps(document, allow_nan=False, ensure_ascii=False).encode('utf-8')
            except (ValueError, TypeError, UnicodeError, RecursionError):
                raise ControlError(400, 'exchange_document_invalid') from None
            if len(raw) > 16384:
                raise ControlError(413, 'exchange_document_too_large')
            operation = {'kind': 'command', 'command': document, 'key': key}
        elif method == 'GET' and action is None:
            operation = {'kind': 'policy'}
        elif method == 'GET' and action == 'records':
            operation = {'kind': 'get', 'id': record_id} if record_id else {'kind': 'list'}
        else:
            raise ControlError(405, 'exchange_method_invalid')

        pending = None
        with self.repository.lock, self.repository.transaction():
            current, instant = self.identity._current(principal)
            self.authorize_scope(current, method, path)
            now = int(instant.timestamp() * 1000)
            operation['principal'] = self._binding(current, tenant_id, now)
            before, version = self.tables.load_state(tenant_id)
            before = empty_state(tenant_id) if before is None else before
            # Persist retention cleanup even when the requested command fails.
            # Auth and profile checks precede cleanup, exports and retry lookup.
            purged = apply_operation(before, {'kind': 'purge'}, now=now)['state']
            if purged != before:
                version = self.tables.save_state(tenant_id, purged, version)
            try:
                changed = apply_operation(purged, operation, now=now)
                if operation['kind'] == 'command':
                    command = operation['command']
                    if command.get('action') == 'propose' and type(command.get('input')) is dict:
                        self._participant_available(tenant_id, command['input'].get('providerId'), now)
                    elif command.get('action') in ('assent', 'approve', 'deliver'):
                        record = purged['records'].get(command.get('id'))
                        if record is not None:
                            for subject_id in (record['requesterId'], record['providerId']):
                                self._participant_available(tenant_id, subject_id, now)
                if changed['state'] != purged:
                    self.tables.save_state(tenant_id, changed['state'], version)
                result = changed['result']
            except ControlError as error:
                pending = error
        if pending is not None:
            raise pending
        return 200, {'records': result} if operation['kind'] == 'list' else result, False
