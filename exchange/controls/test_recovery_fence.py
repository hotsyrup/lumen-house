"""Policy-independent expiry/erasure recovery fixtures; no real data or grants."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

from . import repository as storage
from .core import apply_operation
from .maintenance import MaintenanceRunner, export_checkpoint
from .maintenance_cli import load_configuration, main
from .recovery_fence import FenceError, journal, make_checkpoint, reconcile_erased_content, validate_checkpoint_file
from .test_core import Harness, OWNER
from .test_service import FixtureRepository


class FenceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='exchange-fence-test-')
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.directory = self.root / 'checkpoints'
        self.directory.mkdir(mode=0o700)
        self.repo = FixtureRepository()
        self.addCleanup(self.repo.connection.close)
        self.tables = storage.ExchangeTables(self.repo)
        self.h = Harness()
        self.record = self.h.propose()
        self.h.command('cancel', OWNER, self.record, {'reason': 'Synthetic terminal fixture'})
        with self.repo.transaction():
            self.repo.connection.execute("INSERT INTO x_exchange_tenants VALUES('tenant-a','owner')")
            self.tables.save_state('tenant-a', self.h.state, 0)

    def delete(self):
        record = self.h.get(self.record)
        self.h.command('delete', OWNER, record, {'reason': 'Synthetic erasure request'})
        with self.repo.transaction():
            _, version = self.tables.load_state('tenant-a')
            self.tables.save_state('tenant-a', self.h.state, version)

    def test_disabled_runner_and_no_config_cli_touch_no_repository(self):
        class NeverRead:
            def __getattribute__(self, name):
                raise AssertionError('Disabled maintenance touched storage')
        self.assertEqual(MaintenanceRunner(NeverRead()).run_once(), {'enabled': False, 'ran': False, 'installed': False})
        with mock.patch('builtins.print'):
            self.assertEqual(main([]), 0)
            self.assertEqual(main(['--execute']), 0)
        for args in ({}, {'cadence_ms': 1}, {'cadence_ms': True, 'checkpoint_directory': self.directory}):
            with self.assertRaises(ValueError):
                MaintenanceRunner(self.repo, enabled=True, **args)

    def test_explicit_config_is_owner_private_no_policy_values_are_invented(self):
        path = self.root / 'maintenance.json'
        document = {'format': 'lumen-house-exchange-maintenance/v1', 'enabled': False,
                    'storePath': None, 'checkpointDirectory': None, 'cadenceMs': None}
        path.write_text(json.dumps(document)); path.chmod(0o600)
        self.assertFalse(load_configuration(path)['enabled'])
        document['enabled'] = True
        path.write_text(json.dumps(document))
        with self.assertRaises(ValueError):
            load_configuration(path)
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            load_configuration(path)

    def test_idle_tenant_sweep_and_restart_are_idempotent_and_exchange_only(self):
        self.repo.connection.execute('CREATE TABLE ordinary_intake(id TEXT,expires_at TEXT)')
        self.repo.connection.execute("INSERT INTO ordinary_intake VALUES('fictional-existing-application',NULL)")
        before = self.repo.connection.execute('SELECT * FROM ordinary_intake').fetchall()
        runner = MaintenanceRunner(self.repo, enabled=True, cadence_ms=1000, checkpoint_directory=self.directory)
        instant = self.h.now + self.h.state['limits']['retentionMs'] + 1
        result = runner.run_once(now=instant)
        self.assertEqual(result['removed'], 1)
        self.assertEqual(result['pin']['erasureSequence'], 1)
        self.assertFalse(runner.run_once(now=instant + 1)['ran'])
        restarted = MaintenanceRunner(self.repo, enabled=True, cadence_ms=1000, checkpoint_directory=self.directory)
        again = restarted.run_once(now=instant + 2000)
        self.assertEqual(again['removed'], 0)
        self.assertEqual(again['pin'], result['pin'])
        self.assertEqual(self.repo.connection.execute('SELECT * FROM ordinary_intake').fetchall(), before)
        with self.repo.transaction():
            state, _ = self.tables.load_state('tenant-a')
            self.assertEqual(state['records'], {})
            self.assertEqual(journal(self.repo.connection)['erasureSequence'], 1)

    def test_failed_checkpoint_publication_leaves_durable_erasure_and_stale_pin_rejected(self):
        prior = export_checkpoint(self.repo, self.directory)
        runner = MaintenanceRunner(self.repo, enabled=True, cadence_ms=1000, checkpoint_directory=self.directory)
        instant = self.h.now + self.h.state['limits']['retentionMs'] + 1
        with mock.patch('exchange.controls.maintenance.publish_checkpoint', side_effect=OSError('synthetic fsync failure')):
            with self.assertRaises(OSError):
                runner.run_once(now=instant)
        self.assertIsNone(runner.last_completed)
        with self.repo.transaction():
            state, _ = self.tables.load_state('tenant-a')
            self.assertEqual(state['records'], {})
            doc, raw = make_checkpoint(self.repo.connection, storage.SCHEMA12_SHA256)
            with self.assertRaises(FenceError):
                validate_checkpoint_file(self.directory / prior['checkpointFile'], prior['pin'], doc, raw)
        completed = runner.run_once(now=instant + 1)
        self.assertEqual(completed['pin']['erasureSequence'], 1)

    def test_erasure_receipt_and_state_rollback_together_on_commit_failure(self):
        before = deepcopy(self.h.state)
        record = self.h.get(self.record)
        self.h.command('delete', OWNER, record, {'reason': 'Fictional interrupted delete'})
        with self.assertRaises(RuntimeError):
            with self.repo.transaction():
                self.tables.save_state('tenant-a', self.h.state, 1)
                self.assertEqual(journal(self.repo.connection)['erasureSequence'], 1)
                raise RuntimeError('Synthetic interruption')
        with self.repo.transaction():
            self.assertEqual(self.tables.load_state('tenant-a')[0], before)
            self.assertEqual(journal(self.repo.connection)['erasureSequence'], 0)

    def test_prior_backup_erasure_reconciles_before_content_can_return(self):
        old = sqlite3.connect(':memory:', isolation_level=None)
        self.addCleanup(old.close)
        self.repo.connection.backup(old)
        self.delete()
        with self.repo.transaction():
            document, encoded = make_checkpoint(self.repo.connection, storage.SCHEMA12_SHA256)
        old.execute('BEGIN IMMEDIATE')
        removed = reconcile_erased_content(old, document)
        self.assertEqual(removed, 1)
        state = json.loads(old.execute('SELECT body_json FROM x_exchange_state').fetchone()[0])
        self.assertEqual(state['records'], {})
        self.assertFalse(any(request['result'] is not None and request['proposalId'] == self.record['id'] for request in state['requests']))
        self.assertEqual(journal(old)['erasureSequence'], 1)
        self.assertNotIn('One fictional review', encoded.decode())
        self.assertNotIn(self.record['id'], encoded.decode())
        old.commit()
        with self.repo.transaction():
            _, version = self.tables.load_state('tenant-a')
            with self.assertRaises(FenceError):
                self.tables.save_state('tenant-a', {**self.h.state, 'records': {self.record['id']: self.record}}, version)
            # Even a caller catching the error inside its outer transaction
            # cannot commit a half-written state or erase receipt.
            self.assertEqual(self.tables.load_state('tenant-a')[0]['records'], {})

    def test_bad_pin_tampered_manifest_scope_and_journal_fail_closed(self):
        checkpoint = export_checkpoint(self.repo, self.directory)
        path = self.directory / checkpoint['checkpointFile']
        with self.repo.transaction():
            document, encoded = make_checkpoint(self.repo.connection, storage.SCHEMA12_SHA256)
        for pin in (None, {}, {**checkpoint['pin'], 'erasureSequence': 99},
                    {**checkpoint['pin'], 'erasureSequence': False}, {**checkpoint['pin'], 'lineage': '0' * 32}):
            with self.assertRaises(FenceError):
                validate_checkpoint_file(path, pin, document, encoded)
        path.write_bytes(encoded[:-1])
        with self.assertRaises(FenceError):
            validate_checkpoint_file(path, checkpoint['pin'], document, encoded)
        self.delete()
        self.repo.connection.execute("UPDATE x_exchange_recovery SET erasure_head=?", ('0' * 64,))
        with self.assertRaises(FenceError):
            journal(self.repo.connection)

    def test_checkpoint_output_cannot_reuse_insecure_existing_file(self):
        checkpoint = export_checkpoint(self.repo, self.directory)
        path = self.directory / checkpoint['checkpointFile']
        path.chmod(0o644)
        with self.assertRaises(FenceError):
            export_checkpoint(self.repo, self.directory)


@unittest.skipUnless(os.environ.get('LUMEN_HOUSE_NATIVE_SOURCE'), 'explicit current native source required for recovery-fence integration')
class NativeFenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(Path(os.environ['LUMEN_HOUSE_NATIVE_SOURCE']).resolve()))
        import fulfillment_workflow as native
        cls.native = native
        cls.Native = storage.native_repository_class()

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='exchange-native-erasure-')
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.path = self.root / 'current.sqlite3'
        original = self.native.FulfillmentRepository(self.path)
        with original.transaction():
            original.connection.execute("INSERT INTO principals VALUES('owner','owner',1,'2026-01-01T00:00:00Z')")
        original.close()
        self.repo = self.Native(self.path, migrate=True)
        self.addCleanup(self.repo.close)
        self.tables = storage.ExchangeTables(self.repo)
        self.h = Harness()
        self.record = self.h.propose()
        self.h.command('cancel', OWNER, self.record, {'reason': 'Fictional terminal record'})
        with self.repo.transaction():
            self.repo.connection.execute("INSERT INTO x_exchange_tenants VALUES('tenant-a','owner')")
            self.tables.save_state('tenant-a', self.h.state, 0)
        self.directory = self.root / 'checkpoints'
        self.directory.mkdir(mode=0o700)
        self.backup = self.root / 'before-delete.sqlite3'
        storage.backup_store(self.path, self.backup)

    def options(self):
        checkpoint = export_checkpoint(self.repo, self.directory)
        return {'authority_store': self.path, 'checkpoint_path': self.directory / checkpoint['checkpointFile'], 'checkpoint_pin': checkpoint['pin']}

    def delete(self):
        self.h.command('delete', OWNER, self.h.get(self.record), {'reason': 'Fictional deletion after backup'})
        with self.repo.transaction():
            self.tables.save_state('tenant-a', self.h.state, 1)

    def test_old_backup_cannot_resurrect_deleted_payload_or_cached_response(self):
        self.delete()
        options = self.options()
        self.repo.close()
        destination = self.root / 'reconciled.sqlite3'
        result = storage.restore_store(self.backup, destination, **options)
        self.assertEqual(result['erased_records_reconciled'], 1)
        recovered = self.Native(destination)
        self.addCleanup(recovered.close)
        with recovered.transaction():
            state, _ = storage.ExchangeTables(recovered).load_state('tenant-a')
            self.assertTrue(state['stopped'])
            self.assertEqual(state['records'], {})
            self.assertTrue(all(request['result'] is None for request in state['requests']))
            self.assertEqual(journal(recovered.connection)['erasureSequence'], 1)
        self.assertTrue(result['erasure_authority_verified'])

    def test_missing_or_stale_checkpoint_and_unavailable_current_source_publish_nothing(self):
        stale = self.options()
        self.delete()
        current = self.options()
        self.repo.close()
        for options in ({}, stale, {**current, 'authority_store': self.root / 'lost.sqlite3'},
                        {**current, 'authority_store': self.backup}):
            destination = self.root / 'never.sqlite3'
            with self.assertRaises(Exception):
                storage.restore_store(self.backup, destination, **options)
            self.assertFalse(destination.exists())

    def test_active_current_writer_is_not_bypassed(self):
        options = self.options()
        destination = self.root / 'locked.sqlite3'
        with self.assertRaisesRegex(storage.RepositoryError, 'current_authority_must_be_quiesced'):
            storage.restore_store(self.backup, destination, **options)
        self.assertFalse(destination.exists())

    def test_new_business_state_or_obligation_cannot_be_lost_to_old_backup(self):
        # A new delivery/reservation after the backup must block recovery, even
        # when a freshly exported erasure pin is internally consistent.
        self.h.delivered()
        with self.repo.transaction():
            self.tables.save_state('tenant-a', self.h.state, 1)
        options = self.options()
        self.repo.close()
        destination = self.root / 'missing-obligation.sqlite3'
        with self.assertRaisesRegex(FenceError, 'restore_requires_current_state_reconciliation'):
            storage.restore_store(self.backup, destination, **options)
        self.assertFalse(destination.exists())

    def test_erased_completed_payload_does_not_erase_consumed_units(self):
        delivered = self.h.delivered()
        completed = self.h.command('accept', OWNER, delivered, {'deliveryRevision': 1,
            'deliveryHash': delivered['evidence']['deliveryHash'], 'assessment': 'Fictional independent verification'})
        with self.repo.transaction():
            self.tables.save_state('tenant-a', self.h.state, 1)
        backup = self.root / 'consumed-before-delete.sqlite3'
        storage.backup_store(self.path, backup)
        self.h.command('delete', OWNER, completed, {'reason': 'Erase fictional evidence'})
        with self.repo.transaction():
            self.tables.save_state('tenant-a', self.h.state, 2)
        options = self.options()
        self.repo.close()
        destination = self.root / 'consumed-recovered.sqlite3'
        storage.restore_store(backup, destination, **options)
        recovered = self.Native(destination)
        self.addCleanup(recovered.close)
        with recovered.transaction():
            state, _ = storage.ExchangeTables(recovered).load_state('tenant-a')
            self.assertNotIn(completed['id'], state['records'])
            self.assertEqual(state['resources']['synthetic-review']['used'], 1)
            self.assertEqual(state['resources']['synthetic-review']['reserved'], 0)

    def test_wrong_lineage_and_divergent_journal_publish_no_destination(self):
        self.delete()
        checkpoint = self.options()
        self.repo.close()
        # A different legitimate authority has a different immutable lineage.
        other_path = self.root / 'other-source.sqlite3'
        other = self.native.FulfillmentRepository(other_path)
        other.close()
        other = self.Native(other_path, migrate=True)
        try:
            other_checkpoint = export_checkpoint(other, self.directory)
        finally:
            other.close()
        wrong = {'authority_store': other_path,
                 'checkpoint_path': self.directory / other_checkpoint['checkpointFile'],
                 'checkpoint_pin': other_checkpoint['pin']}
        destination = self.root / 'wrong-lineage.sqlite3'
        with self.assertRaisesRegex(FenceError, 'restore_lineage_or_cursor_mismatch'):
            storage.restore_store(self.backup, destination, **wrong)
        self.assertFalse(destination.exists())
        # A journal header edited independently of its receipt chain is refused
        # during current-authority preflight, before destination publication.
        db = sqlite3.connect(self.path)
        db.execute("UPDATE x_exchange_recovery SET erasure_head=?", ('f' * 64,))
        db.commit(); db.close()
        with self.assertRaises(FenceError):
            storage.restore_store(self.backup, destination, **checkpoint)
        self.assertFalse(destination.exists())

    def test_new_native_business_rows_are_not_silently_rolled_back(self):
        with self.repo.transaction():
            self.repo.connection.execute("INSERT INTO principals VALUES('new-native-buyer','buyer',1,'2026-01-01T00:00:00Z')")
        options = self.options()
        self.repo.close()
        destination = self.root / 'older-native-state.sqlite3'
        with self.assertRaisesRegex(FenceError, 'restore_requires_native_business_reconciliation'):
            storage.restore_store(self.backup, destination, **options)
        self.assertFalse(destination.exists())

    def test_explicit_terminal_runner_requires_quiesced_native_writer(self):
        config = self.root / 'maintenance.json'
        config.write_text(json.dumps({'format': 'lumen-house-exchange-maintenance/v1', 'enabled': True,
            'storePath': str(self.path), 'checkpointDirectory': str(self.directory), 'cadenceMs': 1000}))
        config.chmod(0o600)
        with mock.patch('builtins.print') as output:
            self.assertEqual(main(['--config', str(config), '--execute']), 1)
        denied = json.loads(output.call_args.args[0])
        self.assertFalse(denied['checkpointConfirmed'])
        self.assertEqual(denied['mutationOutcome'], 'may_have_committed')
        self.assertEqual(list(self.directory.iterdir()), [])
        self.repo.close()
        with mock.patch('builtins.print') as output:
            self.assertEqual(main(['--config', str(config), '--execute']), 0)
        completed = json.loads(output.call_args.args[0])
        self.assertTrue(completed['ran'])
        self.assertFalse(completed['installed'])
        self.assertEqual(completed['pin']['erasureSequence'], 0)
        self.assertTrue((self.directory / completed['checkpointFile']).is_file())


if __name__ == '__main__':
    unittest.main()
