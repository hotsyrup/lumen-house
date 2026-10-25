"""Explicit schema13 request storage on the existing native House database.

Importing this module opens nothing. Migration accepts only a reviewed existing
schema12 under an explicit offline decision. It creates no request, budget,
membership, credential, resource grant, execution approval or wallet capability.
"""
import datetime as dt
import fcntl
import json
import os
import stat
import uuid

from ..controls import repository as exchange
from ..controls.errors import RepositoryError
from ..controls.recovery_fence import make_checkpoint, publish_checkpoint, reconcile_erased_content, validate_checkpoint_file

SCHEMA_VERSION = 13
MAX_STATE_BYTES = 1048576
TABLES = ("p_payment_request_state",)
DDL = (
    "CREATE TABLE p_payment_request_state(tenant_id TEXT PRIMARY KEY NOT NULL REFERENCES x_exchange_tenants(tenant_id),body_json TEXT NOT NULL CHECK(json_valid(body_json) AND json_type(body_json)='object' AND length(CAST(body_json AS BLOB))<=1048576),version INTEGER NOT NULL CHECK(typeof(version)='integer' AND version BETWEEN 1 AND 2147483647))",
    "CREATE TRIGGER p_payment_request_state_version BEFORE UPDATE ON p_payment_request_state WHEN NEW.tenant_id!=OLD.tenant_id OR NEW.version!=OLD.version+1 BEGIN SELECT RAISE(ABORT,'payment request state requires next exact version'); END",
    "CREATE TRIGGER p_payment_request_state_retained BEFORE DELETE ON p_payment_request_state BEGIN SELECT RAISE(ABORT,'payment request exposure is retained'); END",
)
# Independently pinned from a synthetic reviewed schema12 plus the DDL above.
# No expected schema digest is learned from a database being opened.
SCHEMA13_SHA256 = "PENDING"


def _core():
    from . import core
    if not callable(getattr(core, "validate_state", None)):
        raise RepositoryError("payment_state_validator_required")
    return core


def _serialized(state):
    if not isinstance(state, dict):
        raise RepositoryError("invalid_payment_request_state", 400)
    try:
        body = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        size = len(body.encode("utf-8"))
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise RepositoryError("invalid_payment_request_state", 400) from None
    if size > MAX_STATE_BYTES:
        raise RepositoryError("payment_request_state_capacity", 409)
    return body


def _validate_state(state, tenant_id, owner_id):
    try:
        _core().validate_state(state)
        if state["tenantId"] != tenant_id:
            raise ValueError()
        if state["approval"] is not None and state["approval"]["ownerId"] != owner_id:
            raise ValueError()
        if any(record["ownerId"] != owner_id for record in state["records"].values()):
            raise ValueError()
        _serialized(state)
    except RepositoryError:
        raise
    except Exception:
        # Never echo source records, recipient data, JSON or database errors.
        raise RepositoryError("invalid_payment_request_state") from None


def _retain_exposure(before, after):
    """The request-only model has no authority to erase earned principal holds."""
    if before is None:
        return
    if (not set(before["records"]).issubset(after["records"])
            or not set(before["obligations"]).issubset(after["obligations"])
            or after["settledPrincipalAtomic"] < before["settledPrincipalAtomic"]
            or any(after["settledByDay"].get(day, 0) < amount for day, amount in before["settledByDay"].items())):
        raise RepositoryError("payment_request_exposure_cannot_be_erased", 409)
    for request_id, original in before["records"].items():
        current = after["records"][request_id]
        if (current["principalHoldAtomic"] != original["principalHoldAtomic"]
                or current["amountAtomic"] != original["amountAtomic"]
                or current["jobId"] != original["jobId"]
                or current["sourceHash"] != original["sourceHash"]
                or current["obligationKey"] != original["obligationKey"]
                or after["obligations"].get(original["obligationKey"]) != request_id):
            raise RepositoryError("payment_request_exposure_cannot_be_erased", 409)


