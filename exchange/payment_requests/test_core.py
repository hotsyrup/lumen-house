"""Synthetic request-preparation tests. No wallet, provider or settlement calls."""
from copy import deepcopy
from datetime import datetime
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("payment_request_core", Path(__file__).with_name("core.py"))
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


def ms(value):
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


NOW = ms("2026-10-25T12:00:00Z")
OWNER = {"tenantId": "tenant-a", "subjectId": "owner-a", "role": "owner",
         "scopes": ["payments:read", "payments:prepare", "payments:control"]}


def source(job="paid-job-1", amount=100_000, **changes):
    return {"tenantId": "tenant-a", "jobId": job, "ownerId": "owner-a", "workerId": "worker-a",
            "payee": "0x" + "1" * 40, "termsHash": "a" * 64, "termsVersion": 1,
            "termsDocumentHash": "b" * 64, "evidenceId": "evidence-1", "evidenceHash": "c" * 64,
            "evidenceSize": 512, "acceptanceId": "acceptance-1", "acceptanceHash": "d" * 64,
            "amountAtomic": amount, "currency": "USDC", "network": "base", "asset": "usdc", "decimals": 6,
            "sourceKind": "native-paid-fulfillment", "status": "accepted", "recordEvidenceClass": "synthetic",
            "artifactCheck": {"status": "unavailable", "reason": "artifact_bytes_not_retained"},
            "payeeVerification": {"basis": "owner_attestation", "bindingHash": "e" * 64, "independentProof": False}, **changes}


class Harness:
    def __init__(self, configured=True):
        self.state, self.now, self.seq = core.empty_state("tenant-a"), NOW, 0
        if configured:
            self.command("configure", {"approvalReference": "Synthetic approved ceiling fixture"})
            self.command("open")

    def operation(self, op):
        self.seq += 1
        result = core.apply_operation(self.state, op, now=self.now, id=f"request-{self.seq}")
        self.state = result["state"]
        return result["result"]

    def command(self, action, value=None, *, request_id=None, principal=None, snapshot=None, key=None):
        command = {"action": action, "input": value or {}}
        if request_id is not None:
            command["id"] = request_id
        op = {"kind": "command", "principal": principal or OWNER, "command": command}
        if snapshot is not None:
            op["source"] = snapshot
        if key is not None:
            op["key"] = key
        return self.operation(op)

    def prepare(self, job="paid-job-1", amount=100_000, *, snapshot=None, key=None):
        snapshot = source(job, amount) if snapshot is None else snapshot
        return self.command("prepare", {"jobId": job}, snapshot=snapshot, key=key or f"prepare-{job}")

    def policy(self):
        return self.operation({"kind": "policy", "principal": OWNER})


