"""Trusted host maintenance functions; no CLI, HTTP route or schedule installed.

The owner must approve the maintenance runner and backup retention before launch.
Use the existing host's administration boundary. Unresolved obligations remain
held for explicit owner resolution and are reported without returning content.
"""
import time

from .core import apply_operation
from .repository import ExchangeTables, SCHEMA12_SHA256
from .recovery_fence import make_checkpoint, publish_checkpoint


def purge_expired(repository, *, now=None):
    instant = int(time.time() * 1000) if now is None else now
    tables = ExchangeTables(repository)
    removed, held = 0, 0
    with repository.lock, repository.transaction():
        tenants = [row[0] for row in repository.connection.execute('SELECT tenant_id FROM x_exchange_tenants')]
        for tenant_id in tenants:
            before, version = tables.load_state(tenant_id)
            if before is None:
                continue
            changed = apply_operation(before, {'kind': 'purge'}, now=instant)
            if changed['state'] != before:
                tables.save_state(tenant_id, changed['state'], version)
            removed += changed['result']['purged']
            held += sum(1 for record in changed['state']['records'].values() if record.get('retentionHold'))
    return {'removed': removed, 'unresolvedRetentionHolds': held}


def export_checkpoint(repository, directory):
    with repository.lock, repository.transaction():
        document, encoded = make_checkpoint(repository.connection, SCHEMA12_SHA256)
        published = publish_checkpoint(directory, document, encoded)
    return {'checkpointFile': published.pop('checkpointFile'), 'pin': published}


class MaintenanceRunner:
    """Uninstalled in-process runner using the existing native writer.

    The host must supply an approved cadence and private checkpoint directory.
    There is no background thread, timer, CLI flag or environment activation.
    A separate process cannot bypass the native writer lease.
    """
    def __init__(self, repository=None, *, enabled=False, cadence_ms=None, checkpoint_directory=None):
        if type(enabled) is not bool:
            raise ValueError('Explicit boolean maintenance gate required')
        if enabled and (repository is None or type(cadence_ms) is not int or cadence_ms <= 0 or checkpoint_directory is None):
            raise ValueError('Owner-approved cadence, checkpoint directory and native repository required')
        self.repository = repository
        self.enabled = enabled
        self.cadence_ms = cadence_ms
        self.checkpoint_directory = checkpoint_directory
        self.last_completed = None

    def run_once(self, *, now=None):
        if not self.enabled:
            return {'enabled': False, 'ran': False, 'installed': False}
        instant = int(time.time() * 1000) if now is None else now
        if type(instant) is not int or instant < 0:
            raise ValueError('Valid maintenance time required')
        if self.last_completed is not None and instant < self.last_completed + self.cadence_ms:
            return {'enabled': True, 'ran': False, 'reason': 'cadence_not_due'}
        result = purge_expired(self.repository, now=instant)
        # Failed publication is never reported as checkpoint success. The
        # committed native erasure journal remains authoritative. Restore must
        # compare a checkpoint against this current source and will reject stale
        # output after a crash or publication failure.
        result.update(export_checkpoint(self.repository, self.checkpoint_directory))
        self.last_completed = instant
        return dict(result, enabled=True, ran=True, installed=False)
