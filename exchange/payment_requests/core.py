"""Owner-reviewed payment request preparation. No payment execution authority.

The host authenticates the current owner, resolves accepted *paid* native work,
and commits this reducer with the native repository in one transaction. Sources
are trusted host inputs, never HTTP body fields. No operation releases an earned
principal hold, expires a job, sends a payment, or makes a wallet handoff usable.
"""
from copy import deepcopy
from datetime import datetime, time, timedelta, timezone
from hashlib import sha256
import json
import re
import time as clock
from types import MappingProxyType
import uuid
from zoneinfo import ZoneInfo

SCHEMA_VERSION = 1
PER_JOB_ATOMIC = 100_000
PER_DAY_ATOMIC = 1_000_000
LIFETIME_ATOMIC = 5_000_000
TIMEZONE = "America/Los_Angeles"
MAX_RECORDS = 1000
MAX_RETRIES = 4000
MAX_AUDIT = 256
MAX_STATE_BYTES = 700 * 1024
MAX_SAFETY_BYTES = 950 * 1024
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
HASH = re.compile(r"^[0-9a-f]{64}$")
PAYEE = re.compile(r"^0x[0-9a-fA-F]{40}$")
SOURCE_FIELDS = {"tenantId", "jobId", "ownerId", "workerId", "payee", "termsHash",
                 "evidenceHash", "acceptanceId", "acceptanceHash", "amountAtomic",
                 "currency", "network", "asset", "decimals", "sourceKind", "status",
                 "payeeVerification", "termsVersion", "termsDocumentHash", "evidenceId",
                 "evidenceSize", "recordEvidenceClass", "artifactCheck"}
LIMITS = MappingProxyType({"perJobAtomic": PER_JOB_ATOMIC, "perDayAtomic": PER_DAY_ATOMIC,
          "lifetimeAtomic": LIFETIME_ATOMIC, "timezone": TIMEZONE,
          "currency": "USDC", "network": "base", "asset": "usdc", "decimals": 6})


class ControlError(Exception):
    def __init__(self, status, code, message=None):
        self.status, self.code = status, code
        super().__init__(message or code)


def fail(status, code):
    raise ControlError(status, code)


def canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value):
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def exact(value, fields, required=None):
    if not isinstance(value, dict) or set(value) - set(fields) or set(required or ()) - set(value):
        fail(400, "invalid_fields")


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        fail(400, "invalid_identifier")
    return value


def integer(value, minimum=0, maximum=LIFETIME_ATOMIC):
    if type(value) is not int or not minimum <= value <= maximum:
        fail(400, "invalid_integer")
    return value


def text(value, maximum=500):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        fail(400, "invalid_text")
    try:
        value.encode("utf-8")
    except UnicodeError:
        fail(400, "invalid_text")
    return value.strip()


def check_hash(value):
    if not isinstance(value, str) or not HASH.fullmatch(value):
        fail(400, "invalid_hash")
    return value


def day_window(now):
    integer(now, 0, 4_102_444_800_000)
    zone = ZoneInfo(TIMEZONE)
    local = datetime.fromtimestamp(now / 1000, timezone.utc).astimezone(zone)
    start = datetime.combine(local.date(), time.min, tzinfo=zone)
    end = datetime.combine(local.date() + timedelta(days=1), time.min, tzinfo=zone)
    return {"date": local.date().isoformat(), "timezone": TIMEZONE,
            "startUtcMs": int(start.timestamp() * 1000), "endUtcMs": int(end.timestamp() * 1000)}


def empty_state(tenant_id):
    identifier(tenant_id)
    return {"schemaVersion": SCHEMA_VERSION, "tenantId": tenant_id, "stopped": True,
            "approval": None, "records": {}, "obligations": {}, "retries": [],
            "settledPrincipalAtomic": 0, "settledByDay": {}, "audit": [], "auditDropped": 0}


