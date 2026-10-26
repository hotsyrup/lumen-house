"""Synthetic schema13 maintenance checks; no live stores or scheduling."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from . import maintenance, maintenance_cli, repository as adapter
from . import test_repository as storage_fixtures
from ..controls.repository import ExchangeTables
from ..controls.recovery_fence import validate_checkpoint_file, make_checkpoint
from ..controls.test_repository import recovery_fixture


class MaintenanceDefaultTests(unittest.TestCase):
    def test_default_runner_and_cli_do_not_open_or_query_stores(self):
        class NoDatabase:
            def __getattribute__(self, name):
                raise AssertionError("disabled database access")
        runner = maintenance.MaintenanceRunner(NoDatabase())
        self.assertEqual(runner.run_once(), {"enabled": False, "ran": False, "installed": False})
        output = io.StringIO()
        with patch.object(maintenance_cli, "native_repository_class", side_effect=AssertionError("opener called")):
            with contextlib.redirect_stdout(output):
                self.assertEqual(maintenance_cli.main([]), 0)
        self.assertEqual(json.loads(output.getvalue()), {"enabled": False, "ran": False, "installed": False})

    def test_private_config_is_required_but_config_or_execute_alone_do_nothing(self):
        with tempfile.TemporaryDirectory(prefix="payment-maintenance-config-") as folder:
            path = Path(folder) / "owner-private.json"
            value = {"format": maintenance_cli.FORMAT, "enabled": True, "storePath": str(Path(folder) / "absent.sqlite3"),
                     "checkpointDirectory": str(Path(folder) / "checkpoints"), "cadenceMs": 60000}
            path.write_text(json.dumps(value))
            path.chmod(0o600)
            with patch.object(maintenance_cli, "native_repository_class", side_effect=AssertionError("opener called")):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(maintenance_cli.main(["--config", str(path)]), 0)
                    value["enabled"] = False
                    path.write_text(json.dumps(value))
                    self.assertEqual(maintenance_cli.main(["--config", str(path), "--execute"]), 0)
            self.assertFalse(Path(value["storePath"]).exists())
            path.chmod(0o644)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(maintenance_cli.main(["--config", str(path), "--execute"]), 1)
            self.assertEqual(json.loads(output.getvalue())["error"], "maintenance_failed")

    def test_ambiguous_failure_never_claims_no_mutation_or_checkpoint_success(self):
        config = {"format": maintenance_cli.FORMAT, "enabled": True, "storePath": "/synthetic/store",
                  "checkpointDirectory": "/synthetic/checkpoints", "cadenceMs": 1}
        class Repository:
            closed = False
            def close(self):
                self.closed = True
        repository = Repository()
        with patch.object(maintenance_cli, "load_configuration", return_value=config), \
             patch.object(maintenance_cli, "native_repository_class", return_value=lambda path: repository), \
             patch.object(maintenance_cli.MaintenanceRunner, "run_once", side_effect=OSError("private path")):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(maintenance_cli.main(["--config", "synthetic", "--execute"]), 1)
        self.assertTrue(repository.closed)
        self.assertEqual(json.loads(output.getvalue()), {"error": "maintenance_failed", "ran": False,
            "checkpointConfirmed": False, "mutationOutcome": "may_have_committed"})


@unittest.skipUnless(os.environ.get("LUMEN_HOUSE_NATIVE_SOURCE"), "explicit installed House source required")
class NativeMaintenanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        storage_fixtures.NativePaymentRepositoryTests.setUpClass()

    def setUp(self):
        helper = storage_fixtures.NativePaymentRepositoryTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        self.helper = helper

    def populated(self):
        repository, payment = self.helper.populated13()
        state, _ = recovery_fixture()
        # The complete record is erased after its existing selected retention
        # interval; delivered unresolved content remains on a retention hold.
        instant = max(row["expiresAt"] for row in state["records"].values()) + 1
        with repository.lock, repository.transaction():
            db = repository.connection
            db.execute("INSERT INTO principals VALUES('fixture-owner','owner',1,'2026-01-01T00:00:00Z')")
            db.execute("INSERT INTO x_exchange_tenants VALUES('fixture-tenant','fixture-owner')")
            ExchangeTables(repository).save_state("fixture-tenant", state, 0)
        return repository, payment, instant

    def test_retention_sweep_preserves_payment_holds_and_publishes_exact13_checkpoint(self):
        repository, payment, instant = self.populated()
        self.addCleanup(repository.close)
        directory = self.helper.directory / "maintenance-checkpoints"
        directory.mkdir(mode=0o700)
        runner = maintenance.MaintenanceRunner(repository, enabled=True, cadence_ms=60000, checkpoint_directory=directory)
        result = runner.run_once(now=instant)
        self.assertEqual((result["removed"], result["unresolvedRetentionHolds"]), (1, 1))
        self.assertEqual((result["enabled"], result["ran"], result["installed"]), (True, True, False))
        with repository.lock, repository.transaction():
            self.assertEqual(adapter.PaymentTables(repository).load_state("tenant-a"), (payment, 1))
            state, _ = ExchangeTables(repository).load_state("fixture-tenant")
            self.assertEqual(len(state["records"]), 1)
            document, encoded = make_checkpoint(repository.connection, adapter.SCHEMA13_SHA256, schema_version=13)
        validate_checkpoint_file(directory / result["checkpointFile"], result["pin"], document, encoded)
        self.assertEqual(document["schemaVersion"], 13)
        self.assertEqual(runner.run_once(now=instant + 1), {"enabled": True, "ran": False, "reason": "cadence_not_due"})

    def test_failed_publication_keeps_committed_erasure_journal_and_payment_holds(self):
        repository, payment, instant = self.populated()
        self.addCleanup(repository.close)
        runner = maintenance.MaintenanceRunner(repository, enabled=True, cadence_ms=60000, checkpoint_directory=self.helper.directory)
        with patch.object(maintenance, "export_checkpoint", side_effect=OSError("synthetic publication failure")):
            with self.assertRaises(OSError):
                runner.run_once(now=instant)
        self.assertIsNone(runner.last_completed)
        with repository.lock, repository.transaction():
            self.assertEqual(repository.connection.execute("SELECT COUNT(*) FROM x_exchange_erasures").fetchone()[0], 1)
            self.assertEqual(adapter.PaymentTables(repository).load_state("tenant-a"), (payment, 1))

    def test_cli_exact13_opener_and_native_writer_lease_without_migration(self):
        self.helper.at12()
        before = self.helper.path.read_bytes()
        directory = self.helper.directory / "checkpoints"
        directory.mkdir(mode=0o700)
        config = self.helper.directory / "owner-private.json"
        config.write_text(json.dumps({"format": maintenance_cli.FORMAT, "enabled": True,
            "storePath": str(self.helper.path), "checkpointDirectory": str(directory), "cadenceMs": 60000}))
        config.chmod(0o600)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(maintenance_cli.main(["--config", str(config), "--execute"]), 1)
        self.assertEqual(self.helper.path.read_bytes(), before)
        repository = self.helper.Native(self.helper.path, migrate=True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(maintenance_cli.main(["--config", str(config), "--execute"]), 1)
        repository.close()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(maintenance_cli.main(["--config", str(config), "--execute"]), 0)
        result = json.loads(output.getvalue())
        self.assertTrue(result["ran"])
        self.assertEqual(result["removed"], 0)
        self.assertEqual(adapter.check_store(self.helper.path)["schema_version"], 13)


if __name__ == "__main__":
    unittest.main()