class PaymentTables:
    """Caller owns fresh native identity, authorization, lock and transaction."""
    def __init__(self, repository):
        self.repository = repository
        if repository.connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise RepositoryError("payment_requests_migration_required")

    def _db(self):
        connection = self.repository.connection
        if connection is None or not connection.in_transaction:
            raise RepositoryError("native_transaction_required")
        return connection

    def tenant(self, tenant_id):
        row = self._db().execute("SELECT tenant_id,owner_id FROM x_exchange_tenants WHERE tenant_id=?", (exchange._identifier(tenant_id),)).fetchone()
        return None if row is None else dict(zip(("tenant_id", "owner_id"), row))

    def load_state(self, tenant_id):
        row = self._db().execute("SELECT body_json,version FROM p_payment_request_state WHERE tenant_id=?", (exchange._identifier(tenant_id),)).fetchone()
        return (None, 0) if row is None else (json.loads(row[0]), row[1])

    def save_state(self, tenant_id, state, expected_version):
        db = self._db()
        tenant = self.tenant(tenant_id)
        if tenant is None:
            raise RepositoryError("payment_request_tenant_required", 403)
        if type(expected_version) is not int or not 0 <= expected_version < 2147483647:
            raise RepositoryError("invalid_state_version", 409)
        _validate_state(state, tenant_id, tenant["owner_id"])
        before, _ = self.load_state(tenant_id)
        _retain_exposure(before, state)
        body = _serialized(state)
        if expected_version == 0:
            cursor = db.execute("INSERT INTO p_payment_request_state VALUES(?,?,1) ON CONFLICT(tenant_id) DO NOTHING", (tenant_id, body))
        else:
            cursor = db.execute("UPDATE p_payment_request_state SET body_json=?,version=version+1 WHERE tenant_id=? AND version=?", (body, tenant_id, expected_version))
        if cursor.rowcount != 1:
            raise RepositoryError("payment_request_state_conflict", 409)
        return expected_version + 1


def _fingerprints(native):
    result = exchange._fingerprints(native)
    result[SCHEMA_VERSION] = SCHEMA13_SHA256
    return result


def _metadata(connection):
    native, base = exchange._native()
    result = base._metadata(connection, SCHEMA_VERSION, _fingerprints(native), native.FulfillmentRepository._count_tables + exchange.TABLES + TABLES)
    exchange._validate_exchange_tables(connection)
    for tenant_id, body, version, owner_id in connection.execute("SELECT p.tenant_id,p.body_json,p.version,t.owner_id FROM p_payment_request_state p JOIN x_exchange_tenants t USING(tenant_id)"):
        try:
            exchange._identifier(tenant_id)
            _validate_state(json.loads(body), tenant_id, owner_id)
            if type(version) is not int or not 1 <= version <= 2147483647:
                raise ValueError()
        except (ValueError, TypeError):
            raise RepositoryError("invalid_payment_request_state") from None
    return result


def native_repository_class():
    native, base = exchange._native()

    class PaymentRequestRepository(native.FulfillmentRepository):
        _schema_version = SCHEMA_VERSION
        _schema_fingerprints = _fingerprints(native)
        _count_tables = native.FulfillmentRepository._count_tables + exchange.TABLES + TABLES

        def __init__(self, path, busy_timeout=2.0, *, migrate=False):
            if type(migrate) is not bool:
                raise RepositoryError("explicit_payment_migration_decision_required")
            regular = base._regular_path(path)
            connection = base._readonly(regular)
            try:
                connection.execute("BEGIN")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version == 12 and migrate:
                    exchange._metadata(connection)
                elif version == SCHEMA_VERSION:
                    _metadata(connection)
                else:
                    raise RepositoryError("explicit_payment_request_migration_required")
            finally:
                connection.close()
            self._payment_migrate = migrate
            # Bypass the schema12 constructor intentionally. Its exact preflight
            # is performed above; it must never auto-adopt a schema11 store.
            native.FulfillmentRepository.__init__(self, path, busy_timeout)

        def _migrate(self):
            with self.transaction():
                version = self.connection.execute("PRAGMA user_version").fetchone()[0]
                if version == 12 and self._payment_migrate:
                    for statement in DDL:
                        self.connection.execute(statement)
                    stamp = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
                    self.connection.execute("INSERT INTO schema_migrations VALUES(13,?,?)", (stamp, "Request-only principal bookkeeping; no budgets, requests, grants or execution authority provisioned"))
                    self.connection.execute("PRAGMA user_version=13")
                elif version != SCHEMA_VERSION:
                    raise RepositoryError("explicit_payment_request_migration_required")
                self._metadata(self.connection)

        def _metadata(self, connection):
            return _metadata(connection)

    return PaymentRequestRepository


def check_store(path):
    _, base = exchange._native()
    return base._check_store(path, _metadata)


def backup_store(source, destination):
    _, base = exchange._native()
    return base._backup_store(source, destination, _metadata)