def _source(value, tenant_id, owner_id):
    exact(value, SOURCE_FIELDS, SOURCE_FIELDS)
    if value["tenantId"] != tenant_id or value["ownerId"] != owner_id:
        fail(403, "source_owner_mismatch")
    for field in ("tenantId", "jobId", "ownerId", "workerId", "acceptanceId", "evidenceId"):
        identifier(value[field])
    for field in ("termsHash", "evidenceHash", "acceptanceHash", "termsDocumentHash"):
        check_hash(value[field])
    integer(value["termsVersion"], 1, 10**12)
    integer(value["evidenceSize"], 0, 2**63 - 1)
    if value["recordEvidenceClass"] not in ("live", "synthetic") or value["artifactCheck"] != {"status": "unavailable", "reason": "artifact_bytes_not_retained"}:
        fail(403, "unsupported_artifact_verification")
    if not isinstance(value["payee"], str) or not PAYEE.fullmatch(value["payee"]) or int(value["payee"][2:], 16) == 0:
        fail(400, "invalid_payee")
    integer(value["amountAtomic"], 1, PER_JOB_ATOMIC)
    if (value["currency"], value["network"], value["asset"], value["decimals"], value["sourceKind"], value["status"]) != ("USDC", "base", "usdc", 6, "native-paid-fulfillment", "accepted") or type(value["decimals"]) is not int:
        fail(403, "accepted_paid_source_required")
    verification = value["payeeVerification"]
    exact(verification, {"basis", "bindingHash", "independentProof"}, {"basis", "bindingHash", "independentProof"})
    if verification["basis"] != "owner_attestation" or verification["independentProof"] is not False:
        fail(403, "unsupported_payee_verification")
    check_hash(verification["bindingHash"])
    return deepcopy(value)


def _principal(state, value, scope):
    if not isinstance(value, dict):
        fail(401, "authentication_required")
    if value.get("tenantId") != state["tenantId"]:
        fail(403, "tenant_mismatch")
    identifier(value.get("subjectId"))
    if value.get("role") != "owner":
        fail(403, "owner_required")
    if not isinstance(value.get("scopes"), list) or scope not in value["scopes"]:
        fail(403, "scope_required")
    return value


def _obligation(tenant_id, job_id):
    return digest({"domain": "lumen-house/earned-principal/v1", "tenantId": tenant_id, "jobId": job_id})


def _request_hash(row):
    return digest({"domain": "lumen-house/payment-request/v1", **{key: row[key] for key in (
        "tenantId", "obligationKey", "sourceHash", "policyHash", "amountAtomic", "currency",
        "network", "asset", "decimals", "createdAt", "dayWindow")}})


def _event(state, action, principal, now, request_id=None):
    entry = {"action": action, "actorId": principal["subjectId"], "at": now, "requestId": request_id,
             "sequence": state["audit"][-1]["sequence"] + 1 if state["audit"] else state["auditDropped"] + 1,
             "previousHash": state["audit"][-1]["hash"] if state["audit"] else None}
    entry["hash"] = digest(entry)
    state["audit"].append(entry)
    if len(state["audit"]) > MAX_AUDIT:
        state["audit"].pop(0)
        state["auditDropped"] += 1


def _exposure(state, now):
    window = day_window(now)
    held = sum(record["principalHoldAtomic"] for record in state["records"].values())
    settled_today = state["settledByDay"].get(window["date"], 0)
    return {"dayWindow": window, "heldPrincipalAtomic": held,
            "settledTodayAtomic": settled_today, "dailyExposureAtomic": settled_today + held,
            "settledPrincipalAtomic": state["settledPrincipalAtomic"],
            "lifetimeExposureAtomic": state["settledPrincipalAtomic"] + held}


def _policy(state, now):
    return {"stopped": state["stopped"], "approval": deepcopy(state["approval"]), "ceilings": dict(LIMITS),
            "exposure": _exposure(state, now), "executionEnabled": False, "handoffAvailable": False,
            "fees": {"status": "unknown", "amountAtomic": None, "approved": False}}


