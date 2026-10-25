"""Additive native House repository boundary; never opens a store on import.

The native runtime is supplied by its existing installation. Ordinary startup
accepts only an already provisioned schema12. Migration is an explicit offline
operation; it creates no tenant, membership, resource, credential or grant.
"""
import datetime as dt
import json
import re

SCHEMA_VERSION = 12
MAX_STATE_BYTES = 1048576
TABLES = ("x_exchange_tenants", "x_exchange_state", "x_exchange_memberships")
SCOPES = frozenset(("exchange:read", "exchange:write", "exchange:admin"))
DDL = (
    "CREATE TABLE x_exchange_tenants(tenant_id TEXT PRIMARY KEY NOT NULL,owner_id TEXT NOT NULL REFERENCES principals(id),CHECK(length(tenant_id) BETWEEN 1 AND 100),CHECK(length(owner_id) BETWEEN 1 AND 100))",
    "CREATE TABLE x_exchange_state(tenant_id TEXT PRIMARY KEY NOT NULL REFERENCES x_exchange_tenants(tenant_id),body_json TEXT NOT NULL CHECK(json_valid(body_json) AND json_type(body_json)='object' AND length(CAST(body_json AS BLOB))<=1048576),version INTEGER NOT NULL CHECK(typeof(version)='integer' AND version BETWEEN 1 AND 2147483647))",
    "CREATE TABLE x_exchange_memberships(tenant_id TEXT NOT NULL REFERENCES x_exchange_tenants(tenant_id),principal_id TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('owner','agent')),native_role TEXT NOT NULL CHECK(native_role IN ('owner','buyer','provider')),scopes_json TEXT NOT NULL CHECK(json_valid(scopes_json) AND json_type(scopes_json)='array' AND length(CAST(scopes_json AS BLOB))<=128),active INTEGER NOT NULL CHECK(typeof(active)='integer' AND active IN (0,1)),expires_at INTEGER CHECK(expires_at IS NULL OR (typeof(expires_at)='integer' AND expires_at BETWEEN 0 AND 9007199254740991)),PRIMARY KEY(tenant_id,principal_id),CHECK((role='owner' AND native_role='owner') OR (role='agent' AND native_role IN ('buyer','provider'))),CHECK(length(principal_id) BETWEEN 1 AND 100))",
    "CREATE TRIGGER x_exchange_tenant_owner BEFORE INSERT ON x_exchange_tenants WHEN NOT EXISTS(SELECT 1 FROM principals WHERE id=NEW.owner_id AND role='owner' AND active=1) BEGIN SELECT RAISE(ABORT,'active native owner required'); END",
    "CREATE TRIGGER x_exchange_tenant_identity BEFORE UPDATE ON x_exchange_tenants BEGIN SELECT RAISE(ABORT,'tenant owner is immutable'); END",
    "CREATE TRIGGER x_exchange_tenant_retained BEFORE DELETE ON x_exchange_tenants BEGIN SELECT RAISE(ABORT,'tenant identity is retained'); END",
    "CREATE TRIGGER x_exchange_membership_owner BEFORE INSERT ON x_exchange_memberships WHEN (NEW.role='owner' AND NOT EXISTS(SELECT 1 FROM x_exchange_tenants WHERE tenant_id=NEW.tenant_id AND owner_id=NEW.principal_id)) OR (NEW.role='agent' AND EXISTS(SELECT 1 FROM x_exchange_tenants WHERE tenant_id=NEW.tenant_id AND owner_id=NEW.principal_id)) BEGIN SELECT RAISE(ABORT,'exact tenant role binding required'); END",
    "CREATE TRIGGER x_exchange_membership_identity BEFORE UPDATE OF tenant_id,principal_id,role,native_role ON x_exchange_memberships BEGIN SELECT RAISE(ABORT,'membership identity is immutable'); END",
    "CREATE TRIGGER x_exchange_membership_scopes_insert BEFORE INSERT ON x_exchange_memberships WHEN EXISTS(SELECT 1 FROM json_each(NEW.scopes_json) WHERE type!='text' OR value NOT IN ('exchange:read','exchange:write','exchange:admin')) OR (SELECT COUNT(*) FROM json_each(NEW.scopes_json))!=(SELECT COUNT(DISTINCT value) FROM json_each(NEW.scopes_json)) OR (NEW.role!='owner' AND EXISTS(SELECT 1 FROM json_each(NEW.scopes_json) WHERE value='exchange:admin')) BEGIN SELECT RAISE(ABORT,'explicit bounded exchange scopes required'); END",
    "CREATE TRIGGER x_exchange_membership_scopes_update BEFORE UPDATE OF scopes_json ON x_exchange_memberships WHEN EXISTS(SELECT 1 FROM json_each(NEW.scopes_json) WHERE type!='text' OR value NOT IN ('exchange:read','exchange:write','exchange:admin')) OR (SELECT COUNT(*) FROM json_each(NEW.scopes_json))!=(SELECT COUNT(DISTINCT value) FROM json_each(NEW.scopes_json)) OR (NEW.role!='owner' AND EXISTS(SELECT 1 FROM json_each(NEW.scopes_json) WHERE value='exchange:admin')) BEGIN SELECT RAISE(ABORT,'explicit bounded exchange scopes required'); END",
    "CREATE TRIGGER x_exchange_state_version BEFORE UPDATE ON x_exchange_state WHEN NEW.tenant_id!=OLD.tenant_id OR NEW.version!=OLD.version+1 BEGIN SELECT RAISE(ABORT,'state requires next exact version'); END",
)
# Independently pinned from a synthetic schema11 fixture plus the reviewed DDL.
# This is never learned from the store being opened.
SCHEMA12_SHA256 = "df34dfcc92de5df039c5d76bcf91b8c1373da9ff0dcb5f97d038d094f3c055d9"


