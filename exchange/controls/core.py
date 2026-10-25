"""Pure exchange controls; the host must authenticate and commit atomically.

All state, including reservations, consumed capacity, assent and retries, belongs
in one native tenant repository transaction. This module performs no I/O, grants
no actual resource access, and contains no payment or wallet integration.
"""

from copy import deepcopy
from hashlib import sha256
import json
import re
import time
import uuid

SCHEMA_VERSION = 1
ACTIVE = frozenset(("proposed", "agreed", "delivered", "disputed"))
TERMINAL = frozenset(("completed", "cancelled", "revoked"))
MAX_REQUESTS = 4000
MAX_EVENTS = 100
NORMAL_STATE_BYTES = 650 * 1024
SAFETY_STATE_BYTES = 950 * 1024
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ControlError(Exception):
    def __init__(self, status, code, message=None):
        self.status = status
        self.code = code
        super().__init__(message or code)


def fail(status, code, message=None):
    raise ControlError(status, code, message)


def canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def digest(value):
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def empty_state(tenant_id):
    identifier(tenant_id)
    return {"schemaVersion": SCHEMA_VERSION, "tenantId": tenant_id,
            "stopped": True, "policyRevision": 0, "limits": None,
            "resources": {}, "grants": {}, "records": {},
            "requests": [], "administrativeAudit": []}


