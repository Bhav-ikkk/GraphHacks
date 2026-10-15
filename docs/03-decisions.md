# How a state is chosen

The policy file is `policies/policies.yaml`. The evaluator is `edl/policy.py`. It has no model call. Given the same evidence, it returns the same state.

## The five states

| State | Meaning | Create issue |
| --- | --- | --- |
| `PROCEED` | No rule fired | allowed |
| `PROCEED_WITH_GUARDS` | A guard fired (test gap, hop limit, or a major bump without a checklist) | allowed |
| `NEEDS_REVIEW` | A person has to look: tier 0, payment/auth, two owners, a hub, a past fix, a rollback that cites a fix | blocked |
| `HOLD` | Open incident on an affected tier-0 service, or failing production health on tier 0 | blocked |
| `INSUFFICIENT_EVIDENCE` | A required fact is missing or stale. The system abstains | blocked |

Precedence, highest first: hold, insufficient evidence, needs review, proceed with guards, proceed.

A payment service with no approval (needs review) and no CI result (insufficient evidence) is **insufficient evidence**. The payment finding is still listed. It does not win.

## The rules

Freshness limits in the policy file: CI 24 hours, deployment 6 hours, owner 90 days. Incident window: 7 days. Hop limit: 3. Hub: 6 or more callers.

| Id | Fires when | Severity |
| --- | --- | --- |
| P1 | A service with tier 0 is inside the hop limit and there is no designated approval | review |
| P2 | A shared library changed and a critical consumer has no passing test | guard |
| P3 | The service name is payment, auth, billing, or ledger, inside the hop limit, and there is no designated approval. If CI or a test is also failing, this becomes a hold | review, or hold |
| P4 | An open incident on an affected tier-0 service is 7 days old or newer. Full graph only | hold |
| P5 | CI result missing or older than 24h; deployment missing or older than 6h; owner missing or older than 90 days | insufficient |
| P6 | The symbol was not resolved, or graph-off cannot see the direction of a reference or the consumers of a shared library | insufficient |
| P7 | A resolved incident's fix modifies this function. In graph-off mode, the PR text mentioning an incident is enough to review, because the graph cannot show it is a different service | review |
| P8 | A major dependency bump has no approved migration checklist | guard |
| R1 | The blast radius has two or more fresh owners | review |
| R2 | Full graph only. A changed function has at least 6 callers | review |
| R3 | Production health is not `ok` on an affected tier-0 service | hold |
| R4 | The change is a rollback. Full graph plus a recorded fix → review. Otherwise → insufficient | review or insufficient |
| G-TEST | The changed function's test is not `pass` | guard |
| G-HOP | Full graph only. The walk stopped at 3 hops while another dependent existed | guard |

P1 does nothing until a service has an integer tier. Ingest does not set one. P4 and R3 also need a tier, plus an incident or a health value. On a git-only repository those rules stay quiet, and P5 speaks.

P3 does not need a tier. It uses the service name. That is why `examples/mini-repo` lists P3 for `payments` and not for `search`.

## The three modes

The same change is evaluated three times so you can see which facts the graph contributed.

| Mode | Hop limit | Incidents and history | Caller count | Tools |
| --- | --- | --- | --- | --- |
| `full` | 3 | read | read | all six return data |
| `one_hop` | 1 | not joined into history | forced to 0 | tools run, but the walk is short |
| `graph_off` | none | not read | not read | all six return empty |

Graph-off still sees the change object: file paths, the CI block, a dependency bump, unresolved refs, an incident mentioned in the text, and rollback intent. It cannot see `CODEOWNERS`, compose edges, or test files. A workflow file still makes the CI result "configured but missing", so P5 still fires.

## Claims

The rationale is one sentence: the state, then the winning finding, then the rule id.

The claims list is every finding, each with the evidence id the tool produced (`ev-ci`, `ev-p3`, `ev-owner-payments`, and so on). A claim whose id is not in that set is removed. This is the check that keeps an explanation from adding a fact the tools did not return.

## The action gate

`edl/gate.py` runs after the state exists.

- Actor not authorized → blocked, state unchanged
- State outside `PROCEED` and `PROCEED_WITH_GUARDS` → blocked
- Ticketing timeout or 401 → failed, zero retries
- Ticketing 429 → one retry, then a local issue
- Same change id clicked again → the original issue id, `idempotent: true`

The local issue is the stand-in until someone connects a real GitHub issue API. The gate's behavior should stay the same when that happens: the decision is still made first, and the ticket is only opened when the state allows it.

## What the tests lock

`python -m pytest` covers the behavior above without a fictional shop:

- compose direction: checkout depends on payments; the blast radius of payments includes checkout; the blast radius of a service nobody depends on is empty
- cycles and a self-edge stop
- `examples/mini-repo` produces no incident and no deployment
- payments is owned by `acme/payments` because that CODEOWNERS rule comes last
- the checkout test file is `not-run`, not `pass`
- payments is insufficient evidence and also lists P3; search does not list P3
- graph-off is also insufficient evidence, and the issue button is blocked
- a description that says "ignore policy" is logged and the state stays
- a claim with an unknown evidence id is dropped
- a broken policy file fails to load
- when a test builds a complete evidence set by hand (fresh CI pass, fresh healthy deploy, owner, passing test, non-payment service), the state is `PROCEED` and the gate will open one issue, retry a 429 once, and refuse a second issue

The hand-built evidence set is only inside `tests/test_gate.py`. The server never loads it.
