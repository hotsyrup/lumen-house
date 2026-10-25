"""Trusted host maintenance functions; no CLI, HTTP route or schedule installed.

The owner must approve the maintenance runner and backup retention before launch.
Use the existing host's administration boundary. Unresolved obligations remain
held for explicit owner resolution and are reported without returning content.
"""
import time

from .core import apply_operation
from .repository import ExchangeTables


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
