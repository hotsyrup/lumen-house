"""Adversarial reducer tests; native transaction/concurrency tests live in host suite."""
import importlib.util
from pathlib import Path
import unittest
from copy import deepcopy
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("exchange_controls_core", Path(__file__).with_name("core.py"))
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)

NOW = 1800000000000
SCOPES = ["exchange:read", "exchange:write"]
OWNER = {"tenantId": "tenant-a", "subjectId": "ben-owner", "role": "owner", "scopes": SCOPES + ["exchange:admin"]}


def agent(subject="requester", **changes):
    return {"tenantId": "tenant-a", "subjectId": subject, "role": "agent", "scopes": list(SCOPES), **changes}


def config(capacity=5, max_units=5):
    return {"limits": {"retentionMs": 3600000, "reservationMs": 60000, "maxRecords": 30},
            "resources": {"synthetic-review": capacity},
            "grants": [{"subjectId": sid, "resources": ["synthetic-review"], "maxUnits": max_units}
                       for sid in ("requester", "provider", "outsider")]}


def terms(units=1):
    return {"description": "One fictional review", "deliverable": "Synthetic hashes",
            "acceptance": "Owner recomputes each hash", "cancellation": "Cancel before delivery; dispute afterwards",
            "resourceId": "synthetic-review", "units": units}


class Harness:
    def __init__(self, setup=True):
        self.state = core.empty_state("tenant-a")
        self.now = NOW
        self.seq = 0
        if setup:
            self.command("configure", OWNER, value=config())
            self.command("open", OWNER)

    def operation(self, operation, proposal_id=None):
        updated = core.apply_operation(self.state, operation, now=self.now, id=proposal_id)
        self.state = updated["state"]
        return updated["result"]

    def command(self, action, principal, proposal=None, value=None, key=None, command_extra=None):
        self.seq += 1
        command = {"action": action, "input": value or {}}
        if proposal is not None:
            command.update({"id": proposal["id"], "expectedVersion": proposal["version"]})
        if command_extra:
            command.update(command_extra)
        return self.operation({"kind": "command", "principal": principal, "command": command,
                               "key": key or f"key-{self.seq}"}, f"proposal-{self.seq}")

    def propose(self, units=1, provider="provider", key=None):
        return self.command("propose", agent(), value={"providerId": provider, "terms": terms(units), "sharingConsent": True}, key=key)

    def get(self, proposal, principal=OWNER):
        return self.operation({"kind": "get", "principal": principal, "id": proposal["id"]})

    def assent(self, proposal, subject, key=None):
        return self.command("assent", agent(subject), proposal,
                            {"termsRevision": proposal["termsRevision"], "termsHash": proposal["termsHash"], "sharingConsent": True}, key)

    def agreed(self, units=1):
        p = self.propose(units)
        p = self.assent(p, "requester")
        p = self.assent(p, "provider")
        return self.command("approve", OWNER, p,
                            {"termsRevision": p["termsRevision"], "termsHash": p["termsHash"]})

    def delivered(self):
        return self.command("deliver", agent("provider"), self.agreed(),
                            {"evidence": {"sha256": "a" * 64, "description": "Synthetic delivery digest"}})


