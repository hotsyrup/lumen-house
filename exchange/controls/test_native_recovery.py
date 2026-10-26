"""Non-empty native recovery regression, using explicitly supplied fixtures.

Both environment variables are required. Native source and its existing test
helpers remain outside this repository. All policies, identities and stores are
temporary synthetic fixtures. Inert execution/standing history rows test native
revocation preservation only; they are deliberately not financial authority.
"""
import hashlib
import importlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from . import repository as exchange


@unittest.skipUnless(os.environ.get('LUMEN_HOUSE_NATIVE_SOURCE') and os.environ.get('LUMEN_HOUSE_NATIVE_FIXTURES'),
                     'explicit native source and native fixture helpers required')
class NativeRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(os.environ['LUMEN_HOUSE_NATIVE_SOURCE']).resolve()
        fixtures = Path(os.environ['LUMEN_HOUSE_NATIVE_FIXTURES']).resolve()
        if not (source / 'fulfillment_workflow.py').is_file() or not all(
                (fixtures / name).is_file() for name in ('test_fulfillment_workflow.py', 'test_fulfillment_policy.py')):
            raise ValueError('exact existing native source and fixture helper directories required')
        sys.path[:0] = [str(source), str(fixtures)]
        cls.native = importlib.import_module('fulfillment_workflow')
        if Path(cls.native.__file__).resolve().parent != source:
            raise ValueError('native source precedence mismatch')
        cls.Fixture = importlib.import_module('test_fulfillment_workflow').WorkflowFixture
        cls.policy_helpers = importlib.import_module('test_fulfillment_policy')
        cls.policy_module = importlib.import_module('fulfillment_policy')

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='exchange-recovery-regression-')
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.path = self.root / 'synthetic.sqlite3'
        fixture = self.Fixture(str(self.path))
        self.addCleanup(fixture.repo.close)
        fixture.earn()
        fixture.authorize()
        fixture.prepare()
        document = self.policy_helpers.policy_document(fixture.owner.id, now=fixture.now)
        proof = self.policy_helpers.controlled_terminal_approval(fixture, document)
        with fixture.repo.transaction():
            db = fixture.repo.connection
            self.policy_module.OwnerPolicyStore(db).adopt(document, proof,
                authenticated_owner=fixture.owner.id, observed_at=fixture.stamp())
            for index in (1, 2):
                approval = self.native.digest('inert synthetic recovery approval ' + str(index))
                grant = self.native.digest('inert synthetic recovery standing grant ' + str(index))
                db.execute('INSERT INTO wf_execution_approvals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
                    'synthetic-execution-' + str(index), approval, fixture.owner.id,
                    fixture.id, fixture.manifest['manifest_id'], fixture.manifest['manifest_sha256'],
                    fixture.authority['authority_id'], document['policy_id'], self.native.digest(document),
                    self.native.digest('inert request ' + str(index)), 'synthetic-recovery-key-' + str(index),
                    '{"fixture_only":true}', self.native.digest('inert approval receipt ' + str(index)),
                    '{"origin":"synthetic_recovery_fixture"}', 'synthetic', fixture.stamp(), fixture.stamp(3600)))
                db.execute('INSERT INTO wf_standing_grants VALUES(?,?,?,?,?,?,?,?)', (
                    grant, 'synthetic-standing-' + str(index), fixture.owner.id, document['policy_id'],
                    '{"fixture_only":true}', '{"origin":"synthetic_recovery_fixture"}', fixture.stamp(), fixture.stamp(3600)))
                if index == 2:
                    db.execute("INSERT INTO wf_execution_revocations VALUES(?,?,'owner_revoked',?)", (approval, fixture.owner.id, fixture.stamp()))
                    db.execute("INSERT INTO wf_standing_revocations VALUES(?,?,'owner_revoked',?)", (grant, fixture.owner.id, fixture.stamp()))
            self.retained = {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table)]
                             for table in ('wf_engagements', 'wf_manifests', 'wf_execution_approvals', 'wf_standing_grants')}
        self.document, self.owner, self.stamp = document, fixture.owner.id, fixture.stamp()
        fixture.repo.close()

    def assert_native_holds(self, repository, result):
        db = repository.connection
        self.assertEqual(db.execute('SELECT COUNT(*) FROM wf_owner_policy_revocations').fetchone()[0], 1)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM wf_execution_revocations').fetchone()[0], 2)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM wf_standing_revocations').fetchone()[0], 2)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM wf_execution_revocations WHERE reason='owner_revoked'").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM wf_standing_revocations WHERE reason='owner_revoked'").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM wf_control_gaps WHERE error_code='restore_snapshot'").fetchone()[0], 1)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM wf_authorities WHERE revoked_at IS NULL').fetchone()[0], 0)
        self.assertEqual(db.execute('SELECT stopped FROM wf_controls WHERE id=1').fetchone()[0], 1)
        for table in ('credentials', 'r_credentials'):
            self.assertEqual(db.execute('SELECT COUNT(*) FROM ' + table + ' WHERE revoked_at IS NULL').fetchone()[0], 0)
        for table, rows in self.retained.items():
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM ' + table)], rows, table)
        with self.assertRaises(self.policy_module.PolicyError) as raised:
            self.policy_module.OwnerPolicyStore(db).assert_current(self.document['policy_id'],
                authenticated_owner=self.owner, observed_at=self.stamp,
                source_build_sha256=self.document['source_build_sha256'])
        self.assertEqual(str(raised.exception), 'owner_policy_revoked')
        self.assertTrue(result['financial_stop_latched'])
        self.assertFalse(result['financial_execution_enabled'])
        self.assertEqual(result['restored_authorities_revoked'], 1)
        self.assertEqual(result['restored_standing_grants_revoked'], 1)
        self.assertEqual(result['restore_audit_gap_workflows'], 1)

    def prepared_exchange_backup(self):
        from .maintenance import export_checkpoint
        Native = exchange.native_repository_class()
        repository = Native(self.path, migrate=True)
        directory = self.root / 'checkpoints'
        directory.mkdir(mode=0o700)
        checkpoint = export_checkpoint(repository, directory)
        repository.close()
        backup = self.root / 'backup12.sqlite3'
        exchange.backup_store(self.path, backup)
        return Native, backup, {'authority_store': self.path,
            'checkpoint_path': directory / checkpoint['checkpointFile'], 'checkpoint_pin': checkpoint['pin']}

    def test_schema11_native_restore_preserves_all_native_recovery_fences(self):
        backup, destination = self.root / 'backup11.sqlite3', self.root / 'restore11.sqlite3'
        self.native.backup_store(self.path, backup)
        result = self.native.restore_store(backup, destination)
        repository = self.native.FulfillmentRepository(destination)
        self.addCleanup(repository.close)
        self.assert_native_holds(repository, result)

    def test_schema12_restore_uses_complete_native_hold_hook(self):
        self.assertTrue(callable(getattr(self.native, 'apply_restore_holds', None)),
                        'adoption requires the reviewed reusable native restore hook')
        Native, backup, options = self.prepared_exchange_backup()
        destination = self.root / 'restore12.sqlite3'
        result = exchange.restore_store(backup, destination, **options)
        repository = Native(destination)
        self.addCleanup(repository.close)
        self.assert_native_holds(repository, result)

    def test_missing_native_hook_refuses_restore_without_publishing_destination(self):
        Native, backup, options = self.prepared_exchange_backup()
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        destination = self.root / 'missing-hook.sqlite3'
        with mock.patch.object(self.native, 'apply_restore_holds', None, create=True):
            with self.assertRaises(exchange.RepositoryError):
                exchange.restore_store(backup, destination, **options)
        self.assertFalse(destination.exists())
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)


if __name__ == '__main__':
    unittest.main()
