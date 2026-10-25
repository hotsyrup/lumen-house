# Local owner payment-request review

This directory is a read-only synthetic trace viewer, excluded by the observed Pages uploader's `.github` rule. The generator writes fictional `trace.json` beside these files; do not commit or publish live records, credentials, configuration, or private logs.

There is no login, bearer input, API mutation, wallet SDK, wallet URI, QR payment link, cryptographic confirmation, signing, sending, broadcasting, or execution capability. The primary wallet button stays disabled. The only request is a same-origin GET for `trace.json`. Copying a displayed SHA-256 digest copies that digest alone.

The approved principal ceilings are 0.10 USDC per accepted paid job, 1 USDC per `America/Los_Angeles` policy day, and 5 USDC lifetime. They are ceilings, not wallet balances, fee approvals, or automatic awards. Exact displayed principal comes from the accepted paid source. Fees remain unknown and unapproved; wallet confirmation is unavailable in the current native host.

## Trace contract

```json
{
  "steps": [{
    "label": "Request prepared",
    "actor": "synthetic owner",
    "state": {},
    "result": {},
    "policy": {"exposure": {}}
  }],
  "checks": [{"name": "Parallel preparation preserves the shared ceiling", "passed": true}],
  "limitations": ["The current host has no verified mobile wallet integration."]
}
```

`state` and `result` are actual request reducer outputs. `step.policy` should be the actual reducer policy result taken at the same trusted fixture time; its exposure supplies the current LA policy date and daily accounting. The UI never derives policy days from the browser clock or timezone. Without that policy snapshot it labels daily exposure unavailable.

The selected request comes from `step.request`, a request-shaped `step.result`, or `state.records` (optionally selected by `step.requestId`). Each request retains native job/worker/owner IDs; exact principal; source terms version/document hash, evidence ID/size/record class and acceptance digests; source payee attestation; origin policy day; and hold state. Record binding checks do not verify artifact bytes: native evidence retains a hash/size, and `artifactCheck` explicitly remains unavailable because the bytes are not retained. Wallet ownership is owner-attested and not independently verified. This viewer labels immutable `requestHash` as the request binding digest; the fallback `sourceHash` is clearly identified as a source binding instead.

## Reusable renderer boundary

`renderPaymentRequest(container, request)` renders a native request-only service object using safe DOM text nodes. An authenticated host could reuse it after independently enforcing current owner identity and object access. Authentication and route composition are not implemented or verified by this preview. No field can enable the disabled wallet action, including `handoffAvailable=true` in supplied data. A future reviewed handoff must preserve exact payer, payee, chain/token, amount, request binding, and fee disclosure through the owner's final cryptographic confirmation.

Local UI success establishes display behavior only. Source verification, native provisioning, payee proof, canonical asset validation, wallet integration, fees, production end-to-end testing, and launch approval remain separate gates.
