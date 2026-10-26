"""Uninstalled schema13 maintenance; owner-selected noncash retention only.

Request records and unresolved principal holds are retained. No timer, schedule,
HTTP route, retention policy, wallet capability or authority is installed here.
"""
import time

from ..controls.maintenance import MaintenanceRunner as ExchangeMaintenanceRunner, purge_expired
from .repository import _metadata, export_checkpoint


class MaintenanceRunner(ExchangeMaintenanceRunner):
    """Use the existing native writer; default gate performs no database work."""

    def run_once(self, *, now=None):
        if not self.enabled:
            return {"enabled": False, "ran": False, "installed": False}
        instant = int(time.time() * 1000) if now is None else now
        if type(instant) is not int or instant < 0:
            raise ValueError("Valid maintenance time required")
        if self.last_completed is not None and instant < self.last_completed + self.cadence_ms:
            return {"enabled": True, "ran": False, "reason": "cadence_not_due"}
        # Refuse a schema12 or forged store before any retention mutation.
        with self.repository.lock, self.repository.transaction():
            _metadata(self.repository.connection)
        result = purge_expired(self.repository, now=instant)
        # The native erasure journal remains committed if publication fails.
        # Restoration requires a current matching schema13 checkpoint and pin.
        result.update(export_checkpoint(self.repository, self.checkpoint_directory))
        self.last_completed = instant
        return dict(result, enabled=True, ran=True, installed=False)