class ControlsTests(unittest.TestCase):
    def assert_error(self, status, code, run):
        with self.assertRaises(core.ControlError) as caught:
            run()
        self.assertEqual(caught.exception.status, status)
        self.assertEqual(caught.exception.code, code)

    def test_default_grants_nothing(self):
        h = Harness(False)
        self.assertTrue(h.state["stopped"])
        self.assertEqual(h.state["resources"], {})
        self.assertEqual(h.state["grants"], {})
        h.command("stop", OWNER, value={"reason": "Stop remains available before policy exists"})
        self.assertTrue(h.state["stopped"])
        self.assert_error(503, "exchange_stopped", lambda: h.propose())
        self.assert_error(409, "configuration_required", lambda: h.command("open", OWNER))

    def test_configuration_requires_owner_defined_limits_and_denies_non_owner(self):
        h = Harness(False)
        self.assert_error(403, "owner_required", lambda: h.command("configure", agent(), value=config()))
        for field in ("limits", "grants", "resources"):
            value = config()
            del value[field]
            self.assert_error(400, "invalid_fields", lambda: h.command("configure", OWNER, value=value))
        for value in (True, -1, 1000001, 1.1):
            c = config()
            c["resources"]["synthetic-review"] = value
            self.assert_error(400, "invalid_integer", lambda: h.command("configure", OWNER, value=c))
        h.command("configure", OWNER, value=config(capacity=0, max_units=0))
        h.command("open", OWNER)
        self.assert_error(403, "grant_required", lambda: h.propose())

    def test_identity_tenant_scope_and_body_authority_attacks(self):
        h = Harness()
        p = h.propose()
        for principal, code in ((agent(tenantId="tenant-b"), "tenant_mismatch"),
                                (agent(scopes=[]), "scope_required"),
                                (agent(role="admin"), "invalid_role")):
            self.assert_error(403, code, lambda: h.get(p, principal))
        for field in ("actor", "tenantId", "ownerConfirmed", "requesterId", "role"):
            self.assert_error(400, "invalid_fields", lambda: h.command("assent", agent(), p, command_extra={field: "owner"}))
            self.assert_error(400, "invalid_fields", lambda: h.command("assent", agent(), p, {field: "owner"}))
        self.assert_error(404, "proposal_not_found", lambda: h.get(p, agent("outsider")))
        self.assert_error(403, "independent_parties_required", lambda: h.propose(provider="requester"))

    def test_independent_assents_exact_terms_and_separate_owner_approval(self):
        h = Harness()
        p = h.propose()
        binding = {"termsRevision": p["termsRevision"], "termsHash": p["termsHash"]}
        self.assert_error(403, "independent_agent_assent_required", lambda: h.command("assent", OWNER, p, {**binding, "sharingConsent": True}))
        self.assert_error(409, "independent_assents_required", lambda: h.command("approve", OWNER, p, binding))
        p = h.assent(p, "requester")
        self.assert_error(409, "already_assented", lambda: h.assent(p, "requester"))
        self.assert_error(403, "owner_required", lambda: h.command("approve", agent(), p, binding))
        p = h.assent(p, "provider")
        p = h.command("approve", OWNER, p, binding)
        self.assertEqual(p["status"], "agreed")
        self.assertEqual(p["reservedUnits"], 1)
        self.assertEqual(h.state["resources"]["synthetic-review"]["reserved"], 1)

    def test_revision_changes_invalidate_both_assents_and_cross_record_hash(self):
        h = Harness()
        first = h.propose()
        second = h.propose()
        self.assertNotEqual(first["termsHash"], second["termsHash"])
        first = h.assent(first, "requester")
        first = h.assent(first, "provider")
        old = deepcopy(first)
        new_terms = terms()
        new_terms["acceptance"] = "Owner performs an additional exact check"
        first = h.command("revise", agent("provider"), first, {"terms": new_terms})
        self.assertEqual(first["assents"], {})
        self.assertEqual(first["termsRevision"], 2)
        self.assert_error(409, "version_changed", lambda: h.assent(old, "requester"))
        self.assert_error(409, "terms_changed", lambda: h.command("assent", agent(), first,
                          {"termsRevision": old["termsRevision"], "termsHash": old["termsHash"], "sharingConsent": True}))
        self.assert_error(409, "terms_changed", lambda: h.command("assent", agent(), second,
                          {"termsRevision": first["termsRevision"], "termsHash": first["termsHash"], "sharingConsent": True}))

    def test_delivery_evidence_owner_acceptance_and_double_acceptance(self):
        h = Harness()
        p = h.agreed()
        evidence = {"evidence": {"sha256": "b" * 64, "description": "Owner can recompute synthetic manifest"}}
        self.assert_error(403, "provider_required", lambda: h.command("deliver", agent(), p, evidence))
        self.assert_error(400, "invalid_fields", lambda: h.command("deliver", agent("provider"), p,
                          {"evidence": {**evidence["evidence"], "url": "https://private.example"}}))
        p = h.command("deliver", agent("provider"), p, evidence)
        binding = {"deliveryRevision": 1, "deliveryHash": p["evidence"]["deliveryHash"], "assessment": "Owner verified all listed hashes"}
        self.assert_error(403, "owner_required", lambda: h.command("accept", agent(), p, binding))
        self.assert_error(409, "delivery_changed", lambda: h.command("accept", OWNER, p, {**binding, "deliveryHash": "c" * 64}))
        old = deepcopy(p)
        p = h.command("accept", OWNER, p, binding, "accept-once")
        self.assertEqual(p["status"], "completed")
        self.assertEqual(p["reservedUnits"], 0)
        self.assertEqual(h.state["resources"]["synthetic-review"]["used"], 1)
        self.assertEqual(h.command("accept", OWNER, old, binding, "accept-once"), p)
        self.assert_error(409, "terminal_proposal", lambda: h.command("accept", OWNER, p, binding))
        self.assertEqual(h.state["resources"]["synthetic-review"]["used"], 1)

    def test_resource_and_subject_caps_are_cumulative(self):
        h = Harness()
        a = h.agreed(3)
        p = h.propose(3)
        p = h.assent(p, "requester")
        p = h.assent(p, "provider")
        binding = {"termsRevision": p["termsRevision"], "termsHash": p["termsHash"]}
        before = deepcopy(h.state)
        self.assert_error(409, "resource_capacity_exceeded", lambda: h.command("approve", OWNER, p, binding))
        self.assertEqual(h.state, before)
        h.command("cancel", agent(), a, {"reason": "Synthetic requester withdrew"})
        p = h.command("approve", OWNER, p, binding)
        self.assertEqual(p["reservedUnits"], 3)
        h2 = Harness(False)
        h2.command("configure", OWNER, value=config(capacity=10, max_units=2))
        h2.command("open", OWNER)
        p2 = h2.propose(3)
        p2 = h2.assent(p2, "requester")
        p2 = h2.assent(p2, "provider")
        self.assert_error(409, "grant_capacity_exceeded", lambda: h2.command("approve", OWNER, p2,
                          {"termsRevision": p2["termsRevision"], "termsHash": p2["termsHash"]}))

    def test_idempotency_is_subject_tenant_and_content_scoped(self):
        h = Harness()
        p = h.propose(key="same-propose")
        self.assertEqual(h.propose(key="same-propose"), p)
        self.assertEqual(len(h.state["records"]), 1)
        self.assert_error(409, "idempotency_conflict", lambda: h.propose(2, key="same-propose"))
        p = h.assent(p, "requester", key="same-key")
        # Same opaque key from another independently authenticated principal is separate.
        p = h.assent(p, "provider", key="same-key")
        self.assertEqual(len(p["assents"]), 2)

    def test_revoked_identity_cannot_read_evidence_or_replay_cached_result(self):
        h = Harness()
        p = h.delivered()
        h.command("revoke", OWNER, value={"subjectId": "provider", "reason": "Owner revoked participation"})
        self.assertEqual(h.get(p)["status"], "disputed")
        self.assertEqual(h.state["resources"]["synthetic-review"]["reserved"], 1)
        self.assert_error(403, "grant_required", lambda: h.get(p, agent("provider")))
        op = next(r for r in h.state["requests"] if r["result"] and r["result"].get("evidence"))
        self.assertNotIn("Synthetic delivery digest", core.canonical_json(h.state["administrativeAudit"]))
        self.assertEqual(op["proposalId"], p["id"])
        self.assert_error(403, "grant_required", lambda: h.command("deliver", agent("provider"), p,
                         {"evidence": {"sha256": "a" * 64, "description": "Synthetic delivery digest"}}))

    def test_cancel_before_delivery_releases_after_delivery_holds_for_dispute(self):
        h = Harness()
        a = h.agreed(2)
        a = h.command("cancel", agent("provider"), a, {"reason": "Withdraw before delivery"})
        self.assertEqual(a["status"], "cancelled")
        self.assertEqual(h.state["resources"]["synthetic-review"]["reserved"], 0)
        p = h.delivered()
        p = h.command("cancel", agent(), p, {"reason": "Delivery does not match terms"})
        self.assertEqual(p["status"], "disputed")
        self.assertEqual(p["reservedUnits"], 1)
        self.assert_error(403, "owner_required", lambda: h.command("resolve", agent(), p, {"outcome": "cancel", "reason": "Self resolution"}))
        self.assert_error(409, "invalid_resolution", lambda: h.command("resolve", OWNER, p, {"outcome": "complete", "reason": "Skip acceptance"}))
        p = h.command("resolve", OWNER, p, {"outcome": "cancel", "reason": "Owner reviewed mismatch"})
        self.assertEqual(p["status"], "cancelled")
        self.assertEqual(h.state["resources"]["synthetic-review"]["reserved"], 0)

    def test_stop_switch_blocks_reads_retries_and_releases_reservations(self):
        h = Harness()
        p = h.agreed(2)
        h.command("stop", OWNER, value={"reason": "Owner safety stop"})
        self.assertTrue(h.state["stopped"])
        self.assertEqual(h.get(p)["status"], "revoked")
        self.assertEqual(h.state["resources"]["synthetic-review"]["reserved"], 0)
        self.assert_error(503, "exchange_stopped", lambda: h.get(p, agent()))
        self.assert_error(503, "exchange_stopped", lambda: h.propose())
        h.command("open", OWNER)
        self.assertEqual(h.get(p)["status"], "revoked")

    def test_policy_change_revokes_old_assents_and_preserves_consumed_capacity(self):
        h = Harness()
        p = h.delivered()
        evidence = p["evidence"]
        p = h.command("accept", OWNER, p, {"deliveryRevision": 1, "deliveryHash": evidence["deliveryHash"], "assessment": "Owner checked"})
        pending = h.agreed()
        h.command("configure", OWNER, value=config(capacity=4))
        self.assertTrue(h.state["stopped"])
        self.assertEqual(h.get(pending)["status"], "revoked")
        self.assertEqual(h.state["resources"]["synthetic-review"]["used"], 1)
        self.assertEqual(h.state["resources"]["synthetic-review"]["reserved"], 0)
        self.assert_error(409, "capacity_below_consumed", lambda: h.command("configure", OWNER, value=config(capacity=0)))

    def test_retention_purges_content_audit_retries_but_preserves_consumed_accounting(self):
        h = Harness()
        p = h.delivered()
        evidence = p["evidence"]
        p = h.command("accept", OWNER, p, {"deliveryRevision": 1, "deliveryHash": evidence["deliveryHash"], "assessment": "Owner checked"})
        h.now += 3600001
        result = h.operation({"kind": "purge"})
        self.assertEqual(result["purged"], 1)
        self.assertEqual(h.state["records"], {})
        self.assertFalse(any(r["proposalId"] == p["id"] for r in h.state["requests"]))
        self.assertNotIn("Synthetic delivery digest", core.canonical_json(h.state))
        self.assertEqual(h.state["resources"]["synthetic-review"]["used"], 1)

    def test_reservation_expiry_holds_for_owner_resolution_without_revival(self):
        h = Harness()
        p = h.agreed()
        h.now += 60001
        h.operation({"kind": "purge"})
        p = h.get(p)
        self.assertEqual(p["status"], "disputed")
        self.assertEqual(p["reservedUnits"], 1)
        self.assert_error(409, "invalid_transition", lambda: h.command("deliver", agent("provider"), p,
                         {"evidence": {"sha256": "a" * 64, "description": "Late synthetic delivery"}}))

    def test_logical_delete_owns_terminal_only_and_erases_private_content(self):
        h = Harness()
        p = h.propose()
        self.assert_error(409, "terminal_record_required", lambda: h.command("delete", OWNER, p, {"reason": "Too early"}))
        p = h.command("cancel", agent(), p, {"reason": "Withdraw"})
        self.assert_error(403, "owner_required", lambda: h.command("delete", agent(), p, {"reason": "No owner"}))
        h.command("delete", OWNER, p, {"reason": "Owner approved logical erasure"})
        self.assertEqual(h.state["records"], {})
        self.assertTrue(all(r["result"] is None for r in h.state["requests"] if r["proposalId"] == p["id"]))
        self.assertNotIn("Synthetic hashes", core.canonical_json(h.state))
        self.assert_error(404, "proposal_not_found", lambda: h.get(p))

    def test_state_reopen_is_detached_and_unknown_or_corrupt_state_fails_closed(self):
        h = Harness()
        p = h.agreed()
        recovered = deepcopy(h.state)
        self.assertEqual(core.apply_operation(recovered, {"kind": "get", "id": p["id"], "principal": OWNER}, now=h.now)["result"], p)
        for update in ({"schemaVersion": 99}, {"stopped": "false"}, {"privateUnknown": "value"}):
            invalid = {**deepcopy(h.state), **update}
            self.assert_error(503, "unsupported_state" if "schemaVersion" in update or "stopped" in update else "corrupt_state",
                              lambda: core.apply_operation(invalid, {"kind": "purge"}, now=h.now))
        invalid = deepcopy(h.state)
        invalid["resources"]["synthetic-review"]["reserved"] = 0
        self.assert_error(503, "corrupt_accounting", lambda: core.apply_operation(invalid, {"kind": "purge"}, now=h.now))
        invalid = deepcopy(h.state)
        invalid["records"][p["id"]]["terms"]["units"] = 4
        self.assert_error(503, "corrupt_terms", lambda: core.apply_operation(invalid, {"kind": "purge"}, now=h.now))

    def test_failed_operation_and_returned_results_cannot_mutate_input_state(self):
        h = Harness()
        p = h.propose()
        before = deepcopy(h.state)
        self.assert_error(409, "terms_changed", lambda: h.command("assent", agent(), p, {"termsRevision": 1, "termsHash": "a" * 64, "sharingConsent": True}))
        self.assertEqual(h.state, before)
        p["terms"]["description"] = "Mutated caller copy"
        self.assertEqual(h.get(p)["terms"]["description"], "One fictional review")

    def test_each_agent_must_consent_to_exact_versioned_disclosure(self):
        h = Harness()
        p = h.propose()
        self.assertEqual(p["disclosure"]["version"], "exchange-noncash-v1")
        self.assertTrue(p["disclosure"]["noExecution"])
        self.assertIn("no resale", p["disclosure"]["rights"])
        binding = {"termsRevision": p["termsRevision"], "termsHash": p["termsHash"]}
        self.assert_error(400, "invalid_fields", lambda: h.command("assent", agent(), p, binding))
        self.assert_error(400, "sharing_consent_required", lambda: h.command("assent", agent(), p, {**binding, "sharingConsent": False}))
        self.assert_error(409, "terms_changed", lambda: h.command("assent", agent(), p, {**binding, "termsRevision": True, "sharingConsent": True}))
        p = h.assent(p, "requester")
        self.assert_error(400, "sharing_consent_required", lambda: h.command("assent", agent("provider"), p, {**binding, "sharingConsent": False}))
        changed = deepcopy(p)
        changed["disclosure"]["rights"] = "resale allowed"
        self.assertNotEqual(core._terms_hash(changed), p["termsHash"])

    def test_erased_propose_key_returns_gone_instead_of_resurrecting_content(self):
        h = Harness()
        p = h.propose(key="delete-retry")
        p = h.command("cancel", agent(), p, {"reason": "Withdraw synthetic negotiation"})
        h.command("delete", OWNER, p, {"reason": "Approved erasure"})
        self.assert_error(410, "record_erased", lambda: h.propose(key="delete-retry"))
        self.assertEqual(h.state["records"], {})
        tombstones = [r for r in h.state["requests"] if r["proposalId"] == p["id"]]
        self.assertTrue(tombstones)
        self.assertTrue(all(r["result"] is None for r in tombstones))
        h.now += 3600001
        h.operation({"kind": "purge"})
        self.assertFalse(any(r["proposalId"] == p["id"] for r in h.state["requests"]))

    def test_retry_window_expiry_cannot_repeat_acceptance_or_release_consumed_quota(self):
        h = Harness()
        p = h.delivered()
        binding = {"deliveryRevision": 1, "deliveryHash": p["evidence"]["deliveryHash"], "assessment": "Owner verified"}
        p = h.command("accept", OWNER, p, binding, "stable-acceptance")
        h.now += 3600001
        h.operation({"kind": "purge"})
        self.assert_error(404, "proposal_not_found", lambda: h.command("accept", OWNER, p, binding, "stable-acceptance"))
        self.assertEqual(h.state["resources"]["synthetic-review"]["used"], 1)
        self.assert_error(409, "capacity_below_consumed", lambda: h.command("configure", OWNER, value=config(0)))

    def test_timeout_and_stop_hold_unresolved_delivery_until_owner_resolves(self):
        h = Harness()
        p = h.delivered()
        original_evidence = deepcopy(p["evidence"])
        h.now += 3600001
        h.operation({"kind": "purge"})
        p = h.get(p)
        self.assertEqual(p["status"], "disputed")
        self.assertTrue(p["retentionHold"])
        self.assertEqual(p["evidence"], original_evidence)
        self.assert_error(409, "unresolved_obligation", lambda: h.command("configure", OWNER, value=config()))
        h.command("stop", OWNER, value={"reason": "Owner stop while dispute is held"})
        p = h.get(p)
        self.assertEqual(p["reservedUnits"], 1)
        self.assertTrue(p["authorityRevoked"])
        p = h.command("resolve", OWNER, p, {"outcome": "cancel", "reason": "Owner reviewed held evidence"})
        self.assertEqual(h.state["resources"]["synthetic-review"]["reserved"], 0)
        h.operation({"kind": "purge"})
        self.assertEqual(h.state["records"], {})

    def test_stop_retry_is_effective_after_reopen_and_policy_revision(self):
        h = Harness()
        h.command("stop", OWNER, value={"reason": "Owner stop"}, key="stable-stop")
        h.command("open", OWNER)
        self.assertFalse(h.state["stopped"])
        h.command("stop", OWNER, value={"reason": "Owner stop"}, key="stable-stop")
        self.assertTrue(h.state["stopped"])
        h.state = core.recover_state(h.state, now=h.now)
        h.command("open", OWNER)
        h.command("stop", OWNER, value={"reason": "Owner stop"}, key="stable-stop")
        self.assertTrue(h.state["stopped"])
        h.command("configure", OWNER, value=config(capacity=7))
        h.command("open", OWNER)
        h.command("stop", OWNER, value={"reason": "Owner stop"}, key="stable-stop")
        self.assertTrue(h.state["stopped"])

    def test_old_open_cannot_bypass_stop_and_old_approval_marks_historical_response(self):
        h = Harness(False)
        h.command("configure", OWNER, value=config())
        h.command("open", OWNER, key="stable-open")
        p = h.propose()
        p = h.assent(p, "requester")
        p = h.assent(p, "provider")
        before = deepcopy(p)
        binding = {"termsRevision": p["termsRevision"], "termsHash": p["termsHash"]}
        p = h.command("approve", OWNER, p, binding, key="stable-approve")
        h.command("stop", OWNER, value={"reason": "Owner stop"})
        self.assert_error(503, "exchange_stopped", lambda: h.command("open", OWNER, key="stable-open"))
        self.assert_error(503, "exchange_stopped", lambda: h.command("approve", OWNER, before, binding, key="stable-approve"))
        h.command("open", OWNER)
        result = h.command("approve", OWNER, before, binding, key="stable-approve")
        self.assertEqual(result["replay"], {"historicalResponse": True, "currentVersion": h.get(p)["version"], "currentStatus": "revoked"})
        self.assertEqual(h.get(p)["reservedUnits"], 0)

    def test_recovery_stops_and_keeps_reserved_debt_evidence_and_retry_tombstones(self):
        h = Harness()
        pending = h.propose()
        reserved = h.agreed()
        delivered = h.delivered()
        accounting = deepcopy(h.state["resources"])
        recovered = core.recover_state(h.state, now=NOW + 999999999)
        self.assertEqual(recovered["resources"], accounting)
        self.assertTrue(recovered["stopped"])
        self.assertEqual(recovered["records"][pending["id"]]["status"], "revoked")
        for proposal in (reserved, delivered):
            row = recovered["records"][proposal["id"]]
            self.assertEqual(row["status"], "disputed")
            self.assertEqual(row["assents"], {})
            self.assertIsNone(row["approved"])
            self.assertTrue(row["authorityRevoked"])
            self.assertEqual(row["reservedUnits"], 1)
        self.assertEqual(recovered["records"][delivered["id"]]["evidence"], delivered["evidence"])
        self.assertTrue(all(r["result"] is None for r in recovered["requests"]))
        self.assertEqual(recovered["administrativeAudit"][-1]["action"], "offline-recovery")

    def test_corrupted_assent_evidence_audit_approval_and_retry_fail_closed(self):
        h = Harness()
        p = h.delivered()
        mutations = [
            (lambda row, state: row["assents"]["provider"].update(termsHash="a" * 64), "corrupt_assent"),
            (lambda row, state: row["evidence"].update(sha256="b" * 64), "corrupt_evidence"),
            (lambda row, state: row["events"][0].update(actorId="forged-owner"), "corrupt_audit"),
            (lambda row, state: row["approved"].update(ownerId="requester"), "corrupt_approval"),
            (lambda row, state: state["requests"][0].update(result={"forged": True}), "corrupt_retry"),
        ]
        for mutate, code in mutations:
            state = deepcopy(h.state)
            mutate(state["records"][p["id"]], state)
            self.assert_error(503, code, lambda: core.apply_operation(state, {"kind": "purge"}, now=NOW))

    def test_retained_completed_history_detects_downward_consumed_debt_corruption(self):
        h = Harness()
        p = h.delivered()
        h.command("accept", OWNER, p, {"deliveryRevision": 1, "deliveryHash": p["evidence"]["deliveryHash"], "assessment": "Owner checked"})
        invalid = deepcopy(h.state)
        invalid["resources"]["synthetic-review"]["used"] = 0
        self.assert_error(503, "corrupt_accounting", lambda: core.apply_operation(invalid, {"kind": "purge"}, now=NOW))

    def test_safety_controls_survive_retry_and_audit_capacity(self):
        h = Harness()
        p = h.agreed()
        # Saturate the ordinary growth threshold using existing, valid state.
        with patch.object(core, "NORMAL_STATE_BYTES", 1):
            self.assert_error(409, "state_capacity_reached", lambda: h.propose())
            count = len(h.state["requests"])
            p = h.command("cancel", agent(), p, {"reason": "Capacity must not trap a reservation"})
            self.assertEqual(p["status"], "cancelled")
            self.assertEqual(len(h.state["requests"]), count)
            h.command("stop", OWNER, value={"reason": "Safety controls remain available"})
            self.assertTrue(h.state["stopped"])
        h = Harness()
        p = h.propose()
        # Lower only the audit cap to isolate it from the independent byte quota.
        with patch.object(core, "MAX_EVENTS", 5):
            for index in range(core.MAX_EVENTS - 1):
                change = terms()
                change["description"] = f"Synthetic revision {index}"
                p = h.command("revise", agent(), p, {"terms": change})
            self.assert_error(409, "audit_capacity_reached", lambda: h.command("revise", agent(), p, {"terms": terms()}))
            p = h.command("cancel", agent(), p, {"reason": "Audit capacity must not prevent cancellation"})
            self.assertEqual(p["status"], "cancelled")
            self.assertEqual(len(p["events"]), core.MAX_EVENTS)
            self.assertEqual(p["auditDropped"], 1)

    def test_repeated_disputes_and_revocations_do_not_grow_record_audit(self):
        h = Harness()
        p = h.delivered()
        p = h.command("dispute", agent(), p, {"reason": "Synthetic delivery mismatch"})
        self.assert_error(409, "already_disputed", lambda: h.command("dispute", agent(), p, {"reason": "Repeat mismatch"}))
        h.command("revoke", OWNER, value={"subjectId": "provider", "reason": "Owner revokes provider"})
        p = h.get(p)
        version, events = p["version"], len(p["events"])
        h.command("revoke", OWNER, value={"subjectId": "provider", "reason": "Owner retries revocation"})
        p = h.get(p)
        self.assertEqual((p["version"], len(p["events"])), (version, events))


