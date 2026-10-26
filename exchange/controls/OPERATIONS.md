# Uninstalled maintenance and fenced recovery

This is prepared code and synthetic verification. No runner, cadence, configuration, checkpoint directory, backup policy or production migration is installed or approved by this review. Existing intake and job management gain no cutoff. A schema12 migration creates an opaque technical lineage and empty erasure journal; it creates no membership, offered resource, credential or grant.

## Maintenance boundary

`MaintenanceRunner` defaults to `enabled=False`; it starts no thread, timer or scheduler. An approved host can call `run_once()` with its existing repository. Enabling requires an explicit owner-selected cadence and private checkpoint directory; there is no cadence default. Purge uses the existing native lock and transaction and enumerates exchange state only. Reserved/delivered disputes remain holds for owner resolution. The separate ordinary intake store is neither opened nor changed.

The optional owner-terminal module also does nothing by default:

```sh
python3.11 -B -m exchange.controls.maintenance_cli
```

Its explicit configuration format is shown below. The unset template cannot run:

```json
{
  "format": "lumen-house-exchange-maintenance/v1",
  "enabled": false,
  "storePath": null,
  "checkpointDirectory": null,
  "cadenceMs": null
}
```

For review, `--config OWNER_SELECTED_FILE` reads an owner-private regular configuration file and reports that it has not executed. `--execute` additionally requires the enabled configuration. This one-shot path opens an already migrated exact-schema store without migration and must obtain its native lifetime writer lease. It refuses an active host writer; it cannot be installed beside that writer as a second database process. Use the existing separately approved maintenance fence for terminal runs, or the in-process runner for an online host. Installation/scheduling and maintenance completion still require their own approved native operational procedure.

Checkpoint output requires an existing owner-owned directory without group/other access. Files are immutable, owner-private, fsynced and content addressed. Output contains digest selectors, journal cursor/head, lineage, schema fingerprint and business-state fences; no proposal/terms/evidence, raw record IDs or credential values are exported. Preserve its returned four-field pin in separately trusted owner records. A checksum copied from a backup folder is not freshness authority.

If purge commits and checkpoint publication then fails, erasure receipts remain committed in the native database. No checkpoint success is reported; the terminal error says mutation may have committed. Reconcile/retry from that current source. A stale exported checkpoint will be rejected by restore. No automatic retry, reopening, grant or cleanup schedule is installed.

## Erasure and backup reconciliation

Every logical record removal through the storage adapter writes a minimal append-only erasure receipt in the same native savepoint/transaction as the state CAS. It records a tenant/record digest selector, ordered hash-chain position and timestamp. Requests cannot reuse an erased record identity. Receipts are not automatically pruned while old backups may exist; their disposal needs an approved complete backup inventory and retention process. Hash chains detect inconsistent editing, not a privileged operator rewriting the entire store.

Supported restore requires all of:

1. The exact reviewed native `apply_restore_holds` hook. It reuses the full native credential, policy, execution-approval, standing-grant, authority, financial STOP and `restore_snapshot` control-gap protections. A missing hook fails closed; there is no reduced SQL fallback.
2. The **actual current native store**, separately identified by the trusted owner procedure and quiesced under its writer lease. It cannot be the backup being restored. The code holds that source lease and a read-only snapshot throughout recovery. A stale copy supplied by the trusted operator cannot prove freshness; it is not an acceptable authority. Lost-source disaster recovery is unsupported by this slice.
3. An owner-private checkpoint file and independently retained exact `{sha256, lineage, erasureSequence, erasureHead}` pin matching that current source snapshot. Missing, stale, malformed, wrong-lineage, truncated or inconsistent evidence refuses recovery.

Restore first verifies native backup checksum/schema, journal lineage/prefix and complete tenant scope. It then applies authoritative erasures to the isolated destination before publishing it. It refuses deletion of a backed-up reservation and rejects any remaining exchange business/consumed/held-state difference. A digest fence also requires all other native business rows to match the current source, including obligations, budgets, principals, providers, grants and controls. Credential rows and security events are the only intentional exceptions; the native recovery hook revokes/extends them. Newer native debt or records cannot silently disappear in an older backup.

Only after reconciliation does recovery apply the full native holds and exchange STOP/revocations. The destination must be new; failures remove partial output and never overwrite the current source. Recovered accounting/holds remain for owner review. A backup requiring reconciliation of additional business changes is refused, rather than repaired or opened automatically.

This strict boundary supports an older backup differing only by safely reconcilable exchange erasures. It deliberately refuses general point-in-time rollback that would lose later business changes. It does not restore or verify a separate intake database: a whole-application recovery must independently reconcile that store too, preserve its no-cutoff behavior and retain its current application rows. There is no claim of secure physical erasure of pages, WAL, backups or downloaded copies.

## Smallest outstanding owner decisions

- Select the actual native source/store and compatible maintenance/recovery artifact, private checkpoint location/access controls, owner audit/pin custody and approved runner cadence/installation point.
- Approve retention/retry/dispute-hold policy, complete backup inventory, backup/copy disposal and the rule for retaining erasure receipts until no recoverable older copy remains. No values are selected here.
- Decide whether source-loss recovery is required. That needs a separately reviewed durable external write-ahead erasure authority; an after-the-fact checkpoint alone cannot close a crash-and-source-loss window. This implementation blocks that recovery case.

No production values, schedules or authority are inferred from fixture values. Raw local test logs and exact private source hashes are retained separately; these checks are not GitHub CI or live readiness.
