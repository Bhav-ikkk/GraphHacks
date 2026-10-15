# What a repository actually provides

The scan is allowed to store a fact only when a file in the repository you chose supports it. If the file is missing, the fact is missing. The policy then abstains, instead of guessing.

## Facts the scan stores

| You can point at this in the repo | Node or edge | How it is used |
| --- | --- | --- |
| `docker-compose.yml` service name | `Service` | A reviewable unit |
| `depends_on` | `(consumer)-[:DEPENDS_ON]->(dependency)` | Blast radius. Direction matters |
| `Dockerfile`, `go.mod`, `package.json`, `pyproject.toml`, `pom.xml`, `Cargo.toml` | `Service` named after the parent folder | Finds a service that has no compose entry |
| `services/`, `apps/`, `packages/`, `cmd/` | `Service` | Monorepo layout |
| `def name` in Python, exported `func Name` in Go, `function name` in JS/TS | `Function` | The change id, for example `repo:checkout.place_order` |
| `rpc Name` in a `.proto` file, local or downloaded | `Function` | Same as a source function |
| `.github/CODEOWNERS`, `CODEOWNERS`, or `docs/CODEOWNERS` | `Team` or `Engineer`, edge `OWNS` | Owner for rules P5 and R1. The last matching rule wins, which is how GitHub applies CODEOWNERS |
| a file under `test/`, `tests/`, or named `test_*.py`, `*_test.go`, `*.spec.ts` | `Test` with status `not-run` | Proves a test file exists. It does not prove the test passed |
| `.github/workflows/*.yml` | `ci.configured = true` on the change | Proves a workflow file exists. It does not prove the latest run passed or is fresh |

CODEOWNERS handles that contain a slash (`@acme/payments`) become a `Team`. A handle without a slash becomes an `Engineer`. The owner is dated as observed on the scenario clock, 15 October 2026 12:00 UTC, which is inside the 90-day freshness window. We do not yet read the commit date of the CODEOWNERS file. That is a real gap: a stale owner file would still look fresh.

Payment and auth are recognized from the service name, on word boundaries: `payment`, `payments`, `auth`, `authentication`, `billing`, `ledger`. `author` does not match `auth`. `search` does not match. This replaced a fixed pair of names (`payment-service`, `auth-service`) that only existed in the old fictional shop, so a real service called `payments` never triggered the payment rule.

## Facts the scan refuses to invent

| Missing fact | What the rule does | Why it is missing |
| --- | --- | --- |
| Latest CI run: pass/fail and age | P5, insufficient evidence | A workflow file is not a run. The run lives in GitHub Checks or your CI host |
| Deployment age and health | P5 if absent, R3 if health is failing on a tier-0 service | Production state is not in git |
| Open or resolved incidents | P4 hold, P7 review | Incidents live in PagerDuty, incident.io, or similar |
| Service tier | P1 review when a tier-0 service is inside 3 hops | Nothing in a normal repo declares tier 0–3. A name guess (`pay` means tier 0) was wrong on real trees and has been removed |
| Designated approval | P1 and P3 stay at "review" when the path is sensitive and no approval is on file | Approvals live on the pull request |
| Callers of a function | R2 review when a function has 6 or more callers | Ingest does not parse call expressions yet, so caller count is 0 |
| Passing test | G-TEST stays a guard while status is `missing` or `not-run` | We can see the file. We have not executed it |

Because CI result and deployment are missing on every git-only ingest, the winning state on a real repository is **insufficient evidence**. Other rules still appear in the findings. They do not override P5. Precedence puts insufficient evidence above review and above a guard.

That is the correction the fictional scenarios could not survive. Those scenarios passed only because the overlay inserted a fresh green CI run, a healthy deploy, a passing test, and sometimes an incident. Point the same rules at a real tree and those inserts are false, so the state has to fall back to abstain.

## What you need before a real repository can reach the other states

Each row is a source the team still has to connect. Until it is connected, leave the fact absent.

| State you want to be able to reach | Source you still need | Fact it must write |
| --- | --- | --- |
| Leave insufficient evidence | GitHub Checks (or the CI system you use) | `ci.present`, `ci.result` (`pass` or `fail`), `ci.age_hours` |
| Leave insufficient evidence | Deploy system or a status file you trust | `Deployment` node: `age_hours`, `health` (`ok` or a failure value) |
| `HOLD` on a live incident | Incident tracker | `Incident` with `status: open`, `age_days`, and an `AFFECTED` edge to the service |
| `NEEDS_REVIEW` on a past incident | Incident tracker plus the fixing commit | `Incident` `status: resolved`, a `Fix`, and `MODIFIES` to the function |
| `NEEDS_REVIEW` on tier 0 | A tier declaration you choose (a file in the repo, not a guess from the name) | integer `tier` on the `Service` node |
| `NEEDS_REVIEW` for two teams | CODEOWNERS already does this when two services in the blast radius have different owners | already stored |
| `PROCEED_WITH_GUARDS` | A test run, not just a test file | `Test.status = pass` or `fail` |
| `PROCEED` | All of the fresh-pass facts above, and no hold, review, or guard rule firing | the tools return those values |

Do not fill these with sample values to make a demo look green. The page is supposed to show the gap.

## GitHub versus a local folder

| | Local folder | GitHub URL |
| --- | --- | --- |
| Compose dependencies | read from disk | one compose file downloaded |
| Function names | every source file in the walk | at most 12 source files inside detected services |
| CODEOWNERS | read from disk | downloaded if the path is in the tree |
| Rate limits | none | unauthenticated GitHub API; set `GITHUB_TOKEN` |
| Private repos | yes, if the folder is on disk | only with `GITHUB_TOKEN` |

Function names on a large GitHub repo can be incomplete because of the 12-file cap. The service folder is still reviewed, and the change note says when no function name was found. Raising that cap, or cloning the repo, is remaining work.

## What "resolved mapping" means

Each change lists symbols such as `payments.Charge`. The review looks up the node `fn:payments.Charge`. Ingest only emits a symbol when it created that node, so the mapping is `resolved`.

Rule P6 abstains when a mapping is `inferred` or otherwise unresolved. The running ingest does not emit inferred links. A later call-graph parser must mark a guessed edge as inferred, or P6 will not protect you.
