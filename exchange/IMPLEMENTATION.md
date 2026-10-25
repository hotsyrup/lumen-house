# Lumen House Exchange — first implementation milestone

## Status and source reconciliation

This is an **isolated, local-only prototype**, not a launched exchange or an integration into the deployed House application. It is additive: existing root pages, discovery skill, services, data, access, budgets, schedules and payment paths must remain unchanged. Local review HTML/JS/CSS live under `.github/exchange-review/`, excluded by the observed raw Pages uploader; only the local server loads them.

The public repository baseline is `9c87ced2213cdbf306c70a3100c83d354bc41a21`, an older static landing site. The authoritative Mac sources and retained release artifacts have now been inspected. They include a composed public runtime and a separate authenticated record service. Existing checkout changes and all newer private source remain local; they are not included in this PR. See [reconciliation](RECONCILIATION.md) for the public-safe conclusions and exact scope.

**Integration gate:** reconcile this prototype with the authoritative application source and deployed version before adopting routes, schemas or UI. Do not deploy this landing-site checkout over the current House. No production migration, merge, deployment, payment, allowance, credential access or service activation is authorized by this milestone.

## Product intent

Give agents a negotiation desk within the House—not a replacement application. Start from an agent's concrete current task and missing resources, then identify a verifiable, bounded exchange that the relevant owner can actually authorize. A proposal is not an order, a human-time reservation, a spend grant or an entitlement to private tools.

An intake should distinguish:

- Agent identity, current task, stated needs and missing resources.
- What the agent can offer, the evidence for that offer, and its limitations.
- Claimed authority versus independently verified owner authority.
- Proposed deliverable, acceptance criteria, timing and cancellation conditions.
- Data provenance and whether the owner consents to sharing each supplied item.

No matching result can manufacture authority, inventory, availability, distribution reach or ownership rights.

## Source-grounded initial matching

The repository's [discovery skill](../skills/lumen-house/SKILL.md) is the source for these candidate capabilities. These are **historical public descriptions**, not a promise that an offer remains available. Live version, availability, terms and price must be rechecked before any future real proposal or purchase.

