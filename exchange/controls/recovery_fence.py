"""Minimized erasure journal and current-source recovery fence.

Checksums establish consistency, not operator trust or freshness. Restore also
requires the separately pinned checkpoint AND the quiesced current source. A
lost current source is deliberately unsupported; an old backup cannot declare
itself the latest erasure authority. No data, runner or schedule opens on import.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

from .core import _validate_state, canonical_json, digest
from .errors import RepositoryError

FORMAT = 'lumen-house-exchange-recovery/v1'
HASH = re.compile(r'[0-9a-f]{64}')
MAX_CHECKPOINT_BYTES = 32 * 1024 * 1024


class FenceError(RepositoryError):
    pass


def tenant_hash(tenant_id):
    return digest({'domain': 'lumen-house-exchange/tenant/v1', 'tenantId': tenant_id})


def record_hash(tenant_id, record_id):
    return digest({'domain': 'lumen-house-exchange/record/v1', 'tenantId': tenant_id, 'recordId': record_id})


def initialize_lineage(connection):
    """Technical identity only; called by explicit schema migration, no grants."""
    lineage = uuid.uuid4().hex
    head = digest({'domain': 'lumen-house-exchange/erasure-genesis/v1', 'lineage': lineage})
    connection.execute('INSERT INTO x_exchange_recovery VALUES(1,?,0,?)', (lineage, head))


def journal(connection):
    row = connection.execute('SELECT lineage_id,erasure_sequence,erasure_head FROM x_exchange_recovery WHERE id=1').fetchone()
    if row is None or re.fullmatch(r'[0-9a-f]{32}', row[0]) is None or type(row[1]) is not int or row[1] < 0:
        raise FenceError('erasure_lineage_invalid')
    lineage, sequence, expected_head = row
    previous = digest({'domain': 'lumen-house-exchange/erasure-genesis/v1', 'lineage': lineage})
    events = []
    seen = set()
    for seq, tenant_id, selector, erased_at, prior, event_hash in connection.execute(
            'SELECT sequence,tenant_id,record_hash,erased_at,previous_hash,event_hash FROM x_exchange_erasures ORDER BY sequence'):
        event = {'sequence': seq, 'tenantHash': tenant_hash(tenant_id), 'recordHash': selector,
                 'erasedAt': erased_at, 'previousHash': prior}
        if (type(seq) is not int or seq != len(events) + 1 or prior != previous
                or not isinstance(selector, str) or HASH.fullmatch(selector) is None
                or type(erased_at) is not int or erased_at < 0
                or digest({'domain': 'lumen-house-exchange/erasure/v1', 'lineage': lineage, **event}) != event_hash
                or (tenant_id, selector) in seen):
            raise FenceError('erasure_journal_invalid')
        seen.add((tenant_id, selector))
        events.append(dict(event, hash=event_hash))
        previous = event_hash
    if sequence != len(events) or previous != expected_head:
        raise FenceError('erasure_journal_incomplete')
    return {'lineage': lineage, 'erasureSequence': sequence, 'erasureHead': expected_head, 'events': events}


def record_removals(connection, tenant_id, before, after):
    """Same native transaction as CAS: deletion never loses its erase receipt."""
    if not isinstance(after.get('records'), dict):
        return
    erased = {row[0] for row in connection.execute('SELECT record_hash FROM x_exchange_erasures WHERE tenant_id=?', (tenant_id,))}
    if any(record_hash(tenant_id, pid) in erased for pid in after['records']):
        raise FenceError('erased_record_identity_reused', 409)
    if before is None or not isinstance(before.get('records'), dict):
        return
    removed = sorted(set(before['records']) - set(after['records']))
    if not removed:
        return
    _validate_state(before)
    _validate_state(after)
    current = journal(connection)
    sequence, previous = current['erasureSequence'], current['erasureHead']
    for record_id in removed:
        if before['records'][record_id]['reservedUnits']:
            raise FenceError('erasure_cannot_drop_reservation', 409)
        selector = record_hash(tenant_id, record_id)
        sequence += 1
        event = {'sequence': sequence, 'tenantHash': tenant_hash(tenant_id), 'recordHash': selector,
                 'erasedAt': int(time.time() * 1000), 'previousHash': previous}
        head = digest({'domain': 'lumen-house-exchange/erasure/v1', 'lineage': current['lineage'], **event})
        connection.execute('INSERT INTO x_exchange_erasures VALUES(?,?,?,?,?,?)',
                           (sequence, tenant_id, selector, event['erasedAt'], previous, head))
        previous = head
    connection.execute('UPDATE x_exchange_recovery SET erasure_sequence=?,erasure_head=? WHERE id=1', (sequence, previous))


def state_fence(state):
    if state is None:
        return digest(None)
    _validate_state(state)
    # Retry payloads and rolling operational audit can change with reads/retries.
    # All business records, policy, capacity, consumed totals and holds must match.
    return digest({key: value for key, value in state.items() if key not in ('requests', 'administrativeAudit')})


def native_business_fence(connection):
    """Whole-native business state, without exporting rows or credential hashes.

    Credentials are always revoked and security events extended by the native
    hook. Every other native table is exact: newer obligations, control changes,
    identities, budgets or grants cannot disappear behind exchange-only parity.
    A separate intake database is neither read nor restored by this function.
    """
    ignored = {'credentials', 'r_credentials', 'security_events'}
    tables = []
    for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'x_exchange_%' ORDER BY name"):
        if name in ignored:
            continue
        if re.fullmatch(r'[A-Za-z0-9_]+', name) is None:
            raise FenceError('native_business_table_invalid')
        rows = []
        for row in connection.execute('SELECT * FROM "' + name + '"'):
            values = [{'type': 'blob', 'hex': value.hex()} if isinstance(value, bytes)
                      else {'type': type(value).__name__, 'value': value} for value in row]
            rows.append(digest(values))
        tables.append({'table': name, 'rows': sorted(rows)})
    return digest(tables)


def make_checkpoint(connection, schema_fingerprint, *, schema_version=12):
    if type(schema_version) is not int or schema_version not in (12, 13):
        raise FenceError('checkpoint_schema_unsupported')
    document = {'format': FORMAT, 'schemaVersion': schema_version, 'schemaFingerprint': schema_fingerprint,
                'nativeBusinessFence': native_business_fence(connection), **journal(connection)}
    fences = []
    for tenant_id, body in connection.execute('SELECT t.tenant_id,s.body_json FROM x_exchange_tenants t LEFT JOIN x_exchange_state s USING(tenant_id) ORDER BY t.tenant_id'):
        state = None if body is None else json.loads(body)
        if state is not None and state.get('tenantId') != tenant_id:
            raise FenceError('checkpoint_tenant_mismatch')
        fences.append({'tenantHash': tenant_hash(tenant_id), 'stateFence': state_fence(state)})
    document['tenantFences'] = sorted(fences, key=lambda row: row['tenantHash'])
    encoded = (canonical_json(document) + '\n').encode('utf-8')
    if len(encoded) > MAX_CHECKPOINT_BYTES:
        raise FenceError('checkpoint_capacity_requires_review')
    return document, encoded


def checkpoint_pin(document, encoded):
    return {'sha256': hashlib.sha256(encoded).hexdigest(), 'lineage': document['lineage'],
            'erasureSequence': document['erasureSequence'], 'erasureHead': document['erasureHead']}


def _private_checkpoint_bytes(path):
    candidate = Path(path)
    fd = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > MAX_CHECKPOINT_BYTES):
            raise FenceError('checkpoint_file_invalid')
        with os.fdopen(fd, 'rb') as file:
            fd = None
            raw = file.read(MAX_CHECKPOINT_BYTES + 1)
        if len(raw) > MAX_CHECKPOINT_BYTES:
            raise FenceError('checkpoint_file_invalid')
        return raw
    finally:
        if fd is not None:
            os.close(fd)


def publish_checkpoint(directory, document, encoded):
    """Durably write immutable owner-private output; no location is invented."""
    root = Path(directory)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or root.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise FenceError('checkpoint_directory_requires_owner_private_access')
    pin = checkpoint_pin(document, encoded)
    destination = root / (str(pin['erasureSequence']) + '-' + pin['sha256'] + '.json')
    try:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if _private_checkpoint_bytes(destination) != encoded:
            raise FenceError('checkpoint_output_conflict') from None
    else:
        try:
            with os.fdopen(fd, 'wb') as file:
                file.write(encoded)
                file.flush()
                os.fsync(file.fileno())
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
    folder = os.open(root, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(folder)
    finally:
        os.close(folder)
    return dict(pin, checkpointFile=destination.name)


def validate_checkpoint_file(path, pin, current_document, current_bytes):
    if (type(pin) is not dict or set(pin) != {'sha256', 'lineage', 'erasureSequence', 'erasureHead'}
            or type(pin.get('erasureSequence')) is not int or pin['erasureSequence'] < 0
            or any(type(pin.get(field)) is not str for field in ('sha256', 'lineage', 'erasureHead'))
            or HASH.fullmatch(pin['sha256']) is None or HASH.fullmatch(pin['erasureHead']) is None
            or re.fullmatch(r'[0-9a-f]{32}', pin['lineage']) is None
            or pin != checkpoint_pin(current_document, current_bytes)):
        raise FenceError('current_checkpoint_pin_required')
    raw = _private_checkpoint_bytes(path)
    if hashlib.sha256(raw).hexdigest() != pin['sha256'] or raw != current_bytes:
        raise FenceError('checkpoint_stale_or_changed')


def reconcile_erased_content(connection, checkpoint):
    """Reconcile before native holds/publication; reject non-erasure debt drift."""
    backup = journal(connection)
    if backup['lineage'] != checkpoint['lineage'] or backup['erasureSequence'] > checkpoint['erasureSequence']:
        raise FenceError('restore_lineage_or_cursor_mismatch')
    if backup['events'] != checkpoint['events'][:backup['erasureSequence']]:
        raise FenceError('restore_erasure_prefix_conflict')
    if native_business_fence(connection) != checkpoint['nativeBusinessFence']:
        raise FenceError('restore_requires_native_business_reconciliation')
    tenants = {tenant_hash(row[0]): row[0] for row in connection.execute('SELECT tenant_id FROM x_exchange_tenants')}
    expected = {row['tenantHash']: row['stateFence'] for row in checkpoint['tenantFences']}
    if set(tenants) != set(expected):
        raise FenceError('restore_tenant_scope_changed')
    selectors = {}
    for event in checkpoint['events']:
        if event['tenantHash'] not in tenants:
            raise FenceError('restore_erasure_scope_changed')
        selectors.setdefault(tenants[event['tenantHash']], set()).add(event['recordHash'])
    removed = 0
    for scope, tenant_id in tenants.items():
        row = connection.execute('SELECT body_json,version FROM x_exchange_state WHERE tenant_id=?', (tenant_id,)).fetchone()
        state = None if row is None else json.loads(row[0])
        if state is not None:
            _validate_state(state)
            original = state
            state = deepcopy(state)
            for record_id, record in list(state['records'].items()):
                if record_hash(tenant_id, record_id) in selectors.get(tenant_id, set()):
                    if record['reservedUnits']:
                        raise FenceError('restore_requires_obligation_reconciliation')
                    del state['records'][record_id]
                    for request in state['requests']:
                        if request['proposalId'] == record_id:
                            request['result'], request['resultHash'] = None, digest(None)
                    removed += 1
            if state_fence(state) != expected[scope]:
                raise FenceError('restore_requires_current_state_reconciliation')
            if state != original:
                connection.execute('UPDATE x_exchange_state SET body_json=?,version=version+1 WHERE tenant_id=?', (canonical_json(state), tenant_id))
        elif state_fence(None) != expected[scope]:
            raise FenceError('restore_requires_current_state_reconciliation')
    for event in checkpoint['events'][backup['erasureSequence']:]:
        connection.execute('INSERT INTO x_exchange_erasures VALUES(?,?,?,?,?,?)',
                           (event['sequence'], tenants[event['tenantHash']], event['recordHash'], event['erasedAt'], event['previousHash'], event['hash']))
    connection.execute('UPDATE x_exchange_recovery SET erasure_sequence=?,erasure_head=? WHERE id=1',
                       (checkpoint['erasureSequence'], checkpoint['erasureHead']))
    journal(connection)
    return removed