def _validate(state):
    try:
        fields = {"schemaVersion", "tenantId", "stopped", "approval", "records", "obligations", "retries",
                  "settledPrincipalAtomic", "settledByDay", "audit", "auditDropped"}
        exact(state, fields, fields)
        if type(state["schemaVersion"]) is not int or state["schemaVersion"] != SCHEMA_VERSION or type(state["stopped"]) is not bool:
            fail(503, "unsupported_state")
        identifier(state["tenantId"])
        if not all(isinstance(state[k], dict) for k in ("records", "obligations", "settledByDay")) or not isinstance(state["retries"], list) or not isinstance(state["audit"], list):
            fail(503, "corrupt_state")
        if len(state["records"]) > MAX_RECORDS or len(state["retries"]) > MAX_RETRIES or len(state["audit"]) > MAX_AUDIT:
            fail(503, "corrupt_state")
        integer(state["auditDropped"], 0, 10**12)
        if state["approval"] is None:
            if not state["stopped"] or state["records"] or state["retries"]:
                fail(503, "corrupt_state")
        else:
            exact(state["approval"], {"reference", "ownerId", "at", "ceilingsHash"}, {"reference", "ownerId", "at", "ceilingsHash"})
            text(state["approval"]["reference"])
            identifier(state["approval"]["ownerId"])
            integer(state["approval"]["at"], 0, 4_102_444_800_000)
            if state["approval"]["ceilingsHash"] != digest(dict(LIMITS)):
                fail(503, "corrupt_ceilings")
        integer(state["settledPrincipalAtomic"])
        for day, amount in state["settledByDay"].items():
            if datetime.strptime(day, "%Y-%m-%d").date().isoformat() != day:
                fail(503, "corrupt_state")
            integer(amount, 0, PER_DAY_ATOMIC)
        if sum(state["settledByDay"].values()) != state["settledPrincipalAtomic"]:
            fail(503, "corrupt_accounting")
        obligations = {}
        held = 0
        for rid, row in state["records"].items():
            identifier(rid)
            row_fields = {"id", "tenantId", "jobId", "ownerId", "obligationKey", "sourceHash", "source", "amountAtomic", "principalHoldAtomic", "preparedAt", "createdAt", "dayWindow", "policyHash", "currency", "network", "asset", "decimals", "status", "reportedOutcome", "executionEnabled", "handoffAvailable", "fees", "requestHash"}
            exact(row, row_fields | {"outcomeReport"}, row_fields)
            if row["id"] != rid or row["tenantId"] != state["tenantId"]:
                fail(503, "corrupt_state")
            source = _source(row["source"], state["tenantId"], row["ownerId"])
            if row["sourceHash"] != digest(source) or row["jobId"] != source["jobId"] or row["ownerId"] != source["ownerId"]:
                fail(503, "corrupt_binding")
            if row["policyHash"] != digest(dict(LIMITS)) or row["requestHash"] != _request_hash(row):
                fail(503, "corrupt_request_hash")
            obligation = _obligation(state["tenantId"], row["jobId"])
            if row["obligationKey"] != obligation or obligation in obligations:
                fail(503, "corrupt_obligation")
            obligations[obligation] = rid
            if type(row["amountAtomic"]) is not int or type(row["principalHoldAtomic"]) is not int or row["amountAtomic"] != source["amountAtomic"] or row["principalHoldAtomic"] != row["amountAtomic"]:
                fail(503, "corrupt_accounting")
            integer(row["createdAt"], 0, 4_102_444_800_000)
            if any(row[k] != source[k] for k in ("currency", "network", "asset", "decimals")) or type(row["decimals"]) is not int or row["createdAt"] != row["preparedAt"]:
                fail(503, "corrupt_binding")
            if row["status"] != "awaiting_owner_wallet_setup" or row["reportedOutcome"] not in (None, "failed", "unknown", "wallet_cancelled"):
                fail(503, "corrupt_state")
            if row["executionEnabled"] is not False or row["handoffAvailable"] is not False or canonical_json(row["fees"]) != canonical_json({"status": "unknown", "amountAtomic": None, "approved": False}):
                fail(503, "corrupt_authority")
            if canonical_json(row["dayWindow"]) != canonical_json(day_window(row["preparedAt"])):
                fail(503, "corrupt_day")
            if row["reportedOutcome"] is None and "outcomeReport" in row:
                fail(503, "corrupt_outcome")
            if row["reportedOutcome"] is not None:
                report = row["outcomeReport"]
                exact(report, {"ownerId", "at", "reason", "verified"}, {"ownerId", "at", "reason", "verified"})
                text(report["reason"], 300)
                integer(report["at"], row["preparedAt"], 4_102_444_800_000)
                if report["ownerId"] != row["ownerId"] or report["verified"] is not False:
                    fail(503, "corrupt_outcome")
            held += row["principalHoldAtomic"]
        if obligations != state["obligations"] or held + state["settledPrincipalAtomic"] > LIFETIME_ATOMIC:
            fail(503, "corrupt_accounting")
        keys = set()
        for retry in state["retries"]:
            exact(retry, {"keyHash", "fingerprint", "requestId", "disabled"}, {"keyHash", "fingerprint", "requestId", "disabled"})
            check_hash(retry["keyHash"])
            check_hash(retry["fingerprint"])
            if retry["keyHash"] in keys or retry["requestId"] not in state["records"] or type(retry["disabled"]) is not bool:
                fail(503, "corrupt_retry")
            keys.add(retry["keyHash"])
        previous, sequence = None, state["auditDropped"]
        for event in state["audit"]:
            exact(event, {"action", "actorId", "at", "requestId", "sequence", "previousHash", "hash"}, {"action", "actorId", "at", "requestId", "sequence", "previousHash", "hash"})
            if event["action"] not in ("configure", "open", "stop", "prepare", "report_outcome", "offline-recovery"):
                fail(503, "corrupt_audit")
            identifier(event["actorId"])
            integer(event["at"], 0, 4_102_444_800_000)
            integer(event["sequence"], 1, 10**12 + MAX_AUDIT)
            if event["requestId"] is not None:
                identifier(event["requestId"])
            if event["previousHash"] is not None:
                check_hash(event["previousHash"])
                if sequence == 0:
                    fail(503, "corrupt_audit")
            elif sequence != 0:
                fail(503, "corrupt_audit")
            if event["sequence"] != sequence + 1 or (previous is not None and event["previousHash"] != previous) or event["hash"] != digest({k: v for k, v in event.items() if k != "hash"}):
                fail(503, "corrupt_audit")
            previous, sequence = event["hash"], event["sequence"]
        if len(canonical_json(state).encode("utf-8")) > MAX_SAFETY_BYTES:
            fail(503, "corrupt_state_capacity")
    except ControlError as error:
        if error.status == 503:
            raise
        fail(503, "corrupt_state")
    except (KeyError, TypeError, ValueError, OverflowError):
        fail(503, "corrupt_state")