def exact(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        fail(400, "invalid_fields")


def text(value, maximum=1000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        fail(400, "invalid_text")
    return value.strip()


def identifier(value):
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        fail(400, "invalid_identifier")
    return value


def integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        fail(400, "invalid_integer")
    return value


def check_hash(value):
    if not isinstance(value, str) or not HASH_PATTERN.fullmatch(value):
        fail(400, "invalid_hash")
    return value


def _validate_state(state):
    """Refuse unknown/corrupt state; never reset it or repair away debt."""
    try:
        exact(state, {"schemaVersion", "tenantId", "stopped", "policyRevision", "limits",
                      "resources", "grants", "records", "requests", "administrativeAudit"},
              {"schemaVersion", "tenantId", "stopped", "policyRevision", "limits",
               "resources", "grants", "records", "requests", "administrativeAudit"})
        if type(state["schemaVersion"]) is not int or state["schemaVersion"] != SCHEMA_VERSION or type(state["stopped"]) is not bool:
            fail(503, "unsupported_state")
        identifier(state["tenantId"])
        integer(state["policyRevision"], 0, 10**12)
        if not all(isinstance(state[k], dict) for k in ("resources", "grants", "records")):
            fail(503, "corrupt_state")
        if not isinstance(state["requests"], list) or len(state["requests"]) > MAX_REQUESTS:
            fail(503, "corrupt_state")
        if not isinstance(state["administrativeAudit"], list) or len(state["administrativeAudit"]) > 256:
            fail(503, "corrupt_state")
        seen_keys = set()
        for request in state["requests"]:
            exact(request, {"keyHash", "fingerprint", "proposalId", "result", "resultHash", "expiresAt", "policyRevision"},
                  {"keyHash", "fingerprint", "proposalId", "result", "resultHash", "expiresAt", "policyRevision"})
            check_hash(request["keyHash"])
            check_hash(request["fingerprint"])
            if request["keyHash"] in seen_keys:
                fail(503, "corrupt_state")
            seen_keys.add(request["keyHash"])
            integer(request["expiresAt"], 0, 10**15)
            integer(request["policyRevision"], 0, 10**12)
            if request["resultHash"] != digest(request["result"]):
                fail(503, "corrupt_retry")
            if request["proposalId"] is not None:
                identifier(request["proposalId"])
                if request["result"] is not None and request["proposalId"] not in state["records"]:
                    fail(503, "corrupt_state")
        if state["limits"] is None:
            if state["resources"] or state["grants"] or state["records"] or not state["stopped"]:
                fail(503, "corrupt_state")
            return
        _limits(state["limits"])
        if len(state["records"]) > state["limits"]["maxRecords"]:
            fail(503, "corrupt_state")
        reserved = {r: 0 for r in state["resources"]}
        grant_reserved = {g: 0 for g in state["grants"]}
        retained_used = {r: 0 for r in state["resources"]}
        retained_grant_used = {g: 0 for g in state["grants"]}
        for rid, resource in state["resources"].items():
            identifier(rid)
            exact(resource, {"capacity", "reserved", "used"}, {"capacity", "reserved", "used"})
            for field in resource:
                integer(resource[field], 0, 10**6)
            if resource["used"] + resource["reserved"] > resource["capacity"]:
                fail(503, "corrupt_accounting")
        for sid, grant in state["grants"].items():
            identifier(sid)
            exact(grant, {"resources", "maxUnits", "reserved", "used", "revoked"},
                  {"resources", "maxUnits", "reserved", "used", "revoked"})
            integer(grant["maxUnits"], 0, 10**6)
            integer(grant["reserved"], 0, 10**6)
            integer(grant["used"], 0, 10**6)
            if type(grant["revoked"]) is not bool or not isinstance(grant["resources"], list):
                fail(503, "corrupt_state")
            if any(r not in state["resources"] for r in grant["resources"]):
                fail(503, "corrupt_state")
            if grant["reserved"] + grant["used"] > grant["maxUnits"]:
                fail(503, "corrupt_accounting")
        for pid, proposal in state["records"].items():
            identifier(pid)
            if proposal["id"] != pid or proposal["tenantId"] != state["tenantId"]:
                fail(503, "corrupt_state")
            integer(proposal["createdAt"], 0, 10**15)
            integer(proposal["expiresAt"], proposal["createdAt"] + 1, 10**15)
            if proposal["status"] not in ACTIVE | TERMINAL:
                fail(503, "corrupt_state")
            if len(proposal["events"]) > MAX_EVENTS or len(proposal["assents"]) > 2:
                fail(503, "corrupt_state")
            if proposal["requesterId"] == proposal["providerId"]:
                fail(503, "corrupt_state")
            if proposal["termsHash"] != _terms_hash(proposal):
                fail(503, "corrupt_terms")
            integer(proposal["termsRevision"], 1, 10**12)
            integer(proposal["policyRevision"], 1, state["policyRevision"])
            if proposal["disclosure"] != _disclosure(proposal["disclosure"]["retentionMs"], proposal["disclosure"]["reservationMs"]):
                fail(503, "corrupt_disclosure")
            integer(proposal["disclosure"]["retentionMs"], 3600000, 30 * 86400000)
            integer(proposal["disclosure"]["reservationMs"], 60000, min(proposal["disclosure"]["retentionMs"], 7 * 86400000))
            participants = {proposal["requesterId"], proposal["providerId"]}
            if set(proposal["assents"]) - participants:
                fail(503, "corrupt_assent")
            for sid, assent in proposal["assents"].items():
                exact(assent, {"subjectId", "termsHash", "termsRevision", "at", "sharingConsent"},
                      {"subjectId", "termsHash", "termsRevision", "at", "sharingConsent"})
                if assent["subjectId"] != sid or assent["termsHash"] != proposal["termsHash"] or assent["termsRevision"] != proposal["termsRevision"] or assent["sharingConsent"] is not True:
                    fail(503, "corrupt_assent")
                integer(assent["at"], proposal["createdAt"], 10**15)
            previous = None
            sequence = None
            for event in proposal["events"]:
                if sequence is None and event["sequence"] != proposal.get("auditDropped", 0) + 1:
                    fail(503, "corrupt_audit")
                if sequence is None and not proposal.get("auditDropped", 0) and event["previousHash"] is not None:
                    fail(503, "corrupt_audit")
                if sequence is not None and (event["sequence"] != sequence + 1 or event["previousHash"] != previous):
                    fail(503, "corrupt_audit")
                event_body = {k: v for k, v in event.items() if k != "hash"}
                if digest(event_body) != event["hash"]:
                    fail(503, "corrupt_audit")
                previous, sequence = event["hash"], event["sequence"]
            evidence = proposal.get("evidence")
            if evidence:
                exact(evidence, {"sha256", "description", "deliveryRevision", "providerId", "at", "termsHash", "deliveryHash"},
                      {"sha256", "description", "deliveryRevision", "providerId", "at", "termsHash", "deliveryHash"})
                check_hash(evidence["sha256"])
                text(evidence["description"])
                integer(evidence["deliveryRevision"], 1, 1)
                integer(evidence["at"], proposal["createdAt"], 10**15)
                evidence_body = {k: v for k, v in evidence.items() if k != "deliveryHash"}
                if evidence["deliveryHash"] != digest({"domain": "lumen-house-exchange/delivery/v1", "id": pid, **evidence_body}):
                    fail(503, "corrupt_evidence")
                if evidence["providerId"] != proposal["providerId"] or evidence["termsHash"] != proposal["termsHash"]:
                    fail(503, "corrupt_evidence")
            if proposal["status"] in ("agreed", "delivered", "completed") and (not proposal.get("approved") or set(proposal["assents"]) != participants):
                fail(503, "corrupt_assent")
            approved = proposal.get("approved")
            if approved:
                exact(approved, {"ownerId", "at", "termsHash"}, {"ownerId", "at", "termsHash"})
                identifier(approved["ownerId"])
                integer(approved["at"], proposal["createdAt"], 10**15)
                if approved["ownerId"] in participants or approved["termsHash"] != proposal["termsHash"]:
                    fail(503, "corrupt_approval")
            if proposal["status"] == "completed" and (not evidence or not proposal.get("acceptance") or proposal["acceptance"]["deliveryHash"] != evidence["deliveryHash"]):
                fail(503, "corrupt_acceptance")
            if proposal["status"] == "completed":
                acceptance = proposal["acceptance"]
                exact(acceptance, {"ownerId", "at", "deliveryHash", "assessment"}, {"ownerId", "at", "deliveryHash", "assessment"})
                text(acceptance["assessment"])
                integer(acceptance["at"], evidence["at"], 10**15)
                if acceptance["ownerId"] != approved["ownerId"]:
                    fail(503, "corrupt_acceptance")
                retained_used[proposal["terms"]["resourceId"]] += proposal["terms"]["units"]
                for sid in participants:
                    retained_grant_used[sid] += proposal["terms"]["units"]
            integer(proposal["version"], 1, 10**12)
            integer(proposal["reservedUnits"], 0, 10**6)
            units = proposal["reservedUnits"]
            if units:
                integer(proposal["reservationExpiresAt"], proposal["createdAt"] + 1, proposal["expiresAt"])
                if proposal["status"] not in ("agreed", "delivered", "disputed") or units != proposal["terms"]["units"]:
                    fail(503, "corrupt_accounting")
                rid = proposal["terms"]["resourceId"]
                reserved[rid] += units
                for sid in (proposal["requesterId"], proposal["providerId"]):
                    grant_reserved[sid] += units
            elif proposal["status"] in ("agreed", "delivered", "disputed"):
                # Disputes before reservation are valid but carry no commitment.
                if proposal.get("approved"):
                    fail(503, "corrupt_accounting")
        if any(state["resources"][r]["reserved"] != n for r, n in reserved.items()):
            fail(503, "corrupt_accounting")
        if any(state["grants"][g]["reserved"] != n for g, n in grant_reserved.items()):
            fail(503, "corrupt_accounting")
        if any(state["resources"][r]["used"] < n for r, n in retained_used.items()):
            fail(503, "corrupt_accounting")
        if any(state["grants"][g]["used"] < n for g, n in retained_grant_used.items()):
            fail(503, "corrupt_accounting")
    except ControlError as error:
        if error.status == 503:
            raise
        fail(503, "corrupt_state")
    except (KeyError, TypeError, ValueError, OverflowError):
        fail(503, "corrupt_state")


def _limits(value):
    exact(value, {"retentionMs", "reservationMs", "maxRecords"},
          {"retentionMs", "reservationMs", "maxRecords"})
    integer(value["retentionMs"], 3600000, 30 * 86400000)
    integer(value["reservationMs"], 60000, min(value["retentionMs"], 7 * 86400000))
    integer(value["maxRecords"], 1, 100)
    return deepcopy(value)


def _principal(state, principal, scope):
    if not isinstance(principal, dict):
        fail(401, "authentication_required")
    # The native host creates this object after fresh credential validation.
    if principal.get("tenantId") != state["tenantId"]:
        fail(403, "tenant_mismatch")
    identifier(principal.get("subjectId"))
    if principal.get("role") not in ("owner", "agent"):
        fail(403, "invalid_role")
    scopes = principal.get("scopes")
    if not isinstance(scopes, list) or scope not in scopes:
        fail(403, "scope_required")
    return principal


def _owner(principal):
    if principal["role"] != "owner" or "exchange:admin" not in principal["scopes"]:
        fail(403, "owner_required")


def _grant(state, subject_id, resource_id=None):
    grant = state["grants"].get(subject_id)
    if not grant or grant["revoked"] or grant["maxUnits"] <= 0:
        fail(403, "grant_required")
    if resource_id is not None and resource_id not in grant["resources"]:
        fail(403, "resource_not_granted")
    return grant


def _agent(state, principal, resource_id=None):
    if principal["role"] != "agent":
        fail(403, "agent_required")
    if state["stopped"]:
        fail(503, "exchange_stopped")
    return _grant(state, principal["subjectId"], resource_id)


def _record(state, proposal_id, principal):
    identifier(proposal_id)
    proposal = state["records"].get(proposal_id)
    if proposal is None:
        fail(404, "proposal_not_found")
    if principal["role"] != "owner":
        _agent(state, principal, proposal["terms"]["resourceId"])
        if principal["subjectId"] not in (proposal["requesterId"], proposal["providerId"]):
            fail(404, "proposal_not_found")
    return proposal


def _terms(value, state):
    exact(value, {"description", "deliverable", "acceptance", "cancellation", "resourceId", "units"},
          {"description", "deliverable", "acceptance", "cancellation", "resourceId", "units"})
    result = {k: text(value[k]) for k in ("description", "deliverable", "acceptance", "cancellation")}
    result["resourceId"] = identifier(value["resourceId"])
    if result["resourceId"] not in state["resources"]:
        fail(403, "resource_not_configured")
    result["units"] = integer(value["units"], 1, 10**6)
    return result


def _terms_hash(proposal):
    return digest({"domain": "lumen-house-exchange/noncash/v1",
                   "id": proposal["id"], "tenantId": proposal["tenantId"],
                   "requesterId": proposal["requesterId"], "providerId": proposal["providerId"],
                   "termsRevision": proposal["termsRevision"],
                   "policyRevision": proposal["policyRevision"], "terms": proposal["terms"],
                   "expiresAt": proposal["expiresAt"], "disclosure": proposal["disclosure"]})


def _disclosure(retention_ms, reservation_ms):
    return {"version": "exchange-noncash-v1", "purpose": "bounded negotiation and delivery evidence",
            "audience": "owner and the two independently authenticated participants",
            "rights": "no resale or model training rights", "noExecution": True,
            "retentionMs": retention_ms, "reservationMs": reservation_ms,
            "disputeHold": "Unresolved delivered or disputed obligations retain evidence and reserved capacity until owner resolution."}


def _event(proposal, action, principal, now, safety=False, **details):
    if len(proposal["events"]) >= MAX_EVENTS:
        if not safety:
            fail(409, "audit_capacity_reached")
        proposal["events"].pop(0)
        proposal["auditDropped"] = proposal.get("auditDropped", 0) + 1
    event = {"sequence": proposal["events"][-1]["sequence"] + 1 if proposal["events"] else 1, "action": action,
             "actorId": principal["subjectId"], "at": now,
             "version": proposal["version"], "termsHash": proposal["termsHash"], **details}
    event["previousHash"] = proposal["events"][-1]["hash"] if proposal["events"] else None
    event["hash"] = digest(event)
    proposal["events"].append(event)


def _admin_event(state, action, principal, now, **details):
    # Bounded operational audit contains no terms, evidence or credential values.
    audit = state["administrativeAudit"]
    audit.append({"action": action, "actorId": principal["subjectId"], "at": now,
                  "policyRevision": state["policyRevision"], **details})
    del audit[:-256]


def _release(state, proposal):
    units = proposal["reservedUnits"]
    if units:
        state["resources"][proposal["terms"]["resourceId"]]["reserved"] -= units
        for sid in (proposal["requesterId"], proposal["providerId"]):
            state["grants"][sid]["reserved"] -= units
        proposal["reservedUnits"] = 0


def _revoke_active(state, now, principal, subject_id=None):
    for proposal in state["records"].values():
        if proposal["status"] in ACTIVE and (subject_id is None or subject_id in (proposal["requesterId"], proposal["providerId"])):
            if proposal.get("authorityRevoked"):
                continue
            if proposal["status"] in ("delivered", "disputed") and proposal["reservedUnits"]:
                proposal["status"] = "disputed"
                proposal["authorityRevoked"] = True
            else:
                _release(state, proposal)
                proposal["status"] = "revoked"
                proposal["assents"] = {}
            proposal["version"] += 1
            _event(proposal, "revoke", principal, now, safety=True)


def _purge(state, now):
    deleted = []
    maintenance = {"subjectId": "system:maintenance"}
    for pid, proposal in list(state["records"].items()):
        if proposal["reservedUnits"] and now >= proposal["reservationExpiresAt"] and not proposal.get("reservationExpired"):
            proposal["status"] = "disputed"
            proposal["version"] += 1
            proposal["reservationExpired"] = True
            _event(proposal, "reservation-expired", maintenance, now, safety=True)
        if now >= proposal["expiresAt"] and proposal["reservedUnits"]:
            if not proposal.get("retentionHold"):
                proposal["retentionHold"] = True
                proposal["version"] += 1
                _event(proposal, "retention-hold", maintenance, now, safety=True)
        elif now >= proposal["expiresAt"]:
            _release(state, proposal)
            del state["records"][pid]
            deleted.append(pid)
    for request in state["requests"]:
        if request["proposalId"] in deleted:
            request["result"] = None
            request["resultHash"] = digest(None)
    state["requests"] = [r for r in state["requests"] if now < r["expiresAt"]]
    if state["limits"] is not None:
        state["administrativeAudit"] = [e for e in state["administrativeAudit"] if now - e["at"] < state["limits"]["retentionMs"]]
    return len(deleted)


def _require_version(proposal, command):
    if command.get("expectedVersion") != proposal["version"] or type(command.get("expectedVersion")) is not int:
        fail(409, "version_changed")


def _bind_terms(proposal, value):
    if type(value.get("termsRevision")) is not int or value.get("termsRevision") != proposal["termsRevision"] or value.get("termsHash") != proposal["termsHash"]:
        fail(409, "terms_changed")


def _configure(state, principal, value, now):
    exact(value, {"limits", "resources", "grants"}, {"limits", "resources", "grants"})
    limits = _limits(value["limits"])
    if any(p["status"] in ("delivered", "disputed") and p["reservedUnits"] for p in state["records"].values()):
        fail(409, "unresolved_obligation")
    if limits["maxRecords"] < len(state["records"]):
        fail(409, "record_capacity_below_history")
    if not isinstance(value["resources"], dict) or not 1 <= len(value["resources"]) <= 20:
        fail(400, "invalid_resources")
    resources = {}
    for rid, capacity in value["resources"].items():
        identifier(rid)
        integer(capacity, 0, 10**6)
        used = state["resources"].get(rid, {}).get("used", 0)
        if capacity < used:
            fail(409, "capacity_below_consumed")
        resources[rid] = {"capacity": capacity, "reserved": 0, "used": used}
    if any(rid not in resources and r["used"] for rid, r in state["resources"].items()):
        fail(409, "consumed_resource_required")
    if not isinstance(value["grants"], list) or len(value["grants"]) > 100:
        fail(400, "invalid_grants")
    grants = {}
    for raw in value["grants"]:
        exact(raw, {"subjectId", "resources", "maxUnits"}, {"subjectId", "resources", "maxUnits"})
        sid = identifier(raw["subjectId"])
        if sid in grants or not isinstance(raw["resources"], list) or not 1 <= len(raw["resources"]) <= 20:
            fail(400, "invalid_grants")
        if len(set(raw["resources"])) != len(raw["resources"]) or any(r not in resources for r in raw["resources"]):
            fail(400, "invalid_grants")
        maximum = integer(raw["maxUnits"], 0, 10**6)
        used = state["grants"].get(sid, {}).get("used", 0)
        if maximum < used:
            fail(409, "grant_below_consumed")
        grants[sid] = {"resources": list(raw["resources"]), "maxUnits": maximum,
                       "reserved": 0, "used": used, "revoked": False}
    if any(sid not in grants and g["used"] for sid, g in state["grants"].items()):
        fail(409, "consumed_grant_required")
    _revoke_active(state, now, principal)
    state["resources"] = resources
    state["grants"] = grants
    state["limits"] = limits
    state["policyRevision"] += 1
    state["stopped"] = True
    _admin_event(state, "configure", principal, now, policyHash=digest({"limits": limits, "resources": resources, "grants": grants}))
    return _policy_view(state)


def _policy_view(state):
    return {k: deepcopy(state[k]) for k in ("schemaVersion", "tenantId", "stopped", "policyRevision", "limits", "resources", "grants")}


def _execute(state, principal, command, now, proposal_id):
    action, value = command["action"], command.get("input", {})
    if action in ("configure", "open", "stop", "revoke"):
        _owner(principal)
        if command.get("id") is not None or command.get("expectedVersion") is not None:
            fail(400, "unexpected_proposal")
        if action == "configure":
            return _configure(state, principal, value, now), None
        if action == "open":
            exact(value, {})
            if state["limits"] is None or not state["resources"] or not state["grants"]:
                fail(409, "configuration_required")
            state["stopped"] = False
        elif action == "stop":
            exact(value, {"reason"}, {"reason"})
            text(value["reason"], 300)
            state["stopped"] = True
            _revoke_active(state, now, principal)
        elif action == "revoke":
            exact(value, {"subjectId", "reason"}, {"subjectId", "reason"})
            sid = identifier(value["subjectId"])
            text(value["reason"], 300)
            grant = state["grants"].get(sid)
            if grant is None:
                fail(404, "grant_not_found")
            grant["revoked"] = True
            _revoke_active(state, now, principal, sid)
        details = {}
        if action in ("stop", "revoke"):
            details["reason"] = text(value["reason"], 300)
        if action == "revoke":
            details["subjectId"] = value["subjectId"]
        _admin_event(state, action, principal, now, **details)
        return _policy_view(state), None
    if action == "propose":
        exact(value, {"providerId", "terms", "sharingConsent"}, {"providerId", "terms", "sharingConsent"})
        if command.get("id") is not None or command.get("expectedVersion") is not None:
            fail(400, "unexpected_proposal")
        if value["sharingConsent"] is not True:
            fail(400, "sharing_consent_required")
        _agent(state, principal)
        if len(state["records"]) >= state["limits"]["maxRecords"]:
            fail(409, "record_capacity_reached")
        terms = _terms(value["terms"], state)
        _grant(state, principal["subjectId"], terms["resourceId"])
        provider = identifier(value["providerId"])
        if provider == principal["subjectId"]:
            fail(403, "independent_parties_required")
        _grant(state, provider, terms["resourceId"])
        identifier(proposal_id)
        if proposal_id in state["records"]:
            fail(409, "proposal_id_collision")
        proposal = {"id": proposal_id, "tenantId": state["tenantId"], "kind": "noncash",
                    "requesterId": principal["subjectId"], "providerId": provider,
                    "status": "proposed", "version": 1, "termsRevision": 1,
                    "policyRevision": state["policyRevision"], "terms": terms,
                    "expiresAt": now + state["limits"]["retentionMs"],
                    "createdAt": now, "assents": {}, "reservedUnits": 0,
                    "reservationExpiresAt": None, "approved": None, "evidence": None,
                    "acceptance": None, "events": []}
        proposal["disclosure"] = _disclosure(state["limits"]["retentionMs"], state["limits"]["reservationMs"])
        proposal["termsHash"] = _terms_hash(proposal)
        _event(proposal, action, principal, now)
        state["records"][proposal_id] = proposal
        return deepcopy(proposal), proposal_id
    proposal = _record(state, command.get("id"), principal)
    _require_version(proposal, command)
    pid = proposal["id"]
    if action == "delete":
        _owner(principal)
        exact(value, {"reason"}, {"reason"})
        text(value["reason"])
        if proposal["status"] not in TERMINAL:
            fail(409, "terminal_record_required")
        del state["records"][pid]
        for request in state["requests"]:
            if request["proposalId"] == pid:
                request["result"] = None
                request["resultHash"] = digest(None)
        return {"id": pid, "deleted": True}, pid
    if state["stopped"] and action not in ("cancel", "dispute", "resolve"):
        fail(503, "exchange_stopped")
    if proposal["status"] in TERMINAL:
        fail(409, "terminal_proposal")
    if action not in ("cancel", "dispute", "resolve"):
        _grant(state, proposal["requesterId"], proposal["terms"]["resourceId"])
        _grant(state, proposal["providerId"], proposal["terms"]["resourceId"])
    if action == "revise":
        exact(value, {"terms"}, {"terms"})
        if proposal["status"] != "proposed" or principal["role"] != "agent":
            fail(409, "invalid_transition")
        terms = _terms(value["terms"], state)
        for sid in (proposal["requesterId"], proposal["providerId"]):
            _grant(state, sid, terms["resourceId"])
        proposal["terms"] = terms
        proposal["termsRevision"] += 1
        proposal["assents"] = {}
        proposal["termsHash"] = _terms_hash(proposal)
    elif action == "assent":
        exact(value, {"termsRevision", "termsHash", "sharingConsent"}, {"termsRevision", "termsHash", "sharingConsent"})
        if value["sharingConsent"] is not True:
            fail(400, "sharing_consent_required")
        if proposal["status"] != "proposed" or principal["role"] != "agent":
            fail(403, "independent_agent_assent_required")
        _bind_terms(proposal, value)
        sid = principal["subjectId"]
        if sid in proposal["assents"]:
            fail(409, "already_assented")
        proposal["assents"][sid] = {"subjectId": sid, "termsHash": proposal["termsHash"],
                                   "termsRevision": proposal["termsRevision"], "at": now, "sharingConsent": True}
    elif action == "approve":
        _owner(principal)
        exact(value, {"termsRevision", "termsHash"}, {"termsRevision", "termsHash"})
        _bind_terms(proposal, value)
        if proposal["status"] != "proposed" or set(proposal["assents"]) != {proposal["requesterId"], proposal["providerId"]}:
            fail(409, "independent_assents_required")
        units, rid = proposal["terms"]["units"], proposal["terms"]["resourceId"]
        resource = state["resources"][rid]
        if resource["reserved"] + resource["used"] + units > resource["capacity"]:
            fail(409, "resource_capacity_exceeded")
        for sid in (proposal["requesterId"], proposal["providerId"]):
            grant = _grant(state, sid, rid)
            if grant["reserved"] + grant["used"] + units > grant["maxUnits"]:
                fail(409, "grant_capacity_exceeded")
        resource["reserved"] += units
        for sid in (proposal["requesterId"], proposal["providerId"]):
            state["grants"][sid]["reserved"] += units
        proposal["reservedUnits"] = units
        proposal["reservationExpiresAt"] = min(proposal["expiresAt"], now + state["limits"]["reservationMs"])
        proposal["approved"] = {"ownerId": principal["subjectId"], "at": now, "termsHash": proposal["termsHash"]}
        proposal["status"] = "agreed"
    elif action == "deliver":
        exact(value, {"evidence"}, {"evidence"})
        if principal["role"] != "agent" or principal["subjectId"] != proposal["providerId"]:
            fail(403, "provider_required")
        if proposal["status"] != "agreed":
            fail(409, "invalid_transition")
        evidence = value["evidence"]
        exact(evidence, {"sha256", "description"}, {"sha256", "description"})
        evidence = {"sha256": check_hash(evidence["sha256"]), "description": text(evidence["description"])}
        evidence.update({"deliveryRevision": 1, "providerId": principal["subjectId"], "at": now,
                         "termsHash": proposal["termsHash"]})
        evidence["deliveryHash"] = digest({"domain": "lumen-house-exchange/delivery/v1", "id": pid, **evidence})
        proposal["evidence"] = evidence
        proposal["status"] = "delivered"
    elif action == "accept":
        _owner(principal)
        exact(value, {"deliveryRevision", "deliveryHash", "assessment"}, {"deliveryRevision", "deliveryHash", "assessment"})
        evidence = proposal["evidence"]
        if proposal["status"] != "delivered" or not evidence:
            fail(409, "invalid_transition")
        if type(value["deliveryRevision"]) is not int or value["deliveryRevision"] != evidence["deliveryRevision"] or value["deliveryHash"] != evidence["deliveryHash"]:
            fail(409, "delivery_changed")
        proposal["acceptance"] = {"ownerId": principal["subjectId"], "at": now,
                                  "deliveryHash": evidence["deliveryHash"], "assessment": text(value["assessment"])}
        units = proposal["reservedUnits"]
        _release(state, proposal)
        state["resources"][proposal["terms"]["resourceId"]]["used"] += units
        for sid in (proposal["requesterId"], proposal["providerId"]):
            state["grants"][sid]["used"] += units
        proposal["status"] = "completed"
    elif action in ("cancel", "dispute"):
        exact(value, {"reason"}, {"reason"})
        reason = text(value["reason"], 300)
        if proposal["status"] == "disputed":
            fail(409, "already_disputed")
        if action == "dispute" or proposal["status"] in ("delivered", "disputed"):
            proposal["status"] = "disputed"
            proposal["dispute"] = {"openedBy": principal["subjectId"], "at": now, "reason": reason}
        else:
            _release(state, proposal)
            proposal["status"] = "cancelled"
            proposal["cancellation"] = {"by": principal["subjectId"], "at": now, "reason": reason}
    elif action == "resolve":
        _owner(principal)
        exact(value, {"outcome", "reason"}, {"outcome", "reason"})
        text(value["reason"], 300)
        if value["outcome"] != "cancel" or proposal["status"] != "disputed":
            fail(409, "invalid_resolution")
        _release(state, proposal)
        proposal["status"] = "cancelled"
        proposal["resolution"] = {"ownerId": principal["subjectId"], "at": now, "outcome": "cancel", "reason": text(value["reason"], 300)}
    else:
        fail(400, "unknown_action")
    proposal["version"] += 1
    _event(proposal, action, principal, now, safety=action in ("cancel", "dispute", "resolve"))
    return deepcopy(proposal), pid


def apply_operation(before, operation, *, now=None, id=None):
    """Return detached next state/result; exceptions leave ``before`` unchanged.

    Fresh native identity validation happens outside this function on *every*
    request, inside the same native repository transaction. The transaction must
    include this reducer, content, retries and audit. Purge is a trusted host
    maintenance operation; never expose it as an unauthenticated HTTP action.
    """
    _validate_state(before)
    exact(operation, {"kind", "principal", "command", "key", "id"}, {"kind"})
    now = int(time.time() * 1000) if now is None else now
    if type(now) is not int or now < 0:
        fail(400, "invalid_time")
    state = deepcopy(before)
    purged = _purge(state, now)
    kind = operation["kind"]
    if kind == "purge":
        return {"state": state, "result": {"purged": purged}}
    if kind in ("get", "list", "policy"):
        principal = _principal(state, operation.get("principal"), "exchange:read")
        if principal["role"] == "owner":
            _owner(principal)
        else:
            _agent(state, principal)
        if kind == "policy":
            _owner(principal)
            result = _policy_view(state)
        elif kind == "get":
            result = deepcopy(_record(state, operation.get("id"), principal))
        else:
            result = [deepcopy(p) for p in state["records"].values()
                      if principal["role"] == "owner" or
                      (principal["subjectId"] in (p["requesterId"], p["providerId"]) and
                       p["terms"]["resourceId"] in state["grants"][principal["subjectId"]]["resources"])]
        return {"state": state, "result": result}
    if kind != "command":
        fail(400, "unknown_operation")
    principal = _principal(state, operation.get("principal"), "exchange:write")
    if principal["role"] == "owner":
        _owner(principal)
    command = operation.get("command")
    exact(command, {"action", "id", "expectedVersion", "input"}, {"action"})
    if not isinstance(command["action"], str):
        fail(400, "unknown_action")
    if command["action"] in ("configure", "open", "stop", "revoke", "approve", "accept", "resolve", "delete"):
        _owner(principal)
    key = operation.get("key")
    if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key):
        fail(400, "idempotency_key_required")
    # Check authorization before cached data. A retry never revives revoked access.
    if principal["role"] == "agent":
        _agent(state, principal)
    fingerprint = digest({"tenantId": state["tenantId"], "subjectId": principal["subjectId"],
                          "role": principal["role"], "command": command})
    key_hash = digest({"tenantId": state["tenantId"], "subjectId": principal["subjectId"], "key": key})
    replay = next((r for r in state["requests"] if r["keyHash"] == key_hash), None)
    if replay:
        if replay["fingerprint"] != fingerprint:
            fail(409, "idempotency_conflict")
        if command["action"] == "stop" and (replay["result"] is None or not state["stopped"] or replay["policyRevision"] != state["policyRevision"]):
            # Compaction or offline recovery may erase response bodies. A
            # freshly authenticated owner's exact safety-stop retry still acts.
            state["requests"].remove(replay)
            replay = None
    if replay:
        if replay["result"] is None:
            fail(410, "record_erased")
        if replay["policyRevision"] != state["policyRevision"] and command["action"] != "stop":
            fail(409, "retry_policy_changed")
        if command["action"] in ("open", "approve", "deliver", "assent", "revise", "accept") and state["stopped"]:
            fail(503, "exchange_stopped")
        if replay["proposalId"] is not None:
            current = _record(state, replay["proposalId"], principal)
        result = deepcopy(replay["result"])
        if replay["proposalId"] is not None and result.get("version") != current["version"]:
            result["replay"] = {"historicalResponse": True, "currentVersion": current["version"], "currentStatus": current["status"]}
        return {"state": state, "result": result}
    if command.get("id") is not None:
        _record(state, command["id"], principal)
    if len(state["requests"]) >= MAX_REQUESTS and command["action"] not in ("stop", "revoke", "delete", "cancel", "dispute", "resolve"):
        fail(409, "request_capacity_reached")
    result, pid = _execute(state, principal, command, now, id or str(uuid.uuid4()))
    safety = command["action"] in ("stop", "revoke", "delete", "cancel", "dispute", "resolve")
    if command["action"] != "delete":
        if state["limits"] is None or len(state["requests"]) >= MAX_REQUESTS or (safety and len(canonical_json(state).encode("utf-8")) > NORMAL_STATE_BYTES):
            # Safety controls must not become hostage to a filled retry table.
            # They remain naturally idempotent; retrying them may add admin audit.
            pass
        else:
            state["requests"].append({"keyHash": key_hash, "fingerprint": fingerprint,
                                      "proposalId": pid, "result": deepcopy(result),
                                      "resultHash": digest(result),
                                      "expiresAt": now + state["limits"]["retentionMs"],
                                      "policyRevision": state["policyRevision"]})
    _validate_state(state)
    size = len(canonical_json(state).encode("utf-8"))
    if size > NORMAL_STATE_BYTES and not safety:
        fail(409, "state_capacity_reached")
    if size > SAFETY_STATE_BYTES:
        # Preserve financial/resource debt and live content. Sacrifice cached
        # response bodies rather than preventing cancellation or the stop switch.
        for request in state["requests"]:
            request["result"] = None
            request["resultHash"] = digest(None)
        while len(state["administrativeAudit"]) > 1 and len(canonical_json(state).encode("utf-8")) > SAFETY_STATE_BYTES:
            state["administrativeAudit"].pop(0)
        if len(canonical_json(state).encode("utf-8")) > SAFETY_STATE_BYTES:
            fail(503, "state_capacity_unsafe")
    return {"state": state, "result": deepcopy(result)}


def recover_state(before, *, now=None):
    """Trusted offline restore fence; host separately revokes native identities.

    Recovery never silently expires obligations or replays cached permission.
    Reserved negotiations require owner resolution, then fresh independent terms.
    """
    _validate_state(before)
    now = int(time.time() * 1000) if now is None else integer(now, 0, 10**15)
    state = deepcopy(before)
    state["stopped"] = True
    principal = {"subjectId": "system:offline-recovery"}
    for proposal in state["records"].values():
        if proposal["status"] in ACTIVE:
            proposal["assents"] = {}
            proposal["approved"] = None
            proposal["authorityRevoked"] = True
            proposal["status"] = "disputed" if proposal["reservedUnits"] else "revoked"
            proposal["version"] += 1
            _event(proposal, "offline-recovery", principal, now, safety=True)
    for request in state["requests"]:
        request["result"] = None
        request["resultHash"] = digest(None)
    _admin_event(state, "offline-recovery", principal, now)
    _validate_state(state)
    return state
