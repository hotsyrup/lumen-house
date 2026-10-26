"""Owner-scoped request preparation; no execution or financial authority imports."""
from copy import deepcopy
import json
import re

from .core import ControlError, apply_operation, empty_state, digest
from .repository import PaymentTables
from .source import NativePaidJobReader

PREFIX = '/api/owner/payment-requests'
ID = r'[A-Za-z0-9_-]{1,100}'
ROUTE = re.compile(re.escape(PREFIX) + '/(' + ID + r')(?:/(commands|records)(?:/(' + ID + r'))?)?')


class PaymentRequestService:
    def __init__(self, repository, identity, *, enabled=False, authorized_owner_id=None, authorized_tenant_id=None,
                 authorize_scope=None, source_reader=None):
        if type(enabled) is not bool or getattr(identity, 'repository', None) is not repository:
            raise ValueError('Explicit gate and exact native identity repository required')
        if enabled and any(not isinstance(value, str) or re.fullmatch(ID, value) is None
                           for value in (authorized_owner_id, authorized_tenant_id)):
            raise ValueError('Explicit single owner and tenant installation binding required')
        if authorize_scope is None:
            from pilot_cloud_scope import authorize_cloud_command
            authorize_scope = authorize_cloud_command
        self.repository, self.identity, self.enabled = repository, identity, enabled
        self.owner_id, self.tenant_id, self.authorize_scope = authorized_owner_id, authorized_tenant_id, authorize_scope
        self.tables = PaymentTables(repository) if enabled else None
        self.reader = source_reader or NativePaidJobReader(repository)
        if getattr(self.reader, 'repository', None) is not repository:
            raise ValueError('Source reader must share the exact native transaction')

    def _actor(self, principal, tenant_id, now):
        current, instant = self.identity._current(principal)
        if current.role != 'owner' or current.id != self.owner_id:
            raise ControlError(403, 'exact_payment_request_owner_required')
        # Native paid jobs belong to an owner, not an exchange tenant. One
        # installation is pinned to one tenant so neither job deduplication nor
        # the approved payer budget can be multiplied across that owner's tenants.
        if tenant_id != self.tenant_id:
            raise ControlError(403, 'exact_payment_request_tenant_required')
        # A later installation change must not reset the payer budget by moving
        # to another tenant. Retained state is never deleted; this check is under
        # the same writer transaction as the first configuration/state insert.
        if self.repository.connection.execute(
                'SELECT 1 FROM p_payment_request_state p JOIN x_exchange_tenants t USING(tenant_id) '
                'WHERE t.owner_id=? AND p.tenant_id!=? LIMIT 1', (current.id, tenant_id)).fetchone():
            raise ControlError(409, 'payment_request_payer_already_bound')
        owner = self.tables.tenant_owner(tenant_id)
        row = self.repository.connection.execute(
            'SELECT native_role,role,scopes_json,active,expires_at FROM x_exchange_memberships WHERE tenant_id=? AND principal_id=?',
            (tenant_id, current.id)).fetchone()
        if (owner != current.id or row is None or row[0] != 'owner' or row[1] != 'owner'
                or row[3] != 1 or row[4] is not None and now >= row[4]
                or 'exchange:admin' not in json.loads(row[2])):
            raise ControlError(403, 'exact_tenant_owner_binding_required')
        return {'tenantId': tenant_id, 'subjectId': current.id, 'role': 'owner',
                'scopes': ['payments:read', 'payments:prepare', 'payments:control']}

    def _fresh_view(self, result, state, owner_id, now):
        if isinstance(result, list):
            return [self._fresh_view(item, state, owner_id, now) for item in result]
        if type(result) is not dict or 'source' not in result:
            return result
        view = deepcopy(result)
        try:
            source = self.reader.read(state['tenantId'], result['source']['jobId'], owner_id=owner_id, now=now)
            current = digest(source) == result['sourceHash']
        except ControlError:
            current = False
        view['sourceCurrent'] = current
        if not current:
            view['sourceHold'] = 'current_source_requires_owner_reconciliation'
        return view

    def prepare_for_accepted_job(self, principal, job_id):
        """Derived draft after native owner acceptance; no approval is invented.

        The HTTP adapter invokes this only after successful native acceptance.
        Replaying that acceptance safely retries preparation after a crash. This
        also rechecks all current native records and the caller's credential.
        """
        return self.dispatch('POST', PREFIX + '/' + str(self.tenant_id) + '/commands', {}, principal,
                             {'action': 'prepare', 'input': {'jobId': job_id}},
                             'accepted-job-' + digest({'jobId': job_id}))[1]

    def dispatch(self, method, path, params, principal, body=None, key=None):
        if not self.enabled:
            raise ControlError(404, 'payment_requests_disabled')
        match = ROUTE.fullmatch(path) if isinstance(path, str) else None
        if match is None or type(params) is not dict or params:
            raise ControlError(404 if match is None else 400, 'payment_request_route_invalid')
        tenant_id, action, request_id = match.groups()
        if method == 'POST' and action == 'commands' and request_id is None:
            if type(body) is not dict:
                raise ControlError(400, 'request_document_required')
            try:
                encoded = json.dumps(body, allow_nan=False, ensure_ascii=False).encode()
            except (ValueError, TypeError, UnicodeError, RecursionError):
                raise ControlError(400, 'invalid_request_document') from None
            if len(encoded) > 16384:
                raise ControlError(413, 'request_document_too_large')
            operation = {'kind': 'command', 'command': body, 'key': key}
        elif method == 'GET' and action == 'records':
            operation = {'kind': 'get', 'id': request_id} if request_id else {'kind': 'list'}
        elif method == 'GET' and action is None:
            operation = {'kind': 'policy'}
        else:
            raise ControlError(405, 'payment_request_method_invalid')
        with self.repository.lock, self.repository.transaction():
            current, instant = self.identity._current(principal)
            self.authorize_scope(current, method, path)
            now = int(instant.timestamp() * 1000)
            actor = self._actor(current, tenant_id, now)
            operation['principal'] = actor
            before, version = self.tables.load_state(tenant_id)
            before = empty_state(tenant_id) if before is None else before
            if operation['kind'] == 'command' and body.get('action') == 'prepare':
                value = body.get('input')
                if type(value) is not dict or set(value) != {'jobId'}:
                    raise ControlError(400, 'trusted_job_reference_only')
                operation['source'] = self.reader.read(tenant_id, value['jobId'], owner_id=actor['subjectId'], now=now)
            changed = apply_operation(before, operation, now=now)
            if changed['state'] != before:
                self.tables.save_state(tenant_id, changed['state'], version)
            result = self._fresh_view(changed['result'], changed['state'], actor['subjectId'], now)
            return 200, {'records': result} if operation['kind'] == 'list' else result, False