def _owned_record(state, request_id, principal):
    identifier(request_id)
    row = state["records"].get(request_id)
    if row is None or row["ownerId"] != principal["subjectId"]:
        fail(404, "request_not_found")
    return row


def validate_state(state):
    """Repository entry point: refuse unknown/corrupt state without changing it."""
    _validate(state)


def _projected_size(state):
    # Reserve actual UTF-8/JSON bytes for every permitted future report and
    # bounded control history. Even escaped control characters must fit; no
    # cleanup may reclaim principal to make a stop or unknown report writable.
    projected = deepcopy(state)
    for row in projected["records"].values():
        if row["reportedOutcome"] is None:
            row["reportedOutcome"] = "wallet_cancelled"
            row["outcomeReport"] = {"ownerId": "A" * 128, "at": 4_102_444_800_000, "reason": "\0" * 300, "verified": False}
    if projected["approval"] is not None:
        projected["approval"].update(reference="\0" * 500, ownerId="A" * 128, at=4_102_444_800_000)
    projected["auditDropped"] = 10**12
    event = {"action": "offline-recovery", "actorId": "A" * 128, "requestId": "A" * 128, "at": 4_102_444_800_000, "sequence": 10**12 + MAX_AUDIT, "previousHash": "f" * 64, "hash": "f" * 64}
    projected["audit"] = [event] * MAX_AUDIT
    return len(canonical_json(projected).encode("utf-8"))


