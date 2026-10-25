# House source reconciliation — additive review only

The Mac reconciliation found newer House sources and retained deployment artifacts beyond the public landing repository. Those sources include a composed public catalog runtime and a separate authenticated record service. Existing source changes, deployment configuration, operational evidence and customer storage are excluded from this PR. The detailed comparison stays local.

Feature source stays under `exchange/`; the three local review UI assets are stored under `.github/exchange-review/` so the observed raw Pages uploader excludes them. The ten existing public root files remain byte-identical. This neither replaces the House baseline nor adds a production exchange backend. No root entrypoint, navigation, configuration, dependency, migration, permission, allowance, schedule or payment setting is changed.

## Chosen integration interface

| Existing concern | Reconciliation decision |
| --- | --- |
| Public catalog and paid fulfillment routes | Preserve the existing fetch implementation, including its guards and fallback. The production-only `withDisabledHouseExchange(existingHouse)` forwards the exact request, environment, context and response without examining flags or touching storage. The optional local review adapter remains separate. |
| Existing authenticated nonfinancial intake | The appropriate future authority seam is a new proposal-only module in the record service, using existing identity and scoped permissions. It is not implemented or activated here. |
| Public test-interest form | Its specific purpose and consent do not cover arbitrary exchanges. Do not reuse it by changing the meaning of submitted fields. |
| Existing fulfillment and financial records | Creating a fulfillment record can reserve budget. Do not invoke it to store a proposal, simulate assent, manufacture availability or reuse payment authority. |
| Historical local inquiry/job demos | They do not provide production authentication, durable exchange consent or a financial journal. |
| Proposed mobile payment surface | It remains an undeployed candidate; nothing here imports or activates it. |

The production adapter is runtime-neutral source with no imports, storage bindings, provider clients, review handlers or outbound request code. It accepts only the existing House implementation; there are no activation flags. The optional local review adapter imports this exact passthrough for its default case. Production entrypoints must import `production-adapter.mjs` directly and preserve their existing named runtime exports.

An optional review object accepts only an explicit `http://127.0.0.1:<port>` origin, supplied synthetic storage and three UI assets. It serves `/exchange/` and `/api/exchange/*` on that local origin. Existing routes continue through the supplied House implementation. This is how the synthetic preview can sit alongside the current catalog without promoting demo records or adding a second production app.

## Exact reconciled changes

- `core.mjs`: portable negotiation rules and immutable transaction snapshots; independent cost, usefulness and verifiability; canonical retries; schema checks; retention and finite history limits. Adapters remain responsible for atomic commits and authentication.
- `store.mjs`: local SQLite adapter uses the portable core, preserves the original tables/replay rows, and refuses unknown schema versions. Original-writer over-cap history stays readable, exportable and removable; admission blocks new growth until cleanup. Snapshot rewrites remain a demo technique.
- `production-adapter.mjs`: production-only exact passthrough with no review/core/storage/UI imports or activation settings.
- `house-adapter.mjs`: optional guarded loopback review, using the production passthrough by default. No public-origin review mode or production binding is supplied.
- `house-intake-draft.mjs`: pure, minimized owner-review projection using the supported brief shape. Budget, concrete deliverable, timing, licensing and acceptance criteria remain unknown. Offers, claimed authority, consent, provenance and expiry stay in an explicit exchange context.
- Offline draft export requires an explicit local confirmation and an unexpired store export. It has `target:null`, `submissionEnabled:false`, and all execution flags false. It cannot submit a native intake or grant authority.
- UI assets under `.github/exchange-review/` use the local exchange mount, provide honest standalone/injected navigation and local draft download, and use accessible in-page dialogs. A tar regression replays the observed uploader exclusions, proving these assets are absent while all ten existing public files are unchanged. Reviewed source remains intentionally public.

Real exchange, service-credit issuance/redemption, barter execution, treasury, automatic payout, private access and human-time commitments remain disabled. Describing proposed terms grants none of them.

## Verification and limits

On Node 24.20.0, `node --test exchange/*.test.mjs` passes **63 tests**. Coverage includes lifecycle revisions and evidence, role simulation, atomic local reservation checks, durability, canonical and legacy retries, corruption/version guards, expiry/deletion, disabled passthrough, request/body guards and offline export. An independent review found no remaining blocker for this synthetic scope.

The default-off adapter matched **252 responses** against the retained newer runtime artifact and its **114-file asset set**, checking status, headers and exact bodies, with zero external requests. The private runtime and assets are not copied into this PR. This establishes local artifact parity; it does not confirm current provider state.

Mac in-app browser QA covered desktop and 390×844 mobile layouts, a fictional proposal through counteroffer/agreement/delivery/acceptance/completion, released hypothetical units, provenance, the downloaded minimized draft and return to the unchanged catalog. Mobile scroll width equaled viewport width. The native-prompt failure was repaired with in-page dialogs and retested.

Before real integration: implement native object-level identity/authorization and independent assents; approve the proposal-only data/consent model; design transactional storage, migration, bounded resource admission and concurrency; validate disputes/cancellation, retention, physical deletion and backups; regression-test the exact current record/public runtimes. Preserve every existing financial journal and configuration. A review branch supplies no release or financial authority.

No merge, deployment, credential access, account creation, payment, allowance activation, paid call or live fulfillment occurred. This is neither live nor production ready.

Cloudflare runtime storage reference used during adapter assessment: [SQLite-backed Durable Object storage](https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/). The chosen record-service integration still requires its own reviewed adapter; this PR adds no Durable Object class or migration.