class RepositoryError(Exception):
    def __init__(self, code, status=503):
        super().__init__(code)
        self.code, self.status = code, status


def _identifier(value):
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value) is None:
        raise RepositoryError("invalid_identifier", 400)
    return value


def _serialize(state):
    if not isinstance(state, dict):
        raise RepositoryError("invalid_exchange_state", 400)
    try:
        body = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        size = len(body.encode("utf-8"))
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise RepositoryError("invalid_exchange_state", 400) from None
    if size > MAX_STATE_BYTES:
        raise RepositoryError("exchange_state_capacity", 409)
    return body


class ExchangeTables:
    """Small persistence interface; caller owns native lock and transaction.

    Reads, credential checks, object authorization, command execution and CAS
    belong to that single transaction. Nothing here manufactures identity.
    """
    def __init__(self, repository):
        self.repository = repository
        if repository.connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise RepositoryError("exchange_migration_required")

    def _db(self):
        connection = self.repository.connection
        if connection is None or not connection.in_transaction:
            raise RepositoryError("native_transaction_required")
        return connection

    def tenant(self, tenant_id):
        row = self._db().execute("SELECT tenant_id,owner_id FROM x_exchange_tenants WHERE tenant_id=?", (_identifier(tenant_id),)).fetchone()
        return None if row is None else dict(zip(("tenant_id", "owner_id"), row))

    def membership(self, tenant_id, principal_id):
        columns = ("tenant_id", "principal_id", "role", "native_role", "scopes_json", "active", "expires_at")
        row = self._db().execute("SELECT " + ",".join(columns) + " FROM x_exchange_memberships WHERE tenant_id=? AND principal_id=?", (_identifier(tenant_id), _identifier(principal_id))).fetchone()
        if row is None:
            return None
        result = dict(zip(columns, row))
        result["scopes"] = json.loads(result["scopes_json"])
        return result

    def load_state(self, tenant_id):
        row = self._db().execute("SELECT body_json,version FROM x_exchange_state WHERE tenant_id=?", (_identifier(tenant_id),)).fetchone()
        return (None, 0) if row is None else (json.loads(row[0]), row[1])

    def save_state(self, tenant_id, state, expected_version):
        db = self._db()
        _identifier(tenant_id)
        if type(expected_version) is not int or not 0 <= expected_version < 2147483647:
            raise RepositoryError("invalid_state_version", 409)
        body = _serialize(state)
        if expected_version == 0:
            cursor = db.execute("INSERT INTO x_exchange_state(tenant_id,body_json,version) VALUES(?,?,1) ON CONFLICT(tenant_id) DO NOTHING", (tenant_id, body))
        else:
            cursor = db.execute("UPDATE x_exchange_state SET body_json=?,version=version+1 WHERE tenant_id=? AND version=?", (body, tenant_id, expected_version))
        if cursor.rowcount != 1:
            raise RepositoryError("exchange_state_conflict", 409)
        return expected_version + 1


def _native():
    # Existing installation supplies these modules. No private runtime source,
    # configuration, data, endpoint, key or principal is vendored in this tree.
    import fulfillment_workflow
    import stage2_store
    return fulfillment_workflow, stage2_store


def _fingerprints(native):
    result = dict(native._FINGERPRINTS)
    result[SCHEMA_VERSION] = SCHEMA12_SHA256
    return result


def _metadata(connection):
    native, base = _native()
    result = base._metadata(connection, SCHEMA_VERSION, _fingerprints(native), native.FulfillmentRepository._count_tables + TABLES)
    # json_each constraints are enforced by triggers as well as application
    # validation. Check existing rows during recovery/open, including files
    # modified by an external writer that disabled triggers or checks.
    for row in connection.execute("SELECT tenant_id,body_json,version FROM x_exchange_state"):
        try:
            _identifier(row[0])
            _serialize(json.loads(row[1]))
            if type(row[2]) is not int or not 1 <= row[2] <= 2147483647:
                raise ValueError()
        except (ValueError, TypeError, RepositoryError):
            raise RepositoryError("invalid_exchange_state") from None
    for row in connection.execute("SELECT m.tenant_id,m.principal_id,m.role,m.native_role,m.scopes_json,m.active,m.expires_at,t.owner_id FROM x_exchange_memberships m JOIN x_exchange_tenants t USING(tenant_id)"):
        try:
            _identifier(row[0]); _identifier(row[1])
            scopes = json.loads(row[4])
            if (not isinstance(scopes, list) or any(not isinstance(x, str) or x not in SCOPES for x in scopes)
                    or len(set(scopes)) != len(scopes)
                    or (row[2] == "owner") != (row[1] == row[7])
                    or (row[2] != "owner" and "exchange:admin" in scopes)):
                raise ValueError()
        except (ValueError, TypeError, RepositoryError):
            raise RepositoryError("invalid_exchange_membership") from None
    return result


