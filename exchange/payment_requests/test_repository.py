"""Synthetic-only transaction, migration and recovery tests; no wallet calls.

LUMEN_HOUSE_NATIVE_SOURCE enables tests against an installed source candidate.
Every database is temporary; source code and existing stores are never changed.
"""
from copy import deepcopy
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from . import core, repository as adapter
from ..controls import repository as exchange
from ..controls.recovery_fence import initialize_lineage


NOW = 1792929600000
OWNER = {"tenantId": "tenant-a", "subjectId": "owner-a", "role": "owner",
         "scopes": ["payments:read", "payments:prepare", "payments:control"]}


def source(job="paid-job-1"):
    return {"tenantId": "tenant-a", "jobId": job, "ownerId": "owner-a", "workerId": "worker-a",
            "payee": "0x" + "1" * 40, "termsHash": "a" * 64, "termsVersion": 1,
            "termsDocumentHash": "b" * 64, "evidenceId": "evidence-1", "evidenceHash": "c" * 64,
            "evidenceSize": 512, "acceptanceId": "acceptance-1", "acceptanceHash": "d" * 64,
            "amountAtomic": 100000, "currency": "USDC", "network": "base", "asset": "usdc", "decimals": 6,
            "sourceKind": "native-paid-fulfillment", "status": "accepted", "recordEvidenceClass": "synthetic",
            "artifactCheck": {"status": "unavailable", "reason": "artifact_bytes_not_retained"},
            "payeeVerification": {"basis": "owner_attestation", "bindingHash": "e" * 64, "independentProof": False}}


def command(state, action, value=None, *, job=None, request_id=None, now=NOW):
    value = value or {}
    operation = {"kind": "command", "principal": OWNER,
                 "command": {"action": action, "input": value}}
    if job:
        operation["key"], operation["source"] = "prepare-" + job, source(job)
    if request_id:
        operation["command"]["id"] = request_id
    return core.apply_operation(state, operation, now=now, id="request-" + (job or action))["state"]


def opened_state():
    state = command(core.empty_state("tenant-a"), "configure", {"approvalReference": "Synthetic approved ceiling fixture"})
    return command(state, "open")


def with_request(state=None, job="paid-job-1"):
    return command(state or opened_state(), "prepare", {"jobId": job}, job=job)


class Fixture:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("CREATE TABLE principals(id TEXT PRIMARY KEY,role TEXT,active INTEGER)")
        self.connection.execute("INSERT INTO principals VALUES('owner-a','owner',1)")
        for statement in exchange.DDL + adapter.DDL:
            self.connection.execute(statement)
        initialize_lineage(self.connection)
        self.connection.execute("PRAGMA user_version=13")
        self.connection.execute("INSERT INTO x_exchange_tenants VALUES('tenant-a','owner-a')")
        self.lock = threading.RLock()

    @contextlib.contextmanager
    def transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise


