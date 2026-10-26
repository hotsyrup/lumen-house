# Local verification record

Source-only review on 2026-10-25. All production gates remain off.

| Check | Result | Boundary |
| --- | --- | --- |
| Python controls/native integration suite | 77 passed, zero skipped | Python 3.11; reviewed private native host candidate based on the verified ongoing-intake release context, including the full native recovery hook; disposable stores/loopback ports and existing native test fixtures |
| Existing exchange plus publication regressions | 66 passed, zero skipped | Node 24.20.0; original demo behavior, original public bytes, actual tar exclusion, credential/path scan, unchanged production-only import graph |
| Private disabled-host matrix | 5,980 exact comparisons | 2,990 cases per original/current and candidate schema11/schema12 host; enumerated allowlists, methods, roles, denial variants and absent surfaces; Date header excluded only |
| Upload-time credential revocation | Three actor flows rejected with 401 | Buyer proposal, provider assent and owner approval paused after initial authentication; exact credential revoked before body completion; every exchange table/version unchanged |
| Private startup/native-hook checks | Four startup tests plus exact native callback AST preservation | Same current-context candidate; full native policy/execution/standing/authority revocations and recovery control gaps |
| Exact retained Worker package parity | 248 matching responses; zero external requests | GET/HEAD assets and disabled exchange paths plus disabled POSTs; existing three named DO exports retained; dated deployed-package baseline |
| Desktop/mobile trace review | Passed at 1440×1000 and 390×844 | 16 actual reducer snapshots, 8 adversarial checks; no overflow, page errors, external requests or existing tabs used |
| Public read-only check | Catalog 200; three exchange-looking paths 404 | Four unauthenticated GETs only; no review UI present; provider version metadata and authenticated live readiness not checked |

Adversarial native checks cover current credential/membership/role/tenant mismatch, forged body authority, narrow profile denial, independent exact consent/assent, stale terms and versions, self-acceptance, double acceptance, concurrent resource admission, acceptance/cancellation races, interrupted commit rollback, post-assent participant revocation, evidence object access, disabled route fallback, retention holds, deletion retries, unknown schema, additive migration preservation, corrupt backup rejection and STOP-held recovery.

Follow-up tests use the unchanged real storage thresholds: normal admission reaches 665,323 of 665,600 bytes, safety growth reaches 971,717 of 972,800 bytes before compaction, and 125 recoveries fill the actual 100-event audit bound. All safety actions preserve consumed/reserved accounting, evidence, current authorization and rollback immutability; 768 repeated safety calls stay bounded. No lowered constants substitute for these checks.

Independent review of the original `bf712f2` found that its schema12 restore omitted some native policy/execution/standing revocations and recovery gaps. The revised path requires the complete native hook and differential tests with non-empty approved policy, execution, standing, obligation and credential history. Old owner policy is rejected after both schema11 and schema12 recovery; financial STOP remains latched. This corrects lost recovery protections, not a demonstrated payment bypass.

Deletion/backup tests cover an older pre-deletion backup, receipt/state rollback, checkpoint publication failure after committed purge, stale/missing pins, wrong lineage, active writers, source loss, native business-row drift and consumed quota preservation. Restore requires the actual quiesced current source; source-loss recovery is deliberately unsupported. [OPERATIONS.md](OPERATIONS.md) states that boundary and the uninstalled runner's owner-policy decisions.

An independent source review found no remaining blocking authorization/accounting issue for this disabled slice. The separately retained private host compatibility patch and its exact-route comparisons are required integration artifacts; private source/configuration and records are not published here. They must be reconciled to the final approved deployment artifact before adoption.

These results are from retained Mac-local raw logs with reproducible commands and source/harness hashes. They are not GitHub CI. The original 55-test result at `bf712f2` remains historical evidence; this follow-up adds acceptance coverage and recovery changes and requires a fresh independent private-host review.

These are implementation and packaging results. No production migration, provisioning, credential issuance, security setting, exchange deployment, merge, activation, grant, payment or resource execution was performed. Remaining approval steps and rollback constraints are in [CONTROLS.md](../CONTROLS.md).
