# Local controls trace viewer

This read-only viewer is excluded by the observed Pages uploader because it lives in `.github/`. Serve it only from a loopback review harness. It neither authenticates users nor sends exchange commands. Its buttons select snapshots produced by the actual controls core; no lifecycle rules are implemented in the UI.

Generate the reproducible fictional trace from the repository root:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3.11 .github/exchange-controls-review/generate_trace.py
python3.11 -B -m http.server 8796 --bind 127.0.0.1 --directory .github/exchange-controls-review
```

Open `http://127.0.0.1:8796` for local review only. `trace.json` is generated and excluded from Git as well as the Pages artifact. Its shape is:

```json
{
  "checks": [{"name": "A forged owner assertion is rejected", "passed": true}],
  "steps": [{"label": "Requester assents", "actor": "requester", "state": {}, "result": {}}],
  "limitations": ["Live identity and deployed readiness remain unverified."]
}
```

Each `state` is the full reducer state. The selected proposal is inferred from `result.id` and `state.records`; optionally supply `step.proposal` or `step.proposalId`. `step.description` may give a concise outcome. The viewer recognizes separate requester/provider assents, versioned terms, owner approval, evidence, acceptance, resource accounting, and stop/retention settings. The expandable JSON is the authoritative trace view if a display field is absent.

Use fictional data only. Do not copy native source, customer records, credentials, private deployment configuration, or logs into a trace. A successful local review is implementation evidence; it does not establish live readiness or authorize provisioning or activation.
