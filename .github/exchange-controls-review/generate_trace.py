"""Fictional reducer trace only. Run from the repository root with python -B."""
from copy import deepcopy
import json
from pathlib import Path

from exchange.controls.test_core import Harness, OWNER, agent, config, core

ControlError = core.ControlError

h = Harness(False)
steps = [{'label': 'Default: stopped and empty', 'actor': 'system', 'state': deepcopy(h.state), 'result': {}}]
checks = []


def capture(label, actor, run):
    result = run()
    steps.append({'label': label, 'actor': actor, 'state': deepcopy(h.state), 'result': deepcopy(result)})
    return result


def denied(name, expected_status, run):
    try:
        run()
        raise AssertionError(name)
    except ControlError as error:
        assert error.status == expected_status, (name, error.status)
    checks.append({'name': name, 'passed': True})


denied('Default grants nothing', 503, h.propose)
capture('Owner defines fictional limits', 'owner', lambda: h.command('configure', OWNER, value=config()))
capture('Owner explicitly opens synthetic review', 'owner', lambda: h.command('open', OWNER))
p = capture('Requester proposes exact non-cash terms', 'requester', lambda: h.propose(key='create-trace'))
original = deepcopy(p)
denied('Cross-tenant identity is rejected', 403, lambda: h.get(p, agent(tenantId='other-tenant')))
denied('Owner cannot assent for an agent', 403, lambda: h.command('assent', OWNER, p, {'termsRevision': p['termsRevision'], 'termsHash': p['termsHash'], 'sharingConsent': True}))
p = capture('Requester independently assents', 'requester', lambda: h.assent(p, 'requester'))
denied('Owner approval needs both independent assents', 409, lambda: h.command('approve', OWNER, p, {'termsRevision': p['termsRevision'], 'termsHash': p['termsHash']}))
p = capture('Provider independently assents', 'provider', lambda: h.assent(p, 'provider'))
p = capture('Owner approves and reserves one unit', 'owner', lambda: h.command('approve', OWNER, p, {'termsRevision': p['termsRevision'], 'termsHash': p['termsHash']}))
p = capture('Provider records claimed delivery evidence', 'provider', lambda: h.command('deliver', agent('provider'), p, {'evidence': {'sha256': 'a' * 64, 'description': 'Fictional manifest, independently verified by owner before acceptance'}}))
acceptance = {'deliveryRevision': p['evidence']['deliveryRevision'], 'deliveryHash': p['evidence']['deliveryHash'], 'assessment': 'Fictional owner recomputed the supplied fixture digest'}
denied('Provider cannot accept its own evidence', 403, lambda: h.command('accept', agent('provider'), p, acceptance))
p = capture('Owner accepts: reserved becomes consumed', 'owner', lambda: h.command('accept', OWNER, p, acceptance))
denied('Double acceptance cannot consume twice', 409, lambda: h.command('accept', OWNER, p, acceptance))
q = capture('Second fictional delivery awaits review', 'provider', h.delivered)
q = capture('Cancellation after delivery holds a dispute', 'requester', lambda: h.command('cancel', agent(), q, {'reason': 'Fictional evidence needs review'}))
capture('Stop blocks authority and preserves dispute hold', 'owner', lambda: h.command('stop', OWNER, value={'reason': 'Synthetic stop-switch review'}))
denied('Stopped participant cannot read evidence', 503, lambda: h.get(q, agent('provider')))
q = h.get(q)
q = capture('Owner resolves dispute while stopped', 'owner', lambda: h.command('resolve', OWNER, q, {'outcome': 'cancel', 'reason': 'Synthetic owner resolution without resource execution'}))
capture('Terminal logical deletion erases first payload', 'owner', lambda: h.command('delete', OWNER, p, {'reason': 'Fictional deletion review'}))
capture('Owner deliberately reopens to verify erased retry', 'owner', lambda: h.command('open', OWNER, key='trace-reopen'))
denied('Deleted proposal retry cannot resurrect payload', 410, lambda: h.propose(key='create-trace'))
capture('Review ends stopped', 'owner', lambda: h.command('stop', OWNER, value={'reason': 'End fictional review'}))

output = {'checks': checks, 'steps': steps, 'limitations': [
    'Synthetic reducer outputs; this viewer does not authenticate or send commands.',
    'Native HTTP, current-credential validation and concurrency are separate disposable-fixture tests.',
    'No real permissions, resource offers, budgets, money, wallets or execution are approved.',
    'Production host adoption, provisioning, maintenance/backup policy and controlled live E2E remain gated.'
]}
Path('.github/exchange-controls-review/trace.json').write_text(json.dumps(output, indent=2) + '\n')
print(json.dumps({'steps': len(steps), 'adversarialChecks': len(checks), 'syntheticOnly': True}))