def _admit(state, *, ordinary=True):
    if (ordinary and len(canonical_json(state).encode("utf-8")) > MAX_STATE_BYTES) or _projected_size(state) > MAX_SAFETY_BYTES:
        fail(409, "state_capacity_reached")


def _remember(state, key_hash, fingerprint, request_id, *, ordinary=True):
    if len(state["retries"]) >= MAX_RETRIES:
        fail(409, "request_capacity_reached")
    state["retries"].append({"keyHash": key_hash, "fingerprint": fingerprint, "requestId": request_id, "disabled": False})
    _admit(state, ordinary=ordinary)


def apply_operation(before, operation, *, now=None, id=None):
    _validate(before)
    exact(operation, {"kind", "principal", "command", "key", "id", "source"}, {"kind", "principal"})
    now = int(clock.time() * 1000) if now is None else integer(now, 0, 4_102_444_800_000)
    state = deepcopy(before)
    kind = operation["kind"]
    if kind in ("get", "list", "policy"):
        principal = _principal(state, operation["principal"], "payments:read")
        if kind == "get":
            result = deepcopy(_owned_record(state, operation.get("id"), principal))
        elif kind == "list":
            result = [deepcopy(r) for r in state["records"].values() if r["ownerId"] == principal["subjectId"]]
        else:
            result = _policy(state, now)
        return {"state": state, "result": result}
    if kind != "command":
        fail(400, "unknown_operation")
    command = operation.get("command")
    exact(command, {"action", "id", "input"}, {"action"})
    action, value = command["action"], command.get("input", {})
    if action not in ("configure", "open", "stop", "prepare", "report_outcome"):
        fail(400, "unsupported_action")
    principal = _principal(state, operation["principal"], "payments:prepare" if action == "prepare" else "payments:control")
    if action in ("configure", "open", "stop"):
        if command.get("id") is not None or operation.get("source") is not None:
            fail(400, "unexpected_source")
        if action == "configure":
            exact(value, {"approvalReference"}, {"approvalReference"})
            state["approval"] = {"reference": text(value["approvalReference"]), "ownerId": principal["subjectId"], "at": now, "ceilingsHash": digest(dict(LIMITS))}
            state["stopped"] = True
        elif action == "open":
            exact(value, {})
            if state["approval"] is None:
                fail(409, "approval_required")
            if state["approval"]["ownerId"] != principal["subjectId"]:
                fail(403, "owner_mismatch")
            state["stopped"] = False
        else:
            exact(value, {"reason"}, {"reason"})
            text(value["reason"], 300)
            state["stopped"] = True
        _event(state, action, principal, now)
        if action == "configure":
            _admit(state, ordinary=False)
        _validate(state)
        return {"state": state, "result": _policy(state, now)}
    if action == "report_outcome":
        exact(value, {"outcome", "reason"}, {"outcome", "reason"})
        if operation.get("source") is not None:
            fail(400, "unexpected_source")
        if value["outcome"] not in ("failed", "unknown", "wallet_cancelled"):
            fail(400, "unsupported_outcome")
        text(value["reason"], 300)
        row = _owned_record(state, command.get("id"), principal)
        if row["reportedOutcome"] is None:
            row["reportedOutcome"] = value["outcome"]
            row["outcomeReport"] = {"ownerId": principal["subjectId"], "at": now, "reason": value["reason"].strip(), "verified": False}
            _event(state, action, principal, now, row["id"])
        elif row["reportedOutcome"] != value["outcome"]:
            fail(409, "outcome_already_reported")
        _validate(state)
        return {"state": state, "result": deepcopy(row)}
    exact(value, {"jobId"}, {"jobId"})
    identifier(value["jobId"])
    if command.get("id") is not None:
        fail(400, "unexpected_request_id")
    if state["stopped"] or state["approval"] is None:
        fail(503, "payment_requests_stopped")
    if state["approval"]["ownerId"] != principal["subjectId"]:
        fail(403, "owner_mismatch")
    source = _source(operation.get("source"), state["tenantId"], principal["subjectId"])
    if source["jobId"] != value["jobId"]:
        fail(403, "source_job_mismatch")
    source_hash, obligation = digest(source), _obligation(state["tenantId"], source["jobId"])
    key = operation.get("key")
    if not isinstance(key, str) or not KEY.fullmatch(key):
        fail(400, "idempotency_key_required")
    key_hash = digest({"tenantId": state["tenantId"], "ownerId": principal["subjectId"], "key": key})
    fingerprint = digest({"command": command, "sourceHash": source_hash})
    prior = next((r for r in state["retries"] if r["keyHash"] == key_hash), None)
    if prior:
        if prior["fingerprint"] != fingerprint:
            fail(409, "idempotency_conflict")
        if prior["disabled"]:
            fail(409, "recovery_reauthorization_required")
        return {"state": state, "result": deepcopy(_owned_record(state, prior["requestId"], principal))}
    if obligation in state["obligations"]:
        row = state["records"][state["obligations"][obligation]]
        if row["sourceHash"] != source_hash:
            fail(409, "obligation_binding_changed")
        _remember(state, key_hash, fingerprint, row["id"], ordinary=False)
        return {"state": state, "result": deepcopy(row)}
    exposure, amount = _exposure(state, now), source["amountAtomic"]
    if exposure["dailyExposureAtomic"] + amount > PER_DAY_ATOMIC:
        fail(409, "daily_principal_ceiling")
    if exposure["lifetimeExposureAtomic"] + amount > LIFETIME_ATOMIC:
        fail(409, "lifetime_principal_ceiling")
    if len(state["records"]) >= MAX_RECORDS or len(state["retries"]) >= MAX_RETRIES:
        fail(409, "request_capacity_reached")
    request_id = identifier(id or str(uuid.uuid4()))
    if request_id in state["records"]:
        fail(409, "request_id_collision")
    row = {"id": request_id, "tenantId": state["tenantId"], "jobId": source["jobId"],
           "ownerId": principal["subjectId"], "obligationKey": obligation, "sourceHash": source_hash,
           "source": source, "amountAtomic": amount, "principalHoldAtomic": amount,
           "preparedAt": now, "createdAt": now, "dayWindow": exposure["dayWindow"],
           "policyHash": digest(dict(LIMITS)), **{k: source[k] for k in ("currency", "network", "asset", "decimals")},
           "status": "awaiting_owner_wallet_setup", "reportedOutcome": None,
           "executionEnabled": False, "handoffAvailable": False,
           "fees": {"status": "unknown", "amountAtomic": None, "approved": False}}
    row["requestHash"] = _request_hash(row)
    state["records"][request_id] = row
    state["obligations"][obligation] = request_id
    _remember(state, key_hash, fingerprint, request_id)
    _event(state, "prepare", principal, now, request_id)
    _validate(state)
    _admit(state)
    return {"state": state, "result": deepcopy(row)}


def recover_state(before, *, now=None):
    """Offline restore fences preparation; no hold or acceptance is discarded."""
    _validate(before)
    now = int(clock.time() * 1000) if now is None else integer(now, 0, 4_102_444_800_000)
    state = deepcopy(before)
    state["stopped"] = True
    for retry in state["retries"]:
        retry["disabled"] = True
    _event(state, "offline-recovery", {"subjectId": "system:offline-recovery"}, now)
    _validate(state)
    return state