class PaymentTablesTests(unittest.TestCase):
    def setUp(self):
        self.repository = Fixture()
        self.addCleanup(self.repository.connection.close)
        self.tables = adapter.PaymentTables(self.repository)

    def test_constructor_requires_13_and_creates_no_authority_or_requests(self):
        with self.repository.transaction():
            self.assertEqual(self.tables.load_state("tenant-a"), (None, 0))
            self.assertEqual(self.tables.tenant_owner("tenant-a"), "owner-a")
            self.assertIsNone(self.tables.tenant_owner("absent"))
            self.assertEqual(self.repository.connection.execute("SELECT COUNT(*) FROM x_exchange_memberships").fetchone()[0], 0)
        self.repository.connection.execute("PRAGMA user_version=12")
        with self.assertRaisesRegex(adapter.RepositoryError, "payment_requests_migration_required"):
            adapter.PaymentTables(self.repository)

    def test_reads_and_writes_require_the_native_transaction(self):
        for run in (lambda: self.tables.tenant_owner("tenant-a"),
                    lambda: self.tables.load_state("tenant-a"),
                    lambda: self.tables.save_state("tenant-a", opened_state(), 0)):
            with self.assertRaisesRegex(adapter.RepositoryError, "native_transaction_required"):
                run()

    def test_compare_and_swap_and_rollback_are_atomic(self):
        initial = opened_state()
        with self.repository.transaction():
            self.assertEqual(self.tables.save_state("tenant-a", initial, 0), 1)
        updated = with_request(initial)
        with self.repository.transaction():
            self.assertEqual(self.tables.save_state("tenant-a", updated, 1), 2)
            for stale in (0, 1, True, -1, 2147483647):
                with self.assertRaises(adapter.RepositoryError):
                    self.tables.save_state("tenant-a", updated, stale)
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            with self.repository.transaction():
                self.tables.save_state("tenant-a", with_request(updated, "paid-job-2"), 2)
                raise RuntimeError("interrupted")
        with self.repository.transaction():
            self.assertEqual(self.tables.load_state("tenant-a"), (updated, 2))

    def test_exact_tenant_and_immutable_owner_binding(self):
        for state in (core.empty_state("other-tenant"),
                      {**opened_state(), "approval": {**opened_state()["approval"], "ownerId": "other-owner"}}):
            with self.repository.transaction():
                with self.assertRaisesRegex(adapter.RepositoryError, "invalid_payment_request_state"):
                    self.tables.save_state("tenant-a", state, 0)
        with self.repository.transaction():
            with self.assertRaisesRegex(adapter.RepositoryError, "payment_request_tenant_required"):
                self.tables.save_state("absent", core.empty_state("absent"), 0)

    def test_utf8_capacity_json_type_and_raw_sql_constraints(self):
        for state in ([1], {"value": float("nan")}, {"value": "\ud800"}, {"value": "é" * adapter.MAX_STATE_BYTES}):
            with self.assertRaises(adapter.RepositoryError):
                adapter._serialized(state)
        for body in ("[]", '{"huge":"' + "é" * adapter.MAX_STATE_BYTES + '"}'):
            with self.assertRaises(sqlite3.IntegrityError):
                self.repository.connection.execute("INSERT INTO p_payment_request_state VALUES('tenant-a',?,1)", (body,))

    def test_exposure_original_day_outcome_and_retry_tombstones_are_retained(self):
        initial = with_request()
        request_id = next(iter(initial["records"]))
        reported = command(initial, "report_outcome", {"outcome": "unknown", "reason": "Synthetic wallet outcome unknown"}, request_id=request_id)
        recovered = core.recover_state(reported, now=NOW + 1000)
        with self.repository.transaction():
            self.tables.save_state("tenant-a", recovered, 0)
        variants = [opened_state(), initial, reported]
        moved = deepcopy(recovered)
        row = moved["records"][request_id]
        row["preparedAt"] += 86400000
        row["createdAt"] = row["preparedAt"]
        row["dayWindow"] = core.day_window(row["preparedAt"])
        row["outcomeReport"]["at"] = row["preparedAt"]
        row["requestHash"] = core._request_hash(row)
        variants.append(moved)
        for replacement in variants:
            core.validate_state(replacement)
            with self.repository.transaction():
                with self.assertRaisesRegex(adapter.RepositoryError, "payment_request_exposure_cannot_be_erased"):
                    self.tables.save_state("tenant-a", replacement, 1)
                self.assertEqual(self.tables.load_state("tenant-a"), (recovered, 1))
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.connection.execute("DELETE FROM p_payment_request_state")

    def test_parallel_final_daily_slot_has_one_winner_and_no_overspend(self):
        state = opened_state()
        for index in range(9):
            state = with_request(state, "initial-" + str(index))
        with self.repository.transaction():
            self.tables.save_state("tenant-a", state, 0)
        barrier, outcomes = threading.Barrier(2), []

        def reserve(job):
            barrier.wait()
            try:
                with self.repository.lock, self.repository.transaction():
                    before, version = self.tables.load_state("tenant-a")
                    after = with_request(before, job)
                    self.tables.save_state("tenant-a", after, version)
                outcomes.append("saved")
            except core.ControlError as error:
                outcomes.append(error.code)

        threads = [threading.Thread(target=reserve, args=(job,)) for job in ("parallel-a", "parallel-b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertCountEqual(outcomes, ["saved", "daily_principal_ceiling"])
        with self.repository.transaction():
            final, version = self.tables.load_state("tenant-a")
            self.assertEqual((len(final["records"]), version), (10, 2))
            self.assertEqual(sum(row["principalHoldAtomic"] for row in final["records"].values()), 1000000)


@unittest.skipUnless(os.environ.get("LUMEN_HOUSE_NATIVE_SOURCE"), "explicit installed House source required")
class NativePaymentRepositoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source_path = Path(os.environ["LUMEN_HOUSE_NATIVE_SOURCE"]).resolve()
        if not (source_path / "fulfillment_workflow.py").is_file():
            raise ValueError("existing source candidate required")
        sys.path.insert(0, str(source_path))
        import fulfillment_workflow
        cls.native, cls.Native = fulfillment_workflow, adapter.native_repository_class()

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="payment-request-native-test-")
        self.addCleanup(self.folder.cleanup)
        self.directory = Path(self.folder.name)
        self.path = self.directory / "synthetic.sqlite3"
        repository = self.native.FulfillmentRepository(self.path)
        with repository.lock, repository.transaction():
            repository.connection.execute("INSERT INTO principals VALUES('owner-a','owner',1,'2026-01-01T00:00:00Z')")
            repository.connection.execute("INSERT INTO r_providers VALUES('worker-a',1,'2026-01-01T00:00:00Z')")
        repository.close()

    def at12(self):
        repository = exchange.native_repository_class()(self.path, migrate=True)
        with repository.lock, repository.transaction():
            repository.connection.execute("INSERT INTO x_exchange_tenants VALUES('tenant-a','owner-a')")
            repository.connection.execute("INSERT INTO x_exchange_memberships VALUES('tenant-a','owner-a','owner','owner','[\"exchange:admin\"]',1,NULL)")
        repository.close()

    def rows(self):
        with sqlite3.connect(self.path) as db:
            names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            return {name: db.execute('SELECT * FROM "' + name + '"').fetchall() for name in names}

    def checkpoint(self, repository):
        directory = self.directory / "checkpoints"
        directory.mkdir(mode=0o700, exist_ok=True)
        checkpoint = adapter.export_checkpoint(repository, directory)
        return {"authority_store": self.path, "checkpoint_path": directory / checkpoint["checkpointFile"], "checkpoint_pin": checkpoint["pin"]}

    def populated13(self):
        self.at12()
        repository = self.Native(self.path, migrate=True)
        with repository.lock, repository.transaction():
            state = with_request()
            adapter.PaymentTables(repository).save_state("tenant-a", state, 0)
            repository.connection.execute("INSERT INTO credentials(id,principal_id,token_hash,created_at,expires_at) VALUES('synthetic-owner-credential','owner-a',?,'2026-01-01T00:00:00Z','2099-01-01T00:00:00Z')", ("a" * 64,))
            repository.connection.execute("INSERT INTO r_credentials(id,principal_id,token_hash,created_at,expires_at) VALUES('synthetic-provider-credential','worker-a',?,'2026-01-01T00:00:00Z','2099-01-01T00:00:00Z')", ("b" * 64,))
        return repository, state

    def test_11_is_never_implicitly_or_directly_upgraded(self):
        before = self.path.read_bytes()
        for decision in (False, True):
            with self.assertRaisesRegex(adapter.RepositoryError, "explicit_payment_request_migration_required"):
                self.Native(self.path, migrate=decision)
            self.assertEqual(self.path.read_bytes(), before)

    def test_12_requires_explicit_decision_and_13_preserves_all_old_rows(self):
        self.at12()
        before, before_bytes = self.rows(), self.path.read_bytes()
        with self.assertRaisesRegex(adapter.RepositoryError, "explicit_payment_request_migration_required"):
            self.Native(self.path)
        self.assertEqual(self.path.read_bytes(), before_bytes)
        repository = self.Native(self.path, migrate=True)
        with repository.lock, repository.transaction():
            self.assertEqual(repository.connection.execute("SELECT COUNT(*) FROM p_payment_request_state").fetchone()[0], 0)
        repository.close()
        after = self.rows()
        for name, rows in before.items():
            self.assertEqual(after[name][:-1] if name == "schema_migrations" else after[name], rows, name)
        self.assertEqual(after["schema_migrations"][-1][0], 13)
        self.assertEqual(adapter.check_store(self.path)["schema_version"], 13)
        before_reopen = self.rows()
        repository = self.Native(self.path)
        repository.close()
        self.assertEqual(self.rows(), before_reopen)

    def test_independent_schema_pin_and_tamper_future_missing_refuse_writes(self):
        self.at12()
        repository = self.Native(self.path, migrate=True)
        repository.close()
        with sqlite3.connect(self.path) as db:
            objects = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
            encoded = json.dumps(objects, separators=(",", ":"), ensure_ascii=False).encode()
            self.assertEqual(hashlib.sha256(encoded).hexdigest(), adapter.SCHEMA13_SHA256)
            db.execute("DROP TRIGGER p_payment_request_state_retained")
        before = self.path.read_bytes()
        with self.assertRaises(Exception):
            self.Native(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA user_version=14")
        before = self.path.read_bytes()
        with self.assertRaises(Exception):
            self.Native(self.path, migrate=True)
        self.assertEqual(self.path.read_bytes(), before)
        missing = self.directory / "missing.sqlite3"
        with self.assertRaises(Exception):
            self.Native(missing, migrate=True)
        self.assertFalse(missing.exists())

    def test_reopen_persists_holds_and_second_native_writer_is_rejected(self):
        repository, state = self.populated13()
        with self.assertRaises(Exception):
            self.Native(self.path, busy_timeout=0.01)
        repository.close()
        repository = self.Native(self.path)
        self.addCleanup(repository.close)
        with repository.lock, repository.transaction():
            self.assertEqual(adapter.PaymentTables(repository).load_state("tenant-a"), (state, 1))

    def test_backup_restore_holds_all_authority_and_retains_pending_principal(self):
        repository, state = self.populated13()
        options = self.checkpoint(repository)
        repository.close()
        backup = self.directory / "backup.sqlite3"
        adapter.backup_store(self.path, backup)
        destination = self.directory / "restored.sqlite3"
        result = adapter.restore_store(backup, destination, **options)
        self.assertTrue(result["financial_stop_latched"])
        self.assertTrue(result["exchange_stop_latched"])
        self.assertTrue(result["payment_requests_stop_latched"])
        self.assertTrue(result["erasure_authority_verified"])
        self.assertFalse(result["financial_execution_enabled"])
        recovered = self.Native(destination)
        self.addCleanup(recovered.close)
        with recovered.lock, recovered.transaction():
            actual, version = adapter.PaymentTables(recovered).load_state("tenant-a")
            self.assertEqual((actual["records"], actual["obligations"]), (state["records"], state["obligations"]))
            self.assertEqual(version, 2)
            self.assertTrue(actual["stopped"])
            self.assertTrue(all(row["disabled"] for row in actual["retries"]))
            self.assertEqual(actual["audit"][-1]["action"], "offline-recovery")
            self.assertEqual(recovered.connection.execute("SELECT active FROM x_exchange_memberships").fetchone()[0], 0)
            self.assertEqual(recovered.connection.execute("SELECT stopped FROM wf_controls WHERE id=1").fetchone()[0], 1)
            for table in ("credentials", "r_credentials"):
                self.assertIsNotNone(recovered.connection.execute("SELECT revoked_at FROM " + table).fetchone()[0])

    @unittest.skipUnless(os.environ.get("LUMEN_HOUSE_NATIVE_FIXTURES"), "explicit native fixture helpers required")
    def test_nonempty_native_policy_execution_and_standing_history_restore_at_13(self):
        # Reuse the existing public recovery regression's external fixture
        # contract. Native private fixture implementation stays outside Git.
        from ..controls.test_native_recovery import NativeRecoveryTests
        helper = NativeRecoveryTests()
        helper.setUpClass()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        repository = exchange.native_repository_class()(helper.path, migrate=True)
        with repository.lock, repository.transaction():
            db = repository.connection
            db.execute("INSERT INTO principals VALUES('owner-a','owner',1,'2026-01-01T00:00:00Z')")
            db.execute("INSERT INTO x_exchange_tenants VALUES('tenant-a','owner-a')")
            db.execute("INSERT INTO x_exchange_memberships VALUES('tenant-a','owner-a','owner','owner','[\"exchange:admin\"]',1,NULL)")
        repository.close()
        repository = self.Native(helper.path, migrate=True)
        initial = with_request()
        with repository.lock, repository.transaction():
            adapter.PaymentTables(repository).save_state("tenant-a", initial, 0)
        directory = helper.root / "payment-checkpoints"
        directory.mkdir(mode=0o700)
        checkpoint = adapter.export_checkpoint(repository, directory)
        repository.close()
        backup, destination = helper.root / "backup13.sqlite3", helper.root / "restore13.sqlite3"
        adapter.backup_store(helper.path, backup)
        result = adapter.restore_store(backup, destination, authority_store=helper.path,
            checkpoint_path=directory / checkpoint["checkpointFile"], checkpoint_pin=checkpoint["pin"])
        recovered = self.Native(destination)
        self.addCleanup(recovered.close)
        with recovered.lock, recovered.transaction():
            helper.assert_native_holds(recovered, result)
            state, version = adapter.PaymentTables(recovered).load_state("tenant-a")
            self.assertTrue(state["stopped"])
            self.assertEqual((state["records"], state["obligations"], version),
                             (initial["records"], initial["obligations"], 2))
            self.assertTrue(all(row["disabled"] for row in state["retries"]))

    def test_restore_never_impersonates_an_old_schema_for_native_hook(self):
        repository, _ = self.populated13()
        options = self.checkpoint(repository)
        repository.close()
        backup = self.directory / "backup.sqlite3"
        adapter.backup_store(self.path, backup)
        calls = []

        def old_hook(connection, *, version):
            calls.append((version, connection.execute("PRAGMA user_version").fetchone()[0]))
            raise RuntimeError("old hook rejects actual13")

        destination = self.directory / "never-created.sqlite3"
        with patch.object(self.native, "apply_restore_holds", old_hook):
            with self.assertRaises(Exception):
                adapter.restore_store(backup, destination, **options)
        self.assertEqual(calls, [(13, 13)])
        self.assertFalse(destination.exists())

    def test_missing_stale_current_authority_and_newer_exposure_never_publish(self):
        repository, state = self.populated13()
        options = self.checkpoint(repository)
        backup = self.directory / "backup.sqlite3"
        adapter.backup_store(self.path, backup)
        destination = self.directory / "never-created.sqlite3"
        with self.assertRaisesRegex(adapter.RepositoryError, "current_authority_must_be_quiesced"):
            adapter.restore_store(backup, destination, **options)
        with repository.lock, repository.transaction():
            adapter.PaymentTables(repository).save_state("tenant-a", with_request(state, "new-earned-job"), 1)
        repository.close()
        with self.assertRaises(Exception):
            adapter.restore_store(backup, destination)
        with self.assertRaises(Exception):
            adapter.restore_store(backup, destination, **options)
        # A fresh current checkpoint cannot authorize rewinding a newer hold.
        repository = self.Native(self.path)
        fresh_options = self.checkpoint(repository)
        repository.close()
        with self.assertRaises(Exception):
            adapter.restore_store(backup, destination, **fresh_options)
        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
