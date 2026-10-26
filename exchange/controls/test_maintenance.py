import unittest

from .core import apply_operation, empty_state
from .maintenance import purge_expired
from .repository import ExchangeTables
from .test_service import FixtureRepository, NOW
from .test_core import Harness, OWNER


class MaintenanceTests(unittest.TestCase):
    def test_trusted_sweep_deletes_expired_content_but_reports_disputed_holds(self):
        repo = FixtureRepository()
        self.addCleanup(repo.connection.close)
        h = Harness()
        closed = h.propose()
        h.command('cancel', OWNER, closed, {'reason': 'Synthetic cancellation'})
        delivered = h.delivered()
        h.now += h.state['limits']['retentionMs'] + 1
        with repo.transaction():
            repo.connection.execute("INSERT INTO x_exchange_tenants VALUES('tenant-a','owner')")
            ExchangeTables(repo).save_state('tenant-a', h.state, 0)
        result = purge_expired(repo, now=h.now)
        self.assertEqual(result, {'removed': 1, 'unresolvedRetentionHolds': 1})
        with repo.transaction():
            state, _ = ExchangeTables(repo).load_state('tenant-a')
        self.assertNotIn(closed['id'], state['records'])
        self.assertIn(delivered['id'], state['records'])
        self.assertEqual(state['resources']['synthetic-review']['reserved'], 1)


if __name__ == '__main__':
    unittest.main()