def export_checkpoint(repository, directory):
    with repository.lock, repository.transaction():
        _metadata(repository.connection)
        document, encoded = make_checkpoint(repository.connection, SCHEMA13_SHA256, schema_version=SCHEMA_VERSION)
        published = publish_checkpoint(directory, document, encoded)
    return {"checkpointFile": published.pop("checkpointFile"), "pin": published}


def restore_store(backup, destination, *, authority_store=None, checkpoint_path=None, checkpoint_pin=None):
    """New isolated restore; exact current state/erasures and all holds retained.

    Requires the reviewed native hook to accept the ACTUAL schema13. No version
    impersonation or partial native-recovery fallback exists. Missing source,
    stale checkpoint, active writer or newer payment exposure publishes nothing.
    """
    native, base = exchange._native()
    hook = getattr(native, "apply_restore_holds", None)
    if not callable(hook):
        raise RepositoryError("complete_native_recovery_hook_required")
    if authority_store is None or checkpoint_path is None or checkpoint_pin is None:
        raise RepositoryError("current_erasure_authority_required")
    authority, source_backup = base._regular_path(authority_store), base._regular_path(backup)
    if authority.samefile(source_backup):
        raise RepositoryError("backup_cannot_supply_current_erasure_authority")
    lease = os.open(str(authority) + ".lock", os.O_RDWR | os.O_NOFOLLOW)
    authority_connection, native_result = None, {}
    reconciled = [0]

    def hold(connection):
        reconciled[0] = reconcile_erased_content(connection, current_document)
        # No fake schema12 argument: an old hook must reject the actual schema13.
        native_result.update(hook(connection, version=SCHEMA_VERSION))
        from ..controls.core import recover_state as recover_exchange
        from .core import recover_state as recover_payment
        now = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        for tenant_id, body, version in connection.execute("SELECT tenant_id,body_json,version FROM x_exchange_state").fetchall():
            recovered = recover_exchange(json.loads(body), now=now)
            if recovered["tenantId"] != tenant_id:
                raise RepositoryError("exchange_restore_tenant_mismatch")
            changed = connection.execute("UPDATE x_exchange_state SET body_json=?,version=version+1 WHERE tenant_id=? AND version=?", (exchange._serialize(recovered), tenant_id, version))
            if changed.rowcount != 1:
                raise RepositoryError("exchange_restore_state_conflict")
        for tenant_id, body, version, owner_id in connection.execute("SELECT p.tenant_id,p.body_json,p.version,t.owner_id FROM p_payment_request_state p JOIN x_exchange_tenants t USING(tenant_id)").fetchall():
            before = json.loads(body)
            recovered = recover_payment(before, now=now)
            _validate_state(recovered, tenant_id, owner_id)
            _retain_exposure(before, recovered)
            changed = connection.execute("UPDATE p_payment_request_state SET body_json=?,version=version+1 WHERE tenant_id=? AND version=?", (_serialized(recovered), tenant_id, version))
            if changed.rowcount != 1:
                raise RepositoryError("payment_restore_state_conflict")
        connection.execute("UPDATE x_exchange_memberships SET active=0")
        stamp = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        connection.execute("INSERT INTO security_events(principal_id,action,created_at,detail_json) VALUES(NULL,'payment_request_restore_held',?,?)", (stamp, '{"exchange_memberships_revoked":true,"payment_requests_stopped":true,"wallet_execution_enabled":false}'))

    try:
        if not stat.S_ISREG(os.fstat(lease).st_mode):
            raise RepositoryError("current_authority_lock_invalid")
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RepositoryError("current_authority_must_be_quiesced") from None
        authority_connection = base._readonly(authority)
        authority_connection.execute("BEGIN")
        _metadata(authority_connection)
        current_document, current_bytes = make_checkpoint(authority_connection, SCHEMA13_SHA256, schema_version=SCHEMA_VERSION)
        validate_checkpoint_file(checkpoint_path, checkpoint_pin, current_document, current_bytes)
        result = base._restore_store(backup, destination, accepted_versions=(SCHEMA_VERSION,), source_metadata=_metadata, output_metadata=_metadata, upgrade=hold)
        legacy_count = result["credentials_revoked"]
        result.update(native_result)
        return dict(result, credentials_revoked=legacy_count + native_result["provider_credentials_revoked"],
                    legacy_credentials_revoked=legacy_count, exchange_memberships_revoked=True,
                    exchange_stop_latched=True, payment_requests_stop_latched=True,
                    erased_records_reconciled=reconciled[0], erasure_authority_verified=True,
                    financial_execution_enabled=False)
    finally:
        if authority_connection is not None:
            authority_connection.close()
        os.close(lease)