class RequestCoreTests(unittest.TestCase):
    def assert_error(self, status, code, run):
        with self.assertRaises(core.ControlError) as caught:
            run()
        self.assertEqual((caught.exception.status, caught.exception.code), (status, code))

    def test_default_off_and_immutable_approved_ceiling_configuration(self):
        h = Harness(False)
        self.assertTrue(h.state["stopped"])
        self.assertIsNone(h.state["approval"])
        self.assert_error(503, "payment_requests_stopped", lambda: h.prepare())
        self.assert_error(409, "approval_required", lambda: h.command("open"))
        for field, value in (("perJobAtomic", 100001), ("perDayAtomic", 1000001),
                             ("lifetimeAtomic", 5000001), ("timezone", "UTC"), ("feeBudgetAtomic", 1)):
            self.assert_error(400, "invalid_fields", lambda: h.command("configure", {"approvalReference": "Owner ref", field: value}))
        h.command("configure", {"approvalReference": "Explicit owner approval fixture"})
        self.assertTrue(h.state["stopped"])
        policy = h.command("open")
        self.assertEqual(policy["ceilings"], {"perJobAtomic": 100000, "perDayAtomic": 1000000, "lifetimeAtomic": 5000000,
                                             "timezone": "America/Los_Angeles", "currency": "USDC", "network": "base", "asset": "usdc", "decimals": 6})
        self.assertFalse(policy["executionEnabled"])
        self.assertFalse(policy["handoffAvailable"])

    def test_native_paid_amount_is_exact_not_a_fixed_reward_and_fees_are_unknown(self):
        h = Harness()
        row = h.prepare(amount=12345)
        self.assertEqual(row["amountAtomic"], 12345)
        self.assertEqual(row["principalHoldAtomic"], 12345)
        self.assertEqual(row["status"], "awaiting_owner_wallet_setup")
        self.assertFalse(row["executionEnabled"])
        self.assertFalse(row["handoffAvailable"])
        self.assertEqual(row["fees"], {"status": "unknown", "amountAtomic": None, "approved": False})
        self.assertFalse(row["source"]["payeeVerification"]["independentProof"])
        self.assertEqual(row["source"]["artifactCheck"]["status"], "unavailable")
        self.assertEqual(h.policy()["exposure"]["heldPrincipalAtomic"], 12345)

    def test_positive_integer_principal_per_job_ceiling_and_asset_tuple(self):
        h = Harness()
        for amount in (0, -1, True, 1.5, "100000", 100001):
            self.assert_error(400, "invalid_integer", lambda: h.prepare(snapshot=source(amount=amount)))
        for changes in ({"currency": "ETH"}, {"asset": "eth"}, {"network": "ethereum"},
                        {"decimals": True}, {"decimals": 18}, {"status": "delivered"},
                        {"sourceKind": "noncash"}, {"sourceKind": "converted-exchange"}):
            self.assert_error(403, "accepted_paid_source_required", lambda: h.prepare(snapshot=source(**changes)))
        self.assertEqual(h.state["records"], {})

    def test_source_and_body_forgery_cannot_supply_accepted_work_or_authority(self):
        h = Harness()
        for field, value in (("ownerConfirmed", True), ("amountAtomic", 1), ("payee", "0x" + "2" * 40),
                             ("source", source()), ("fees", {"approved": True}), ("actor", "owner")):
            self.assert_error(400, "invalid_fields", lambda: h.command("prepare", {"jobId": "paid-job-1", field: value}, snapshot=source(), key="body-forgery"))
        for field in ("accepted", "feeBudget", "executionEnabled", "walletKey", "standingGrant"):
            self.assert_error(400, "invalid_fields", lambda: h.prepare(snapshot={**source(), field: True}))
        for changes in ({"ownerId": "other-owner"}, {"tenantId": "tenant-b"}):
            self.assert_error(403, "source_owner_mismatch", lambda: h.prepare(snapshot=source(**changes)))
        self.assert_error(403, "source_job_mismatch", lambda: h.prepare(snapshot=source(job="different-job")))
        self.assert_error(400, "invalid_fields", lambda: h.command("prepare", {"jobId": "paid-job-1"}, key="missing-source"))

    def test_explicit_native_evidence_and_attestation_metadata_is_bound_honestly(self):
        h = Harness()
        for field in ("termsVersion", "termsDocumentHash", "evidenceId", "evidenceSize", "artifactCheck", "recordEvidenceClass"):
            snapshot = source()
            del snapshot[field]
            self.assert_error(400, "invalid_fields", lambda: h.prepare(snapshot=snapshot))
        for changes in ({"artifactCheck": {"status": "verified", "reason": "owner said so"}},
                        {"recordEvidenceClass": "verified"}):
            self.assert_error(403, "unsupported_artifact_verification", lambda: h.prepare(snapshot=source(**changes)))
        for verification in ({"basis": "personhood", "bindingHash": "e" * 64, "independentProof": False},
                             {"basis": "owner_attestation", "bindingHash": "e" * 64, "independentProof": True}):
            self.assert_error(403, "unsupported_payee_verification", lambda: h.prepare(snapshot=source(payeeVerification=verification)))
        for address in ("0x" + "0" * 40, "0x123", "wallet.eth", "1" * 40):
            self.assert_error(400, "invalid_payee", lambda: h.prepare(snapshot=source(payee=address)))

    def test_request_hash_binds_source_policy_amount_and_origin_day(self):
        h = Harness()
        row = h.prepare()
        self.assertEqual(row["sourceHash"], core.digest(row["source"]))
        self.assertEqual(row["requestHash"], core._request_hash(row))
        for field, value in (("tenantId", "tenant-b"), ("obligationKey", "f" * 64), ("sourceHash", "f" * 64),
                             ("policyHash", "f" * 64), ("amountAtomic", 99999), ("currency", "ETH"),
                             ("network", "ethereum"), ("asset", "eth"), ("decimals", 18),
                             ("createdAt", row["createdAt"] + 1), ("dayWindow", core.day_window(NOW + 86400000))):
            self.assertNotEqual(core._request_hash({**row, field: value}), row["requestHash"])

    def test_stable_obligation_and_idempotency_block_duplicate_or_changed_requests(self):
        h = Harness()
        row = h.prepare(key="one-key")
        before = deepcopy(h.state)
        self.assertEqual(h.prepare(key="one-key"), row)
        self.assertEqual(h.state, before)
        self.assertEqual(h.prepare(key="another-key"), row)
        self.assertEqual(len(h.state["records"]), 1)
        self.assertEqual(h.policy()["exposure"]["heldPrincipalAtomic"], 100000)
        self.assert_error(409, "idempotency_conflict", lambda: h.prepare(job="paid-job-2", key="one-key"))
        self.assert_error(409, "idempotency_conflict", lambda: h.prepare(job="paid-job-2", key="another-key"))
        for changes in ({"workerId": "different-worker"}, {"payee": "0x" + "2" * 40}, {"termsHash": "f" * 64},
                        {"termsVersion": 2}, {"evidenceHash": "f" * 64}, {"acceptanceHash": "f" * 64},
                        {"acceptanceId": "new-acceptance"}, {"amountAtomic": 99999}):
            self.assert_error(409, "obligation_binding_changed", lambda: h.prepare(snapshot=source(**changes), key="new-replacement-key"))
        self.assertEqual(len(h.state["records"]), 1)
        self.assertEqual(h.policy()["exposure"]["heldPrincipalAtomic"], 100000)

    def test_daily_ceiling_includes_all_unresolved_holds_across_la_midnight(self):
        h = Harness()
        h.now = ms("2026-10-26T06:59:59.999Z")
        original = [h.prepare(job=f"job-{n}") for n in range(10)]
        self.assertEqual(h.policy()["exposure"]["dailyExposureAtomic"], 1000000)
        before = deepcopy(h.state)
        self.assert_error(409, "daily_principal_ceiling", lambda: h.prepare(job="eleventh-job"))
        self.assertEqual(h.state, before)
        h.now = ms("2026-10-26T07:00:00Z")
        self.assertEqual(h.policy()["exposure"]["dayWindow"]["date"], "2026-10-26")
        self.assert_error(409, "daily_principal_ceiling", lambda: h.prepare(job="overnight-replacement"))
        self.assertTrue(all(r["dayWindow"]["date"] == "2026-10-25" for r in original))
        self.assertEqual(h.policy()["exposure"]["heldPrincipalAtomic"], 1000000)

    def test_la_day_intervals_cover_actual_23_and_25_hour_dst_days(self):
        spring = core.day_window(ms("2026-03-08T12:00:00Z"))
        fall = core.day_window(ms("2026-11-01T12:00:00Z"))
        self.assertEqual((spring["date"], spring["endUtcMs"] - spring["startUtcMs"]), ("2026-03-08", 23 * 3600000))
        self.assertEqual((fall["date"], fall["endUtcMs"] - fall["startUtcMs"]), ("2026-11-01", 25 * 3600000))
        self.assertEqual(spring["startUtcMs"], ms("2026-03-08T08:00:00Z"))
        self.assertEqual(spring["endUtcMs"], ms("2026-03-09T07:00:00Z"))
        self.assertEqual(fall["startUtcMs"], ms("2026-11-01T07:00:00Z"))
        self.assertEqual(fall["endUtcMs"], ms("2026-11-02T08:00:00Z"))
        for window in (spring, fall):
            self.assertEqual(core.day_window(window["endUtcMs"] - 1), window)
            self.assertNotEqual(core.day_window(window["endUtcMs"])["date"], window["date"])

    def test_lifetime_and_settled_today_are_counted_without_a_settlement_interface(self):
        h = Harness()
        # Read-only accounting fixture representing prior confirmed history.
        # No owner command or source snapshot can create these counters.
        h.state["settledByDay"] = {f"2026-10-0{n}": 1000000 for n in range(1, 5)}
        h.state["settledByDay"]["2026-10-05"] = 950000
        h.state["settledPrincipalAtomic"] = 4950000
        core.validate_state(h.state)
        h.prepare(amount=50000)
        self.assertEqual(h.policy()["exposure"]["lifetimeExposureAtomic"], 5000000)
        self.assert_error(409, "lifetime_principal_ceiling", lambda: h.prepare(job="over-lifetime", amount=1))
        h = Harness()
        h.state["settledByDay"] = {"2026-10-25": 1000000}
        h.state["settledPrincipalAtomic"] = 1000000
        self.assert_error(409, "daily_principal_ceiling", lambda: h.prepare(amount=1))
        for action in ("settle", "reconcile", "release", "delete", "expire", "broadcast", "sign", "grant", "fund"):
            self.assert_error(400, "unsupported_action", lambda: h.command(action))

    def test_failed_unknown_and_cancelled_wallet_keep_hold_and_block_replacement(self):
        for outcome in ("failed", "unknown", "wallet_cancelled"):
            with self.subTest(outcome=outcome):
                h = Harness()
                row = h.prepare()
                row = h.command("report_outcome", {"outcome": outcome, "reason": "Owner observation only"}, request_id=row["id"])
                self.assertEqual(row["principalHoldAtomic"], 100000)
                self.assertEqual(row["status"], "awaiting_owner_wallet_setup")
                self.assertFalse(row["outcomeReport"]["verified"])
                self.assertEqual(h.prepare(key="new-key"), row)
                self.assert_error(409, "obligation_binding_changed", lambda: h.prepare(snapshot=source(payee="0x" + "2" * 40), key="replacement"))
                original = deepcopy(h.state)
                h.command("report_outcome", {"outcome": outcome, "reason": "Repeated observation"}, request_id=row["id"])
                self.assertEqual(h.state, original)
                self.assert_error(400, "unsupported_outcome", lambda: h.command("report_outcome", {"outcome": "confirmed", "reason": "Cannot fabricate settlement"}, request_id=row["id"]))

    def test_stop_and_offline_recovery_never_release_or_expire_earned_principal(self):
        h = Harness()
        row = h.prepare()
        h.command("stop", {"reason": "Pause request preparation"})
        self.assert_error(503, "payment_requests_stopped", lambda: h.prepare(key="new-key"))
        h.now = ms("2027-10-25T12:00:00Z")
        h.command("open")
        self.assertEqual(h.prepare(key="new-key"), row)
        before = deepcopy(h.state)
        restored = core.recover_state(h.state, now=h.now)
        self.assertEqual(h.state, before)
        self.assertTrue(restored["stopped"])
        self.assertEqual(restored["records"], before["records"])
        self.assertEqual(restored["obligations"], before["obligations"])
        self.assertTrue(all(r["disabled"] for r in restored["retries"]))
        h.state = restored
        h.command("open")
        self.assert_error(409, "recovery_reauthorization_required", lambda: h.prepare())
        self.assertEqual(h.prepare(key="fresh-authorized-key"), row)
        self.assertEqual(h.policy()["exposure"]["heldPrincipalAtomic"], 100000)

    def test_current_owner_tenant_scope_and_object_boundaries_precede_replay(self):
        h = Harness()
        row = h.prepare()
        for principal, code in (({**OWNER, "tenantId": "tenant-b"}, "tenant_mismatch"),
                                ({**OWNER, "role": "agent"}, "owner_required"),
                                ({**OWNER, "scopes": []}, "scope_required")):
            self.assert_error(403, code, lambda: h.command("prepare", {"jobId": "paid-job-1"}, principal=principal, snapshot=source(), key="prepare-paid-job-1"))
        self.assert_error(404, "request_not_found", lambda: h.operation({"kind": "get", "principal": {**OWNER, "subjectId": "other-owner"}, "id": row["id"]}))
        self.assert_error(403, "owner_mismatch", lambda: h.command("prepare", {"jobId": "paid-job-1"}, principal={**OWNER, "subjectId": "other-owner"}, snapshot=source(), key="key"))

    def test_corrupt_unknown_state_and_changed_financial_binding_fail_closed(self):
        h = Harness()
        row = h.prepare()
        for mutate, code in (
            (lambda s: s.update(schemaVersion=2), "unsupported_state"),
            (lambda s: s["records"][row["id"]]["source"].update(payee="0x" + "2" * 40), "corrupt_binding"),
            (lambda s: s["records"][row["id"]].update(principalHoldAtomic=0), "corrupt_accounting"),
            (lambda s: s["records"][row["id"]].update(handoffAvailable=True), "corrupt_authority"),
            (lambda s: s["records"][row["id"]].update(requestHash="f" * 64), "corrupt_request_hash"),
            (lambda s: s["audit"][0].update(actorId="forged-owner"), "corrupt_audit"),
            (lambda s: s["obligations"].clear(), "corrupt_accounting"),
        ):
            invalid = deepcopy(h.state)
            mutate(invalid)
            before = deepcopy(invalid)
            self.assert_error(503, code, lambda: core.validate_state(invalid))
            self.assertEqual(invalid, before)

    def test_rejected_mutation_and_returned_copies_cannot_change_accepted_obligation(self):
        h = Harness()
        row = h.prepare()
        before = deepcopy(h.state)
        self.assert_error(409, "obligation_binding_changed", lambda: h.prepare(snapshot=source(workerId="changed-worker"), key="rejected"))
        self.assertEqual(h.state, before)
        row["source"]["payee"] = "0x" + "2" * 40
        row["principalHoldAtomic"] = 0
        saved = h.operation({"kind": "get", "principal": OWNER, "id": row["id"]})
        self.assertEqual(saved["principalHoldAtomic"], 100000)
        self.assertEqual(saved["source"]["payee"], source()["payee"])


if __name__ == "__main__":
    unittest.main()
