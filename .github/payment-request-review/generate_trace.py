"""Generate fictional request-only snapshots from the actual payment reducer.

Run with Python 3.11+ from any directory. The output is ignored local review
material. This program opens no native store, credential, provider or wallet.
It makes no network requests and cannot prepare a real payment request.
"""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
DIRECTORY = Path(__file__).resolve().parent
ROOT = DIRECTORY.parents[1]
spec = importlib.util.spec_from_file_location(
    "synthetic_payment_review_core", ROOT / "exchange/payment_requests/core.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)

OWNER = {"tenantId": "synthetic-tenant", "subjectId": "synthetic-owner", "role": "owner",
         "scopes": ["payments:read", "payments:prepare", "payments:control"]}
STEPS = []
CHECKS = []


def ms(value):
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    CHECKS.append({"name": name, "passed": True})


def source(job="synthetic-paid-job-1", amount=37_500, **changes):
    """Fictional accepted paid record; never reads or impersonates native work."""
    return {"tenantId": "synthetic-tenant", "jobId": job, "ownerId": "synthetic-owner",
            "workerId": "synthetic-worker", "payee": "0x" + "1" * 40,
            "termsHash": core.digest({"synthetic": True, "job": job, "binding": "terms"}),
            "termsVersion": 1,
            "termsDocumentHash": core.digest({"synthetic": True, "job": job, "binding": "terms-document"}),
            "evidenceId": "synthetic-evidence-" + job,
            "evidenceHash": core.digest({"synthetic": True, "job": job, "binding": "recorded-evidence"}),
            "evidenceSize": 512, "recordEvidenceClass": "synthetic",
            "artifactCheck": {"status": "unavailable", "reason": "artifact_bytes_not_retained"},
            "acceptanceId": "synthetic-acceptance-" + job,
            "acceptanceHash": core.digest({"synthetic": True, "job": job, "binding": "owner-acceptance"}),
            "amountAtomic": amount, "currency": "USDC", "network": "base", "asset": "usdc", "decimals": 6,
            "sourceKind": "native-paid-fulfillment", "status": "accepted",
            "payeeVerification": {"basis": "owner_attestation",
                                  "bindingHash": core.digest({"synthetic": True, "binding": "owner-payee-attestation"}),
                                  "independentProof": False}, **changes}


class Fixture:
    def __init__(self, now="2026-10-25T12:00:00Z", scenario="Main synthetic request"):
        self.state = core.empty_state("synthetic-tenant")
        self.now, self.sequence, self.scenario = ms(now), 0, scenario

    def operation(self, operation):
        self.sequence += 1
        output = core.apply_operation(self.state, operation, now=self.now,
                                      id=f"synthetic-request-{self.sequence}")
        self.state = output["state"]
        return output["result"]

    def command(self, action, value=None, *, request_id=None, snapshot=None, key=None):
        command = {"action": action, "input": {} if value is None else value}
        if request_id is not None:
            command["id"] = request_id
        operation = {"kind": "command", "principal": OWNER, "command": command}
        if snapshot is not None:
            operation["source"] = snapshot
        if key is not None:
            operation["key"] = key
        return self.operation(operation)

    def prepare(self, snapshot=None, key="synthetic-prepare-1"):
        snapshot = source() if snapshot is None else snapshot
        return self.command("prepare", {"jobId": snapshot["jobId"]}, snapshot=snapshot, key=key)

    def policy(self):
        return self.operation({"kind": "policy", "principal": OWNER})

    def record(self, title, result=None, description=None, request_id=None):
        policy = self.policy()
        STEPS.append({"label": title, "actor": "synthetic owner", "scenario": self.scenario,
                      "synthetic": True, "fixtureTimeUtc": datetime.fromtimestamp(
                          self.now / 1000, tz=timezone.utc).isoformat(),
                      "state": deepcopy(self.state), "result": deepcopy(result),
                      "policy": deepcopy(policy), "description": description,
                      **({"requestId": request_id} if request_id else {})})
        return policy

    def rejected(self, status, code, run):
        before = deepcopy(self.state)
        try:
            run()
        except core.ControlError as error:
            check(f"Rejected {code}; no partial state or principal hold",
                  (error.status, error.code) == (status, code) and self.state == before)
            return {"rejected": True, "status": error.status, "code": error.code}
        raise AssertionError(f"Expected {code}")

    def open_synthetic(self):
        self.command("configure", {"approvalReference": "SYNTHETIC REVIEW ONLY — no production policy adoption"})
        return self.command("open")


def build_trace():
    STEPS.clear()
    CHECKS.clear()
    fixture = Fixture()
    policy = fixture.record("Default: preparation stopped", description="Synthetic fixture starts closed, with no owner approval. Real production preparation remains disabled.")
    check("Defaults are stopped; no policy adoption or execution authority", fixture.state["stopped"] and fixture.state["approval"] is None and not policy["executionEnabled"])
    result = fixture.rejected(503, "payment_requests_stopped", fixture.prepare)
    fixture.record("Unapproved preparation rejected", result, "A fictional paid job cannot bypass the default stop switch. No request or hold was created.")
    result = fixture.command("configure", {"approvalReference": "SYNTHETIC REVIEW ONLY — no production policy adoption"})
    fixture.record("Synthetic ceiling approval recorded", result, "Fixture owner records the fixed principal ceilings. Configuration leaves preparation stopped; it grants no wallet or execution authority.")
    check("Ceiling approval alone does not open request preparation", fixture.state["stopped"] is True)
    result = fixture.command("open")
    fixture.record("Synthetic preparation opened", result, "Only this isolated in-memory scenario opens request preparation. Production policy and routes remain disabled.")
    request = fixture.prepare()
    policy = fixture.record("Exact 0.0375 USDC request prepared", request, "The source amount is 0.0375 USDC, below the 0.10 per-job ceiling. It is not rounded up or automatically awarded. Wallet confirmation remains unavailable.")
    check("Exact accepted paid amount is held, rather than the full per-job ceiling", request["amountAtomic"] == request["principalHoldAtomic"] == 37_500)
    check("Request digest binds source, principal, policy, and origin day", request["requestHash"] == core._request_hash(request))
    check("Wallet execution and handoff stay disabled; fees remain unknown and unapproved", request["executionEnabled"] is False and request["handoffAvailable"] is False and request["fees"] == {"status": "unknown", "amountAtomic": None, "approved": False})
    check("Payee proof and artifact-byte checks are honestly unavailable", request["source"]["payeeVerification"]["independentProof"] is False and request["source"]["artifactCheck"]["reason"] == "artifact_bytes_not_retained")
    result = fixture.prepare()
    fixture.record("Same-key retry: one request", result, "Replaying the same preparation key returns the same request digest. Principal remains held once.")
    check("Same-key retries do not duplicate requests or principal holds", result == request and len(fixture.state["records"]) == 1 and fixture.policy()["exposure"]["heldPrincipalAtomic"] == 37_500)
    result = fixture.prepare(key="synthetic-fresh-key-same-job")
    fixture.record("New key: same earned obligation", result, "A new retry key cannot create a second request for the same native job. The existing earned obligation and its principal hold are retained.")
    check("A new key for the same job cannot create another payable slot", result["id"] == request["id"] and len(fixture.state["records"]) == 1 and fixture.policy()["exposure"]["heldPrincipalAtomic"] == 37_500)
    result = fixture.rejected(409, "obligation_binding_changed", lambda: fixture.prepare(
        source(payee="0x" + "2" * 40), key="synthetic-changed-payee"))
    fixture.record("Changed payee: replacement rejected", result, "Changing the payee cannot replace the earned obligation or free its principal hold. A changed binding requires separate review.", request["id"])
    result = fixture.command("report_outcome", {"outcome": "failed", "reason": "Synthetic owner observation only; no transaction was attempted"}, request_id=request["id"])
    fixture.record("Failed observation retains principal", result, "A fictional failure report is unverified. The earned principal remains held; this record is not proof of a transaction or settlement.")
    check("A failed owner observation does not release principal", result["principalHoldAtomic"] == 37_500 and result["outcomeReport"]["verified"] is False)
    unknown = fixture.prepare(source("synthetic-paid-job-2", 50_000), key="synthetic-prepare-2")
    unknown = fixture.command("report_outcome", {"outcome": "unknown", "reason": "Synthetic unknown-outcome example; no wallet operation"}, request_id=unknown["id"])
    fixture.record("Unknown outcome retains its hold", unknown, "A second fictional paid job holds its exact 0.05 USDC. An unknown observation does not imply failure, success, or permission to pay again.")
    check("Unknown outcomes remain held and distinct earned jobs share exposure", fixture.policy()["exposure"]["heldPrincipalAtomic"] == 87_500 and unknown["outcomeReport"]["verified"] is False)
    origin = request["dayWindow"]
    fixture.now = ms("2026-10-26T07:00:00Z")
    policy = fixture.record("LA midnight: old holds carry forward", unknown, "At the next Los Angeles policy day, unresolved principal from both earlier requests still counts. A new day does not replenish held obligations.")
    check("LA midnight preserves origin-day bindings and all unresolved holds", policy["exposure"]["dayWindow"]["date"] == "2026-10-26" and policy["exposure"]["dailyExposureAtomic"] == 87_500 and fixture.state["records"][request["id"]]["dayWindow"] == origin)
    result = fixture.command("stop", {"reason": "Synthetic review pause; preserve earned obligations"})
    fixture.record("Stop preparation; preserve obligations", result, "Stopping blocks new preparation while preserving existing principal holds and historical request bindings.", request["id"])
    check("Stop preserves every earned principal hold", fixture.state["stopped"] and fixture.policy()["exposure"]["heldPrincipalAtomic"] == 87_500)
    for date, title, expected_hours in (("2026-03-08T12:00:00Z", "Spring DST: 23-hour LA policy day", 23),
                                        ("2026-11-01T12:00:00Z", "Fall DST: 25-hour LA policy day", 25)):
        scenario = Fixture(date, scenario=f"Independent synthetic DST scenario: {expected_hours}-hour policy day")
        scenario.open_synthetic()
        row = scenario.prepare(source(f"synthetic-dst-{expected_hours}", 25_000), key=f"synthetic-dst-{expected_hours}")
        window = scenario.policy()["exposure"]["dayWindow"]
        check(f"LA DST policy interval is exactly {expected_hours} hours", window["endUtcMs"] - window["startUtcMs"] == expected_hours * 3_600_000)
        scenario.record(title, row, f"Independent synthetic scenario. The reducer computes this Los Angeles calendar day's exact {expected_hours}-hour UTC interval, rather than assuming a fixed 24-hour day.")
        row = scenario.command("report_outcome", {"outcome": "wallet_cancelled" if expected_hours == 23 else "unknown",
                                                   "reason": "Synthetic outcome observation only; no wallet or transaction was attempted"}, request_id=row["id"])
        scenario.now = window["endUtcMs"]
        policy = scenario.record(f"{'Spring' if expected_hours == 23 else 'Fall'} DST rollover: hold remains", row,
                                 f"Independent synthetic scenario. After this {expected_hours}-hour LA day ends, the unresolved 0.025 USDC still counts against the next day's exposure. The original day binding remains immutable.")
        check(f"Unresolved hold carries beyond the {expected_hours}-hour DST day", policy["exposure"]["dailyExposureAtomic"] == 25_000 and row["dayWindow"] == window and policy["exposure"]["dayWindow"]["date"] != window["date"])
    recovery = deepcopy(fixture.state)
    fixture.state = core.recover_state(fixture.state, now=fixture.now)
    fixture.record("Offline recovery stays closed", description="Synthetic recovery closes preparation and disables old retry authority. It preserves request records, their digests, and all unresolved principal.", request_id=request["id"])
    check("Recovery preserves requests and holds while fencing preparation", fixture.state["stopped"] and fixture.state["records"] == recovery["records"] and all(item["disabled"] for item in fixture.state["retries"]))
    # Native freshness belongs to the service, not the reducer. These explicitly
    # labeled presentation-only overlays exercise the renderer without claiming
    # that a native service was consulted. State/result stay actual core outputs.
    base_snapshot = deepcopy(STEPS[4])
    annotations = []
    for current in (True, False):
        view = deepcopy(base_snapshot)
        view["label"] = "UI annotation fixture: current source" if current else "UI annotation fixture: historical source held"
        view["actor"] = "synthetic UI annotation"
        view["scenario"] = "Presentation-only service annotation fixture; no native source check"
        view["description"] = (
            "Presentation-only synthetic annotation: sourceCurrent=true. This demonstrates service-result rendering; no native service or current source was checked by this fixture."
            if current else
            "Presentation-only synthetic annotation: sourceCurrent=false and a source hold. The historical request must remain held and must not appear ready. No native service was consulted.")
        view["request"] = {**deepcopy(view["result"]), "sourceCurrent": current}
        if not current:
            view["request"]["sourceHold"] = "current_source_requires_owner_reconciliation"
        view["annotationFixture"] = {"synthetic": True, "presentationOnly": True,
                                     "nativeServiceConsulted": False}
        annotations.append(view)
        STEPS.append(view)
    check("Presentation-only freshness annotations preserve core state and request digest", all(
        view["state"] == base_snapshot["state"] and view["result"] == base_snapshot["result"]
        and view["request"]["requestHash"] == base_snapshot["result"]["requestHash"]
        and all("sourceCurrent" not in row and "sourceHold" not in row for row in view["state"]["records"].values())
        for view in annotations))
    return {"synthetic": True, "generatedBy": "actual payment request reducer snapshots plus explicitly labeled presentation-only freshness annotations",
            "coreSourceSha256": sha256((ROOT / "exchange/payment_requests/core.py").read_bytes()).hexdigest(),
            "steps": STEPS, "checks": CHECKS,
            "limitations": [
                "These are fictional in-memory reducer snapshots. No native store, live account, credential, wallet, or production policy is opened or changed.",
                "Core snapshots retain recorded bindings; they do not check current native-source freshness. The two explicitly labeled freshness-annotation views are presentation-only fixtures, not native verification.",
                "Artifact bytes are unavailable. Payee wallet ownership is owner-attested and not independently verified.",
                "The native host has no verified mobile wallet handoff. The owner's final cryptographic confirmation, canonical chain/token validation, and fee disclosure remain unimplemented launch gates.",
                "Daily and lifetime principal ceilings are separate from fees and wallet balances. Failed, unknown, cancelled-wallet, stopped, and recovered requests keep earned principal held.",
                "Local rendering and reducer checks do not establish current deployed source, production readiness, settlement, or permission to sign, send, fund, grant, or deploy."]}


if __name__ == "__main__":
    trace = build_trace()
    destination = DIRECTORY / "trace.json"
    destination.write_text(json.dumps(trace, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"output": "trace.json", "synthetic": True, "steps": len(trace["steps"]),
                      "checks": len(trace["checks"]), "result": "pass"}))
