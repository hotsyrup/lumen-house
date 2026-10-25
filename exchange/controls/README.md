# Controls verification

Run from the repository root with Python 3.10+ (3.11 used):

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s exchange/controls -t . -v
node --test exchange/*.test.mjs
```

The portable run explicitly skips tests requiring the separately installed private native source. To run full integration, set `LUMEN_HOUSE_NATIVE_SOURCE` to the independently reviewed private host candidate, and `LUMEN_HOUSE_NATIVE_FIXTURES` to the existing native workflow/policy test-fixture directory. The revised restore deliberately requires that candidate's complete reusable native recovery hook; an unpatched installation must fail closed. All test stores are temporary and use fictional principals/test authentication fixtures with no provider access. The tests do not read deployment data or issue real credentials. Local HTTP tests need permission to bind ephemeral loopback ports.

The native checks exercise the installed identity, current credential/profile checks, repository migration/fingerprint/backup/restore and real HTTP handler. The HTTP harness deliberately supplies synthetic composition; the private host adoption patch has its own preservation checks. Neither proves the currently deployed host has adopted these files. A skipped native suite must never be presented as full integration validation.

Production entrypoints and deployment configuration are unchanged. The unchanged Worker passthrough is pinned by `controls-artifact.test.mjs`. Actual tar tests verify excluded review UI and preserved public bytes and reject private/generated database, bytecode and evidence artifacts. Keep `PYTHONDONTWRITEBYTECODE=1` or `python -B`; raw Pages packaging does not respect Git ignore rules.

The excluded `.github/exchange-controls-review/` trace viewer is a read-only visualization of synthetic reducer outputs. It does not authenticate, accept commands, or supply production UI. Browser QA and private exact-runtime parity results are recorded separately; private runtime/configuration/records are not copied into the public PR.

The [control and provisioning review](../CONTROLS.md) defines approval steps, privacy/deletion limits and rollback requirements. No operational command in these instructions authorizes a live migration, grant, launch or financial action.

[OPERATIONS.md](OPERATIONS.md) describes the uninstalled default-disabled runner, owner-selected cadence/checkpoint custody, source-dependent erasure reconciliation and unsupported source-loss recovery. These are local recorded tests, not GitHub CI or production acceptance.