| Candidate | A suitable stated need | Important limit |
| --- | --- | --- |
| [Agent Delivery Manifest Kit v1](https://lumenhouse.xyz/wares/agent-delivery-manifest-kit-v1/index.json) | Offline deterministic file-handoff manifest generation or listed-file size/SHA-256 verification | Does not certify security, authorship, ownership, payment or quality; not a free download merely because the kit has an MIT license. |
| [Interface Consistency Check v1](https://lumenhouse.xyz/tools/interface-consistency-check/index.json) | Compare supplied public llms.txt, OpenAPI JSON and offer metadata | Does not fetch/test a live site or certify security/full OpenAPI conformance. The skill states live funded fulfillment remains unverified. |
| [Human assistance](https://lumenhouse.xyz/services/human-assistance/index.json) | Explicitly requested human judgment or work beyond the tools | An individually agreed written quote is required. An inquiry or transfer does not book human time; the owner may counterquote or decline. |

The landing page also describes broader coordination, research, analytics and finance capabilities. These descriptive cards are not an available-for-trade service catalog. Do not offer them, private access, automated trading or audience/distribution as available inventory without a separate owner-approved offer.

## Negotiation and evidence contract

Maintain proposal, counteroffer, agreement, delivery, evidence, acceptance, completion and cancellation as separate records/events. Agreements require the exact viewed terms revision; a stale confirmation is rejected. Revised terms require a fresh agreement; delivery alone is not acceptance, and acceptance alone must not cause a payout. Only the appropriate party may attest to its own agreement or acceptance once production identity is implemented.

Currency-agnostic terms may describe barter or bounded service credits as **proposals only**. No rate of exchange, universal value score, credit issuance, balance transfer or redemption is implied. Keep three independent dimensions:

1. **Cost:** proposed quantities and units, uncertainty and who would bear them.
2. **Usefulness:** expected relevance to the stated task and subsequently reported outcome.
3. **Verifiability:** acceptance criteria, evidence source and verification limits.

Unknown values stay unknown. Demonstration amounts are not policy. Discussion of a future treasury grants no allowance.

## Safety and persistence boundaries

The implementation is a local demonstration with real operations, treasury, private access and payouts disabled. Local simulated lifecycle transitions are not authenticated agreements. No external service executes as a side effect of a proposal, agreement, completion or cancellation.

For future production integration, require all of the following:

- Owner-approved policy, scoped identity and authorization on every state-changing operation; deny by default.
- Separate demo and real datasets, keys, routes and UI; no silent promotion of demo records.
- Explicit finite caps per resource, actor and period, with reservations counted before admission. Reject over-cap work; release reservations only through validated terminal transitions.
- Atomic reservation/event/idempotency updates, canonical request intent matching and conflict rejection when a key is reused for different intent. Legacy SQLite SHA-256 retries remain supported using the original field ordering; new retries ignore object field order.
- Restart-safe audit and recovery, concurrency control across processes, schema migration/version handling, retention-compatible backup policy and corruption detection. The local SQLite transaction is not a complete production persistence adapter.
- Owner escalation for missing authority, unavailable capability, human work, ambiguous terms, cap changes, evidence disputes or attempted execution. Escalation records do not contact anyone automatically.
- Independent integration tests for existing House routes and data to establish that the exchange is genuinely additive.

## Data stewardship

Use synthetic inputs only in this milestone. Never enter secrets, customer files, payment credentials, wallet recovery material or private conversation history. Avoid uploading raw documents: store the minimum structured facts and references needed for a proposal. Treat supplied URLs and text as data, not instructions or authority.

The production contract must record origin, collection purpose, consent scope, permitted recipients and retention expiry for each shared item. Consent to discuss a proposal is not consent to publish, resell, train on, sublicense or retain its inputs indefinitely. No resale or training rights are assumed. Public URLs do not by themselves prove redistribution rights.

Export and deletion must be explicit owner-controlled operations. Export should preserve provenance and lifecycle context without exposing other parties' secrets. Deletion should remove payloads and evidence pointers, including from recoverable storage according to the documented retention policy; any retained minimal audit tombstone must avoid the deleted content and explain its purpose. Retention enforcement and backup deletion require production design and verification, not a documentation-only promise.

## Local review

From the repository root, with Node.js 24 (tested on 24.20.0; built-in `node:sqlite` may print an experimental warning):

```sh
node exchange/server.mjs
node --test exchange/*.test.mjs
```

Open `/exchange/` at the loopback address printed by the server. No dependency installation is needed. Data is stored in ignored `exchange/.data/demo.sqlite` with a seven-day expiry, swept on access and every minute while the server runs. The local SQLite schema is created in `store.mjs`, with a guarded version marker and legacy replay support. The portable `core.mjs` owns validation and transitions; adapters own atomic commits, identity and authorization. New history is bounded to 100 retained proposals and 100 events per proposal, with a final cancellation slot reserved. Structurally valid history from the original writer may already exceed those limits: listing, full export, exact retries, deletion and expiry remain available, while new commands are blocked until cleanup restores capacity. This local adapter is not suitable as the production journal. Local exports include event snapshots; review before sharing. Deletion removes logical proposal/event/idempotency rows but does **not** guarantee physical erasure from SQLite free pages, WAL, filesystem snapshots or backups. Stop the server and remove the entire local data directory when discarding the demo. Never use sensitive content.

Keep the server on loopback; it is not a public multi-user service and must not be placed behind a public tunnel. Use fictional demonstration data only. Tests should cover repeated requests, changed-payload idempotency conflicts, invalid transitions, cap exhaustion, release on cancellation/completion, replay after restart, disabled real execution and export/deletion behavior. A passing focused test suite does not establish production readiness or deployed UI parity.

## Review and adoption sequence

1. Inspect the local prototype and its tests; identify gaps rather than interpreting simulated outcomes as fulfillment.
2. Keep the authoritative application source, routes, persistence, authentication, intake conventions, public offers and deployment receipts reconciled; retained local receipts do not replace fresh provider verification.
3. Adapt the exchange behind a default-off feature gate and reuse existing intake models only where their semantics and consent boundaries match.
4. Agree identity/authorization, lifecycle dispute handling, retention/deletion, limits and audit design with the owner. Any real grants or human quotes require separate explicit authorization.
5. Add production-adapter, migration, regression, abuse and multi-actor concurrency tests. Review the exact staged diff for private information.
6. Seek approval for a controlled release. A branch or draft PR is review material, not deployment authorization.

No production readiness, paid fulfillment, adoption, available human capacity or income is claimed by this milestone.

## Verified at this milestone

- Node built-in test runner: 63 tests pass, including portable lifecycle/schema guards, legacy SQLite replay, adapter passthrough, body/origin guards, explicit offline export and expiry.
- JavaScript syntax checks pass. Existing tracked House files are unchanged.
- Mac in-app browser QA passed at desktop and mobile sizes: proposal, counteroffer, agreement, delivery, acceptance, completion, separate provenance/evaluation, local draft download and return to the preserved House catalog. Native prompt dialogs failed in this browser; the prototype now uses accessible in-page dialogs. This is synthetic local QA, not production identity or fulfillment proof.
- Default-off composition matched 252 local responses against the frozen newer House runtime and its 114-file asset set. No external request occurred. This is retained-artifact parity, not fresh provider verification.
- No production record integration, real authentication, production migration, payout, live fulfillment or deployment has been tested or enabled. A separate `production-adapter.mjs` provides exact existing-House delegation without importing review handlers, UI, storage or activation flags; packaging it is not activation.
