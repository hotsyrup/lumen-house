# Offline verification boundary

Run from this repository with Python3.11 and Node. Keep bytecode disabled because raw Pages packaging does not follow Git ignore rules.

```sh
PYTHONDONTWRITEBYTECODE=1 python3.11 -B -m unittest discover -s exchange/payment_requests -t . -v
node --test exchange/*.test.mjs
python3.11 -B .github/payment-request-review/generate_trace.py
```

Portable tests explicitly skip installed-native checks. For a complete local run, supply `LUMEN_HOUSE_NATIVE_SOURCE` pointing to the independently reviewed private **schema13-compatible** candidate and `LUMEN_HOUSE_NATIVE_FIXTURES` to its existing workflow/policy fixture helpers. These tests create temporary synthetic stores and use fictional authentication, budgets, policies, terms and evidence; they open no live deployment store and contact no wallet/provider. Native HTTP tests require an ephemeral loopback listener. Never substitute a skipped suite for native integration.

Coverage includes exact owner/tenant/payer binding; forged actor/source/body; current native credential/profile/membership; independent current paid assents and exact delivery acceptance; original payment authority/intent blocking; same-job/new-key replay; changed source and unknown outcomes; parallel final daily-slot admission; retained prior-day exposure and23/25-hour DST days; immutable origin-day, hold/outcome/retry history; actual-size future-report safety headroom; exact schema fingerprint and explicit migration; complete native-table preservation during request lifecycle; writer lease/CAS/restart; current-source fenced restore and full native recovery revocations. Disabled artifact/import-graph checks preserve the storefront and existing Worker graph.

The read-only UI generator uses the actual reducer, fictional jobs and trusted fixture times. Local mobile/desktop checks establish display behavior, overflow/accessibility/error handling and an always-disabled wallet button. It does not prove authenticated deployment UI or cryptographic owner confirmation. Service annotations distinguish current source from a historical held snapshot; hash/size is not artifact-byte verification, and owner attestation is not independently proven wallet ownership.

Raw local stdout/stderr, reproducible commands, private adapter source hashes, synthetic host comparison and browser screenshots are retained privately by the reviewer. They are **not GitHub CI, a live payment, or verified production readiness**. See [OPERATIONS.md](OPERATIONS.md) for required approvals and unsupported recovery cases.

## Recorded local result (2026-10-26)

The combined explicit controls/payment module run passed **144 tests, zero failures/errors/skips**, using the reviewed private schema13 candidate and native fixture helpers. Node passed **68 tests, zero skips**, including public artifact preservation and the unchanged disabled production import graph. The private host's six startup/recovery compatibility checks also passed. These counts include temporary synthetic HTTP and database operations; they do not establish deployment adoption, a verified wallet, actual funding or financial activation.
