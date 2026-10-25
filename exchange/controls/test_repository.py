"""Offline repository tests. Native integration is an explicit installed-source run.

Set LUMEN_HOUSE_NATIVE_SOURCE to the existing House Python source directory to
run native migration/recovery checks. Only temporary synthetic databases open.
"""
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest

from .repository import DDL, ExchangeTables, MAX_STATE_BYTES, RepositoryError, TABLES


def recovery_fixture():
    from .core import apply_operation, empty_state
    now = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    state = empty_state("fixture-tenant")
    scopes = ["exchange:read", "exchange:write"]
    owner = {"tenantId": "fixture-tenant", "subjectId": "fixture-owner", "role": "owner", "scopes": scopes + ["exchange:admin"]}
    a = {"tenantId": "fixture-tenant", "subjectId": "fixture-requester", "role": "agent", "scopes": scopes}
    b = {**a, "subjectId": "fixture-provider"}
    count = 0

    def command(actor, action, payload=None, record=None):
        nonlocal state, count
        count += 1
        request = {"action": action, "input": payload or {}}
        if record is not None:
            request.update(id=record["id"], expectedVersion=record["version"])
        result = apply_operation(state, {"kind": "command", "principal": actor, "command": request, "key": "fixture-" + str(count)}, now=now, id="fixture-proposal-" + str(count))
        state = result["state"]
        return result["result"]

    command(owner, "configure", {"limits": {"retentionMs": 86400000, "reservationMs": 600000, "maxRecords": 10}, "resources": {"fixture-review": 3}, "grants": [{"subjectId": actor["subjectId"], "resources": ["fixture-review"], "maxUnits": 3} for actor in (a, b)]})
    command(owner, "open")
    held = None
    for complete in (True, False):
        record = command(a, "propose", {"providerId": b["subjectId"], "sharingConsent": True, "terms": {"description": "Synthetic review", "deliverable": "Synthetic manifest", "acceptance": "Owner verifies hashes", "cancellation": "Dispute after delivery", "resourceId": "fixture-review", "units": 1}})
        for actor in (a, b):
            record = command(actor, "assent", {"termsRevision": record["termsRevision"], "termsHash": record["termsHash"], "sharingConsent": True}, record)
        record = command(owner, "approve", {"termsRevision": record["termsRevision"], "termsHash": record["termsHash"]}, record)
        record = command(b, "deliver", {"evidence": {"sha256": "c" * 64, "description": "Synthetic digest"}}, record)
        if complete:
            command(owner, "accept", {"deliveryRevision": record["evidence"]["deliveryRevision"], "deliveryHash": record["evidence"]["deliveryHash"], "assessment": "Synthetic verification"}, record)
        else:
            held = record["id"]
    return state, held