class FullSizeSafetyTests(unittest.TestCase):
    """Exercise reachable admission/compaction limits without patching constants.

    All fixture growth goes through real commands. Repeated owner safety calls
    are adversarial new intents, using independent keys, not synthetic rows
    inserted into the state. Trusted recovery is exercised repeatedly because
    it must remain safe even when restored histories already fill the audit.
    """

    @classmethod
    def setUpClass(cls):
        if (core.NORMAL_STATE_BYTES, core.MAX_EVENTS, core.SAFETY_STATE_BYTES) != (650 * 1024, 100, 950 * 1024):
            raise AssertionError("Full-size tests require the actual production constants")
        h = Harness(False)
        h.command("configure", OWNER, value=config(capacity=1000, max_units=1000))
        h.command("open", OWNER)
        completed = h.delivered()
        cls.delivery_operation = {"kind": "command", "principal": agent("provider"),
                                  "key": f"key-{h.seq}",
                                  "command": {"action": "deliver", "id": completed["id"],
                                              "expectedVersion": completed["version"] - 1,
                                              "input": {"evidence": {"sha256": "a" * 64,
                                                                      "description": "Synthetic delivery digest"}}}}
        h.command("accept", OWNER, completed,
                  {"deliveryRevision": 1, "deliveryHash": completed["evidence"]["deliveryHash"],
                   "assessment": "Synthetic owner recomputed every fixture hash"})
        cls.ids = {"completed": completed["id"]}
        cls.ids["cancel"] = h.agreed(2)["id"]
        for name, units in (("dispute", 3), ("revoke", 4), ("resolve", 5)):
            p = h.agreed(units)
            p = h.command("deliver", agent("provider"), p,
                          {"evidence": {"sha256": str(units) * 64,
                                        "description": "Synthetic evidence " + name}})
            if name == "resolve":
                p = h.command("dispute", agent(), p, {"reason": "Owner must resolve synthetic mismatch"})
            cls.ids[name] = p["id"]
        p = h.propose()
        cls.ids["history"] = p["id"]
        large_terms = terms()
        for field in ("description", "deliverable", "acceptance", "cancellation"):
            large_terms[field] = "\U0001f9ea" * 1000
        for index in range(core.MAX_EVENTS):
            before = core.canonical_json(h.state)
            try:
                p = h.command("revise", agent(), p, {"terms": large_terms})
            except core.ControlError as error:
                if error.code != "state_capacity_reached" or core.canonical_json(h.state) != before:
                    raise AssertionError("Expected immutable byte-admission rejection") from error
                break
        else:
            raise AssertionError("Large legitimate histories did not reach byte admission")
        # A small, authorized owner operation fills the remaining gap without
        # altering resource commitments or requiring a fake state edit.
        for _ in range(core.MAX_REQUESTS):
            before = core.canonical_json(h.state)
            try:
                h.command("open", OWNER)
            except core.ControlError as error:
                if error.code != "state_capacity_reached" or core.canonical_json(h.state) != before:
                    raise AssertionError("Ordinary admission did not roll back exactly") from error
                break
        else:
            raise AssertionError("Ordinary commands did not fill the real byte cap")
        size = len(core.canonical_json(h.state).encode("utf-8"))
        if not core.NORMAL_STATE_BYTES * 0.99 <= size <= core.NORMAL_STATE_BYTES:
            raise AssertionError("Fixture must occupy at least 99% of actual normal admission")
        cls.base_state, cls.base_seq = deepcopy(h.state), h.seq
        print("FULL_SIZE_BOUNDARY " + core.canonical_json({
            "bytes": size, "normalBytes": core.NORMAL_STATE_BYTES,
            "safetyBytes": core.SAFETY_STATE_BYTES, "maxEvents": core.MAX_EVENTS,
            "records": len(h.state["records"]), "requests": len(h.state["requests"]),
            "reserved": h.state["resources"]["synthetic-review"]["reserved"],
            "used": h.state["resources"]["synthetic-review"]["used"]}), flush=True)

    def fixture(self):
        h = Harness(False)
        h.state, h.seq = deepcopy(self.base_state), self.base_seq
        return h

    def assert_error(self, status, code, run):
        with self.assertRaises(core.ControlError) as caught:
            run()
        self.assertEqual((caught.exception.status, caught.exception.code), (status, code))

    def assert_bounded(self, state):
        size = len(core.canonical_json(state).encode("utf-8"))
        self.assertLessEqual(size, core.SAFETY_STATE_BYTES)
        self.assertLessEqual(len(state["requests"]), core.MAX_REQUESTS)
        self.assertLessEqual(len(state["administrativeAudit"]), 256)
        self.assertTrue(all(len(p["events"]) <= core.MAX_EVENTS for p in state["records"].values()))
        core._validate_state(state)
        return size

    def test_real_byte_boundary_all_safety_actions_keep_exact_accounting(self):
        results = {}
        for action in ("cancel", "dispute", "resolve", "revoke", "stop", "recover"):
            with self.subTest(action=action):
                h = self.fixture()
                before = deepcopy(h.state)
                before_bytes = core.canonical_json(h.state)
                original_reserved = h.state["resources"]["synthetic-review"]["reserved"]
                original_used = h.state["resources"]["synthetic-review"]["used"]
                # A failed ordinary operation cannot leak a partial record,
                # audit event, retry row or revised resource counter.
                self.assert_error(409, "state_capacity_reached", lambda: h.command("open", OWNER))
                self.assertEqual(core.canonical_json(h.state), before_bytes)
                if action in ("cancel", "dispute", "resolve"):
                    p = h.get({"id": self.ids[action]})
                    value = {"reason": "\U0001f9ea" * 300}
                    if action == "resolve":
                        value["outcome"] = "cancel"
                    h.command(action, OWNER if action == "resolve" else agent(), p, value)
                    expected_reserved = original_reserved - {"cancel": 2, "dispute": 0, "resolve": 5}[action]
                elif action == "revoke":
                    h.command("revoke", OWNER, value={"subjectId": "provider", "reason": "\U0001f9ea" * 300})
                    expected_reserved = original_reserved - 2
                    self.assertTrue(h.state["grants"]["provider"]["revoked"])
                    self.assert_error(403, "grant_required", lambda: h.get({"id": self.ids["revoke"]}, agent("provider")))
                elif action == "stop":
                    h.command("stop", OWNER, value={"reason": "\U0001f9ea" * 300})
                    expected_reserved = original_reserved - 2
                    self.assertTrue(h.state["stopped"])
                    self.assert_error(503, "exchange_stopped", lambda: h.get({"id": self.ids["dispute"]}, agent()))
                else:
                    recovered = core.recover_state(h.state, now=h.now)
                    self.assertEqual(h.state, before)
                    h.state = recovered
                    expected_reserved = original_reserved
                    self.assertTrue(h.state["stopped"])
                    self.assertTrue(all(r["result"] is None for r in h.state["requests"]))
                self.assertEqual(h.state["resources"]["synthetic-review"]["reserved"], expected_reserved)
                self.assertEqual(h.state["resources"]["synthetic-review"]["used"], original_used)
                for sid in ("requester", "provider"):
                    self.assertEqual(h.state["grants"][sid]["reserved"], expected_reserved)
                    self.assertEqual(h.state["grants"][sid]["used"], original_used)
                for name in ("dispute", "revoke", "resolve"):
                    if name == action and action == "resolve":
                        continue
                    p = h.state["records"][self.ids[name]]
                    self.assertEqual(p["evidence"], before["records"][p["id"]]["evidence"])
                    self.assertEqual(p["reservedUnits"], before["records"][p["id"]]["reservedUnits"])
                results[action] = self.assert_bounded(h.state)
        print("FULL_SIZE_SAFETY_ACTIONS " + core.canonical_json(results), flush=True)

    def test_real_safety_compaction_tombstones_reauthorize_and_repeats_stay_bounded(self):
        h = self.fixture()
        first_request = deepcopy(h.state["requests"][0])
        self.assertIsNotNone(first_request["result"])
        peak = len(core.canonical_json(h.state).encode("utf-8"))
        compaction_at = None
        original_resources = deepcopy(h.state["resources"])
        original_debts = {pid: p["reservedUnits"] for pid, p in h.state["records"].items()}
        for index in range(768):
            h.command("revoke", OWNER, value={"subjectId": "outsider", "reason": "\U0001f9ea" * 300})
            size = self.assert_bounded(h.state)
            peak = max(peak, size)
            row = next(r for r in h.state["requests"] if r["keyHash"] == first_request["keyHash"])
            if row["result"] is None and compaction_at is None:
                compaction_at = index + 1
                self.assertTrue(all(r["result"] is None for r in h.state["requests"]))
                self.assertLess(size, core.NORMAL_STATE_BYTES)
        self.assertIsNotNone(compaction_at, "Actual950KiB safety compaction must execute")
        self.assertLessEqual(compaction_at, 256)
        self.assertEqual(len(h.state["administrativeAudit"]), 256)
        self.assertEqual(h.state["resources"], original_resources)
        self.assertEqual({pid: p["reservedUnits"] for pid, p in h.state["records"].items()}, original_debts)
        # The original configure retry is now an opaque tombstone. Exact old
        # content is unavailable, while fresh authorization runs before replay.
        operation = {"kind": "command", "principal": OWNER, "key": "key-1",
                     "command": {"action": "configure", "input": config(capacity=1000, max_units=1000)}}
        before_bytes = core.canonical_json(h.state)
        self.assert_error(410, "record_erased", lambda: h.operation(operation))
        self.assertEqual(core.canonical_json(h.state), before_bytes)
        operation["principal"] = {**OWNER, "tenantId": "tenant-b"}
        self.assert_error(403, "tenant_mismatch", lambda: h.operation(operation))
        operation["principal"] = {**OWNER, "scopes": []}
        self.assert_error(403, "scope_required", lambda: h.operation(operation))
        # A current participant can still read its held delivery; unrelated or
        # revoked actors cannot obtain evidence from either records or retries.
        self.assertIsNotNone(h.get({"id": self.ids["dispute"]}, agent("provider"))["evidence"])
        self.assert_error(403, "grant_required", lambda: h.get({"id": self.ids["dispute"]}, agent("outsider")))
        self.assert_error(410, "record_erased", lambda: h.operation(deepcopy(self.delivery_operation)))
        h.command("revoke", OWNER, value={"subjectId": "provider", "reason": "Revoke access before a cached delivery retry"})
        self.assert_error(403, "grant_required", lambda: h.operation(deepcopy(self.delivery_operation)))
        self.assertEqual(h.state["resources"]["synthetic-review"]["used"], 1)
        self.assertEqual(h.state["resources"]["synthetic-review"]["reserved"], 12)
        print("FULL_SIZE_COMPACTION " + core.canonical_json({
            "compactionAt": compaction_at, "repeatedSafetyCommands": 768,
            "peakCommittedBytes": peak, "finalBytes": self.assert_bounded(h.state),
            "tombstones": sum(r["result"] is None for r in h.state["requests"]),
            "auditEntries": len(h.state["administrativeAudit"])}), flush=True)

    def test_actual_full_audit_recovery_and_resolution_keep_obligations(self):
        h = self.fixture()
        original_resources = deepcopy(h.state["resources"])
        evidence = {pid: deepcopy(p["evidence"]) for pid, p in h.state["records"].items()}
        target_id = self.ids["resolve"]
        for index in range(core.MAX_EVENTS + 25):
            before = deepcopy(h.state)
            before_bytes = core.canonical_json(before)
            h.state = core.recover_state(before, now=h.now + index)
            # Recovery returns a detached copy; even full audit compaction must
            # not mutate the supplied restored snapshot.
            self.assertEqual(core.canonical_json(before), before_bytes)
            self.assertEqual(h.state["resources"], original_resources)
            self.assertTrue(all(r["result"] is None for r in h.state["requests"]))
            self.assert_bounded(h.state)
        target = h.state["records"][target_id]
        self.assertEqual(len(target["events"]), core.MAX_EVENTS)
        self.assertGreater(target["auditDropped"], 0)
        self.assertEqual(target["events"][-1]["sequence"], target["auditDropped"] + core.MAX_EVENTS)
        for pid, original in evidence.items():
            self.assertEqual(h.state["records"][pid]["evidence"], original)
        before_version, before_dropped = target["version"], target["auditDropped"]
        # No further assent/delivery/automatic completion can bypass recovery.
        self.assert_error(503, "exchange_stopped", lambda: h.command("deliver", agent("provider"), target,
                          {"evidence": {"sha256": "a" * 64, "description": "Late delivery"}}))
        before_bytes = core.canonical_json(h.state)
        self.assert_error(409, "version_changed", lambda: h.command("resolve", OWNER, {**target, "version": before_version - 1},
                          {"outcome": "cancel", "reason": "Stale snapshot cannot resolve held debt"}))
        self.assertEqual(core.canonical_json(h.state), before_bytes)
        target = h.command("resolve", OWNER, target, {"outcome": "cancel", "reason": "\U0001f9ea" * 300})
        self.assertEqual(target["status"], "cancelled")
        self.assertEqual(len(target["events"]), core.MAX_EVENTS)
        self.assertEqual(target["auditDropped"], before_dropped + 1)
        self.assertEqual(h.state["resources"]["synthetic-review"]["reserved"], original_resources["synthetic-review"]["reserved"] - 5)
        self.assertEqual(h.state["resources"]["synthetic-review"]["used"], original_resources["synthetic-review"]["used"])
        print("FULL_SIZE_AUDIT_RECOVERY " + core.canonical_json({
            "recoveries": core.MAX_EVENTS + 25, "events": len(target["events"]),
            "auditDropped": target["auditDropped"], "finalBytes": self.assert_bounded(h.state),
            "reserved": h.state["resources"]["synthetic-review"]["reserved"],
            "used": h.state["resources"]["synthetic-review"]["used"]}), flush=True)


if __name__ == "__main__":
    unittest.main()