def native_repository_class():
    """Return an explicit schema12 subclass of the installed House repository."""
    native, base = _native()

    class ExchangeRepository(native.FulfillmentRepository):
        _schema_version = SCHEMA_VERSION
        _schema_fingerprints = _fingerprints(native)
        _count_tables = native.FulfillmentRepository._count_tables + TABLES

        def __init__(self, path, busy_timeout=2.0, *, migrate=False):
            if type(migrate) is not bool:
                raise RepositoryError("explicit_migration_decision_required")
            # Reject missing/unrelated/new stores before the native opener can
            # create files or run older migrations. Native preflight repeats
            # schema checks after obtaining its lifetime writer lease.
            regular = base._regular_path(path)
            connection = base._readonly(regular)
            try:
                connection.execute("BEGIN")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version == 11 and migrate:
                    native._metadata(connection, version=11)
                elif version == SCHEMA_VERSION:
                    _metadata(connection)
                else:
                    raise RepositoryError("explicit_exchange_migration_required")
            finally:
                connection.close()
            self._exchange_migrate = migrate
            super().__init__(path, busy_timeout)

        def _migrate_extensions(self):
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 11:
                if not self._exchange_migrate:
                    raise RepositoryError("explicit_exchange_migration_required")
                for statement in DDL:
                    self.connection.execute(statement)
                stamp = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
                self.connection.execute("INSERT INTO schema_migrations VALUES(12,?,?)", (stamp, "Additive non-cash exchange controls; no memberships or resources provisioned"))
                self.connection.execute("PRAGMA user_version=12")
            elif version != SCHEMA_VERSION:
                raise RepositoryError("unsupported_exchange_schema")

        def _metadata(self, connection):
            return _metadata(connection)

    return ExchangeRepository


def check_store(path):
    _, base = _native()
    return base._check_store(path, _metadata)


def backup_store(source, destination):
    _, base = _native()
    return base._backup_store(source, destination, _metadata)


def restore_store(backup, destination):
    """Restore a new isolated STOP-held file; never overwrite the active file.

    Reuse native checksum/schema/integrity/recovery machinery. Every credential
    and exchange membership is revoked; native financial STOP is permanent.
    Existing evidence and consumed/reserved accounting remain for review but
    no actor retains access. Exchange STOP is recorded in every tenant state;
    stale assents, owner approvals and replay responses cannot revive authority.
    Recovery/reprovisioning requires a separate owner approval.
    """
    _, base = _native()
    provider_count = [0]

    def hold(connection):
        from .core import recover_state
        stamp = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        now = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        for tenant_id, body, version in connection.execute("SELECT tenant_id,body_json,version FROM x_exchange_state").fetchall():
            recovered = recover_state(json.loads(body), now=now)
            if recovered["tenantId"] != tenant_id:
                raise RepositoryError("exchange_restore_tenant_mismatch")
            cursor = connection.execute("UPDATE x_exchange_state SET body_json=?,version=version+1 WHERE tenant_id=? AND version=?", (_serialize(recovered), tenant_id, version))
            if cursor.rowcount != 1:
                raise RepositoryError("exchange_restore_state_conflict")
        provider_count[0] = connection.execute("SELECT COUNT(*) FROM r_credentials").fetchone()[0]
        connection.execute("UPDATE r_credentials SET revoked_at=COALESCE(revoked_at,?)", (stamp,))
        controls = connection.execute("SELECT revision FROM wf_controls WHERE id=1").fetchone()
        if controls is None or type(controls[0]) is not int or not 0 <= controls[0] < 2**63 - 1:
            raise RepositoryError("invalid_native_restore_control")
        connection.execute("UPDATE wf_controls SET stopped=1,revision=revision+1 WHERE id=1")
        connection.execute("UPDATE wf_authorities SET revoked_at=? WHERE revoked_at IS NULL", (stamp,))
        connection.execute("UPDATE x_exchange_memberships SET active=0")
        connection.execute("INSERT INTO security_events(principal_id,action,created_at,detail_json) VALUES(NULL,'exchange_restore_held',?,?)", (stamp, json.dumps({"exchange_memberships_revoked": True, "provider_credentials_revoked": provider_count[0], "native_financial_stop_latched": True}, separators=(",", ":"))))

    result = base._restore_store(backup, destination, accepted_versions=(SCHEMA_VERSION,), source_metadata=_metadata, output_metadata=_metadata, upgrade=hold)
    return dict(result, provider_credentials_revoked=provider_count[0], exchange_memberships_revoked=True, exchange_stop_latched=True, financial_stop_latched=True, financial_execution_enabled=False)