class Fixture:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("CREATE TABLE principals(id TEXT PRIMARY KEY,role TEXT,active INTEGER)")
        self.connection.execute("INSERT INTO principals VALUES('fixture-owner','owner',1)")
        for statement in DDL:
            self.connection.execute(statement)
        self.connection.execute("PRAGMA user_version=12")
        self.connection.execute("INSERT INTO x_exchange_tenants VALUES('fixture-tenant','fixture-owner')")
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


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.repository = Fixture()
        self.tables = ExchangeTables(self.repository)
        self.addCleanup(self.repository.connection.close)

    def test_requires_existing_native_transaction(self):
        with self.assertRaisesRegex(RepositoryError, "native_transaction_required"):
            self.tables.load_state("fixture-tenant")

    def test_default_grants_nothing_and_absent_state_is_explicit(self):
        with self.repository.transaction():
            self.assertEqual(self.tables.load_state("fixture-tenant"), (None, 0))
            self.assertIsNone(self.tables.membership("fixture-tenant", "fixture-owner"))
            self.assertIsNone(self.tables.tenant("other-tenant"))
            self.assertEqual(self.tables.tenant("fixture-tenant")["owner_id"], "fixture-owner")

    def test_compare_and_swap_preserves_one_atomic_state(self):
        with self.repository.transaction():
            self.assertEqual(self.tables.save_state("fixture-tenant", {"available": 1}, 0), 1)
        with self.repository.transaction():
            self.assertEqual(self.tables.save_state("fixture-tenant", {"available": 0}, 1), 2)
        for old in (0, 1):
            with self.assertRaisesRegex(RepositoryError, "exchange_state_conflict"):
                with self.repository.transaction():
                    self.tables.save_state("fixture-tenant", {"available": 99}, old)
        with self.repository.transaction():
            self.assertEqual(self.tables.load_state("fixture-tenant"), ({"available": 0}, 2))

    def test_rollback_does_not_partially_commit_state_or_revision(self):
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            with self.repository.transaction():
                self.tables.save_state("fixture-tenant", {"reserved": 1}, 0)
                raise RuntimeError("interrupted")
        with self.repository.transaction():
            self.assertEqual(self.tables.load_state("fixture-tenant"), (None, 0))

    def test_state_is_bounded_by_utf8_bytes_and_exact_json_type(self):
        for value in ([1], {"text": "é" * MAX_STATE_BYTES}, {"float": float("nan")}):
            with self.assertRaises(RepositoryError):
                with self.repository.transaction():
                    self.tables.save_state("fixture-tenant", value, 0)
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.connection.execute("INSERT INTO x_exchange_state VALUES('fixture-tenant','[]',1)")

    def test_forged_tenant_owner_and_membership_owner_are_rejected(self):
        db = self.repository.connection
        db.execute("INSERT INTO principals VALUES('fixture-buyer','buyer',1)")
        for sql in (
            "INSERT INTO x_exchange_tenants VALUES('forged','fixture-buyer')",
            "UPDATE x_exchange_tenants SET owner_id='fixture-buyer'",
            "DELETE FROM x_exchange_tenants",
            "INSERT INTO x_exchange_memberships VALUES('fixture-tenant','fixture-buyer','owner','owner','[]',1,NULL)",
            "INSERT INTO x_exchange_memberships VALUES('fixture-tenant','fixture-owner','agent','buyer','[]',1,NULL)",
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(sql)

    def test_membership_scopes_are_exact_and_owner_admin_is_distinct(self):
        db = self.repository.connection
        for scopes in ('["exchange:execute"]', '["exchange:read","exchange:read"]', '[true]', '["exchange:admin"]'):
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO x_exchange_memberships VALUES('fixture-tenant','fixture-agent','agent','buyer',?,1,NULL)", (scopes,))
        db.execute("INSERT INTO x_exchange_memberships VALUES('fixture-tenant','fixture-owner','owner','owner','[\"exchange:admin\"]',1,NULL)")
        with self.repository.transaction():
            row = self.tables.membership("fixture-tenant", "fixture-owner")
            self.assertIsNone(row["expires_at"])
            self.assertEqual(row["scopes"], ["exchange:admin"])
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE x_exchange_memberships SET principal_id='replacement'")

    def test_optional_deadline_is_integer_ms_not_automatic_expiry(self):
        db = self.repository.connection
        for expiry in (1.5, "tomorrow", -1):
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO x_exchange_memberships VALUES('fixture-tenant','fixture-agent','agent','buyer','[]',1,?)", (expiry,))
        db.execute("INSERT INTO x_exchange_memberships VALUES('fixture-tenant','fixture-agent','agent','buyer','[]',1,2000000000000)")
        with self.repository.transaction():
            self.assertEqual(self.tables.membership("fixture-tenant", "fixture-agent")["expires_at"], 2000000000000)


@unittest.skipUnless(os.environ.get("LUMEN_HOUSE_NATIVE_SOURCE"), "explicit installed House source required for native fixture checks")
class NativeRepositoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(os.environ["LUMEN_HOUSE_NATIVE_SOURCE"]).resolve()
        if not (source / "fulfillment_workflow.py").is_file():
            raise ValueError("existing native source directory required")
        sys.path.insert(0, str(source))
        import fulfillment_workflow
        from . import repository
        cls.native, cls.adapter = fulfillment_workflow, repository
        cls.Native = repository.native_repository_class()

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="exchange-native-test-")
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "synthetic.sqlite3"
        base = self.native.FulfillmentRepository(self.path)
        with base.lock, base.transaction():
            base.connection.execute("INSERT INTO principals VALUES('fixture-owner','owner',1,'2026-01-01T00:00:00Z')")
            base.connection.execute("INSERT INTO r_providers VALUES('fixture-provider',1,'2026-01-01T00:00:00Z')")
        base.close()

    def native_rows(self):
        db = sqlite3.connect(self.path)
        try:
            names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'x_exchange_%' AND name!='schema_migrations'")]
            return {name: db.execute('SELECT * FROM "' + name + '"').fetchall() for name in names}
        finally:
            db.close()

    def test_startup_refuses_v11_without_any_store_mutation(self):
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(RepositoryError, "explicit_exchange_migration_required"):
            self.Native(self.path)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)
        db = sqlite3.connect(self.path)
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 11)
        db.close()

    def test_explicit_additive_migration_preserves_every_native_row_and_reopens(self):
        before = self.native_rows()
        repository = self.Native(self.path, migrate=True)
        with repository.lock, repository.transaction():
            self.assertEqual([repository.connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] for table in TABLES], [0, 0, 0])
            self.assertEqual(repository.connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 12)
        repository.close()
        self.assertEqual(self.native_rows(), before)
        repository = self.Native(self.path)
        repository.close()
        self.assertEqual(self.adapter.check_store(self.path)["schema_version"], 12)
        with self.assertRaises(Exception):
            self.native.FulfillmentRepository(self.path)

    def test_unknown_or_tampered_schema_fails_before_writable_open(self):
        repository = self.Native(self.path, migrate=True)
        repository.close()
        db = sqlite3.connect(self.path)
        db.execute("DROP TRIGGER x_exchange_tenant_identity")
        db.commit(); db.close()
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with self.assertRaises(Exception):
            self.Native(self.path)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)

    def test_future_version_and_missing_paths_never_create_or_repair(self):
        db = sqlite3.connect(self.path)
        db.execute("PRAGMA user_version=13")
        db.close()
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with self.assertRaises(RepositoryError):
            self.Native(self.path, migrate=True)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)
        missing = Path(self.folder.name) / "missing.sqlite3"
        with self.assertRaises(Exception):
            self.Native(missing, migrate=True)
        self.assertFalse(missing.exists())

    def test_backup_restore_preserves_content_and_revokes_all_access(self):
        repository = self.Native(self.path, migrate=True)
        tables = ExchangeTables(repository)
        with repository.lock, repository.transaction():
            db = repository.connection
            db.execute("INSERT INTO x_exchange_tenants VALUES('fixture-tenant','fixture-owner')")
            db.execute("INSERT INTO x_exchange_memberships VALUES('fixture-tenant','fixture-owner','owner','owner','[\"exchange:read\"]',1,NULL)")
            state, held = recovery_fixture()
            tables.save_state("fixture-tenant", state, 0)
            db.execute("INSERT INTO credentials(id,principal_id,token_hash,created_at,expires_at) VALUES('fixture-credential','fixture-owner',?,'2026-01-01T00:00:00Z','2099-01-01T00:00:00Z')", ("a" * 64,))
            db.execute("INSERT INTO r_credentials(id,principal_id,token_hash,created_at,expires_at) VALUES('fixture-provider-credential','fixture-provider',?,'2026-01-01T00:00:00Z','2099-01-01T00:00:00Z')", ("b" * 64,))
        repository.close()
        backup = Path(self.folder.name) / "backup.sqlite3"
        self.adapter.backup_store(self.path, backup)
        restored = Path(self.folder.name) / "restored.sqlite3"
        result = self.adapter.restore_store(backup, restored)
        self.assertTrue(result["financial_stop_latched"])
        self.assertTrue(result["exchange_stop_latched"])
        recovered = self.Native(restored)
        with recovered.lock, recovered.transaction():
            restored_tables = ExchangeTables(recovered)
            recovered_state, state_version = restored_tables.load_state("fixture-tenant")
            self.assertTrue(recovered_state["stopped"])
            self.assertEqual(state_version, 2)
            self.assertEqual(recovered_state["resources"]["fixture-review"], state["resources"]["fixture-review"])
            self.assertEqual(recovered_state["resources"]["fixture-review"]["used"], 1)
            self.assertEqual(recovered_state["resources"]["fixture-review"]["reserved"], 1)
            held_record = recovered_state["records"][held]
            self.assertEqual(held_record["status"], "disputed")
            self.assertEqual(held_record["assents"], {})
            self.assertIsNone(held_record["approved"])
            self.assertEqual(held_record["evidence"], state["records"][held]["evidence"])
            self.assertEqual(recovered_state["administrativeAudit"][-1]["action"], "offline-recovery")
            self.assertEqual(restored_tables.membership("fixture-tenant", "fixture-owner")["active"], 0)
            self.assertEqual(recovered.connection.execute("SELECT stopped FROM wf_controls WHERE id=1").fetchone()[0], 1)
            for name in ("credentials", "r_credentials"):
                self.assertIsNotNone(recovered.connection.execute("SELECT revoked_at FROM " + name).fetchone()[0])
        recovered.close()
        with self.assertRaises(Exception):
            self.adapter.restore_store(backup, restored)
        self.adapter.check_store(self.path)

    def test_corrupt_backup_manifest_cannot_publish_a_restored_file(self):
        repository = self.Native(self.path, migrate=True)
        repository.close()
        backup = Path(self.folder.name) / "backup.sqlite3"
        self.adapter.backup_store(self.path, backup)
        manifest = Path(str(backup) + ".manifest.json")
        content = json.loads(manifest.read_text())
        content["sha256"] = "0" * 64
        manifest.write_text(json.dumps(content))
        destination = Path(self.folder.name) / "never-created.sqlite3"
        with self.assertRaises(Exception):
            self.adapter.restore_store(backup, destination)
        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
