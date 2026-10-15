# How a review moves through the system

A review answers one question: given this change, which of the five states do the rules require, and which evidence path produced that state?

The state is computed in Python from the policy file. FalkorDB stores the same graph so you can open it in the browser. A sentence on the screen only explains the state. It cannot raise or lower it.

## The pieces

| Piece | File | Job |
| --- | --- | --- |
| Review page | `web/index.html`, `web/app.js` | You paste a repository, pick a change, and read the result |
| HTTP API | `edl/api.py` | The page talks only to this process |
| Engine | `edl/engine.py` | Holds the current graph, the list of changes, and past decisions |
| Ingest | `edl/ingest.py` | Turns the repository you chose into nodes and edges |
| Graph in memory | `edl/memory.py` | The working copy the rules read |
| Graph tools | `edl/queries.py` | The six named reads (blast radius, owner, incidents, tests, deploy, callers) |
| Review | `edl/review.py` | Runs the tools, then asks the policy for a state |
| Policy | `policies/policies.yaml`, `edl/policy.py` | The rules and the fixed precedence |
| Action gate | `edl/gate.py` | Decides whether Create issue is allowed |
| FalkorDB writer | `edl/falkor.py`, `edl/config.py` | Copies the graph with the official Python client |
| Settings | `.env` | Host, port, empty username, password, graph name |

One process serves the page and the API:

```powershell
python -m edl serve
```

Open http://127.0.0.1:8000. On startup the graph is empty. Nothing is analyzed until you paste a target.

## Step 1 — you choose a repository

The box accepts either of these:

- a folder on this machine, for example `examples/mini-repo`
- a GitHub URL, for example `https://github.com/owner/name`

`parse_target` checks the folder first. If that path exists, the tree is read from disk and no network call is made. Otherwise the text is treated as a GitHub repository. There is no default repository baked into the product.

Free-text Cypher is rejected. If the target contains the word `cypher` or starts with `MATCH` or `CREATE`, the API returns 400. The tools that run are the six named ones in `edl/queries.py`.

## Step 2 — the tree is scanned

**Local folder.** The walker records file paths. It skips `.git`, virtualenvs, `node_modules`, build folders, and `docs/`. It does enter `.github`, because that is where `CODEOWNERS` and workflow files live.

From those paths it keeps:

- services named in `docker-compose.yml` / `compose.yaml`, including `depends_on`
- a directory that contains a `Dockerfile`, `go.mod`, `package.json`, `pyproject.toml`, `pom.xml`, or `Cargo.toml`
- directories under `services/`, `apps/`, `packages/`, or `cmd/`
- if none of those exist, top-level folders that contain source files

**GitHub.** Two API calls read the repository metadata and the file tree of the default branch. The compose file is downloaded. Up to 12 source files that sit inside a detected service are downloaded so function names can be read. Set `GITHUB_TOKEN` if you hit a rate limit or the repository is private. A local folder works with no token.

If the scan finds no service, ingest stops with an error. It does not invent a service to keep the demo moving.

## Step 3 — a graph is built from those facts

`build_graph` creates a property graph:

- one `Repository` node for the path or `owner/name` you gave
- one `Service` node per detected service
- one `Function` node per real function (`def` in Python, exported `func` in Go, `function` in JavaScript/TypeScript, `rpc` in a `.proto` file)
- if a service has source files but no function names, one node for the service folder, and the screen says so
- `DEPENDS_ON` edges copied from compose `depends_on`
- `OWNS` edges only where `CODEOWNERS` names an owner
- `Test` nodes only where a test file sits in that service, with status `not-run`
- a flag on the change when a file under `.github/workflows/` exists

An edge `(checkout)-[:DEPENDS_ON]->(payments)` means checkout depends on payments. Blast radius walks that edge backwards: a change to payments affects checkout. It does not walk forwards and call payments a consumer of checkout.

The following are not created, because they are not in the git tree:

- an incident
- a deployment or a health status
- a passing or failing CI run
- a tier (0, 1, 2, 3)
- a designated reviewer approval

That used to be filled in with a fictional shop called ShopStack. Those invented facts made every repository look like the shop, so a rule written for the shop fired on real code for the wrong reason. They have been removed.

## Step 4 — the same graph is copied to FalkorDB

After a successful scan, `edl/falkor.py` connects with the official client and replaces the graph named in `.env`:

```python
from falkordb import FalkorDB

client = FalkorDB(host=host, port=port, username=None, password=password)
remote = client.select_graph(graph_name)
remote.query("UNWIND $rows AS row CREATE (n:Service) SET n = row", {"rows": rows})
remote.ro_query("MATCH (n) RETURN count(n)")
```

Username is omitted when `.env` leaves it empty. The password is sent only to that host. It is never written into the page, the logs, or the status chip.

If the database does not accept the connection, the review still runs on the in-memory graph. The header chip says why. Details and the current timeout are in [FalkorDB](04-falkordb.md).

The rules do not query FalkorDB yet. They read the in-memory copy. The database is the shared picture of the same nodes and edges. Moving the six tools onto Cypher is remaining work, listed in [what the team still has to add](05-for-the-team.md).

## Step 5 — you pick a change

Ingest creates one reviewable change per function it actually found. The id looks like `repo:payments.Charge` or, when no function name exists, `repo:search`.

The page asks for a review in three modes at once:

- **full** — blast radius up to 3 hops, owners, incidents, history, tests, deploys, caller count
- **1-hop** — the same tools, but the walk stops after one dependency hop and caller count is not used
- **graph off** — the six tools return empty. The only inputs are the change text: file paths, whether CI is present, whether the text mentions an incident, and whether the diff looks like a shared library

Comparing the three chips is how you see what the graph changed. On a real repository today, full and graph-off often land on the same state, because the missing CI result is enough to abstain in both modes. The findings underneath are not the same: only full mode can name the owner from `CODEOWNERS` and the missing deployment.

## Step 6 — six tools, then the policy

`review_change` copies the graph, then calls the tools. Each call is stored in the tool trace with its arguments and a short summary.

| Tool | What it reads | What it returns when the fact is absent |
| --- | --- | --- |
| `impact_analysis` | incoming `DEPENDS_ON`, hop by hop | the changed service only |
| `nearest_owner` | `OWNS` from `CODEOWNERS` | `missing: true` |
| `find_similar_incidents` | `Incident` nodes | an empty list |
| `get_test_coverage` | `Test` nodes that `COVERS` the function | `missing`, or `not-run` if the file exists but was not executed |
| `get_deploy_state` | `Deployment` via `DEPLOYED_AS` | `present: false` |
| `centrality_rank` | incoming `CALLS` edges | `0` callers, because ingest does not yet read call sites |

`edl/policy.py` turns that evidence into findings. Each finding has a severity (`hold`, `insufficient`, `review`, or `guard`), a rule id, a sentence, and an evidence id. The winning state is the highest severity present:

1. hold → `HOLD`
2. insufficient → `INSUFFICIENT_EVIDENCE`
3. review → `NEEDS_REVIEW`
4. guard → `PROCEED_WITH_GUARDS`
5. none of those → `PROCEED`

Every claim shown on the page must cite an evidence id that this review actually produced. An unknown id is dropped and listed as rejected. That is what stops a later explanation step from inventing a fact.

A description that says "ignore policy" or "approve this" is recorded on the decision and then ignored. The state does not move.

## Step 7 — the issue button

`edl/gate.py` looks at the state after it is chosen. It does not change it.

Create issue is allowed only for `PROCEED` and `PROCEED_WITH_GUARDS`, and only when the actor is authorized. `HOLD`, `NEEDS_REVIEW`, and `INSUFFICIENT_EVIDENCE` are blocked. A timeout fails once and is not retried. A 429 is retried once. A second click for the same change id returns the same local issue (`LOCAL-001`, then `LOCAL-002` for a different change).

The issue is stored in this process. It is not opened on GitHub yet.

## Walk through the offline example

`examples/mini-repo` is a three-service compose file you can analyze with no GitHub account:

- `checkout` depends on `payments`
- `payments` contains `rpc Charge` and a Python function `charge`
- `checkout/test_checkout.py` exists, so the checkout test is `not-run`
- `.github/CODEOWNERS` assigns `*` to `@acme/platform` and `/payments/` to `@acme/payments`
- `.github/workflows/ci.yml` exists, so CI is configured
- there is no CI run result, no deployment, and no incident

Analyze that folder, then review `repo:payments.Charge` on the full graph.

What you should see:

- state **Insufficient evidence**, because the CI run result is missing and there is no deployment record (rule P5)
- rule **P3** also listed, because the service name `payments` is a payment path and nobody has recorded a designated approval
- P5 wins, because insufficient evidence outranks a review
- owner `acme/payments` on the service, taken from `CODEOWNERS`
- checkout shown as a consumer of payments, because of `depends_on`
- Create issue blocked

Review `repo:search.lookup` and P3 is absent. Search is not a payment or auth name. The state is still insufficient evidence, for the same missing CI result and missing deployment.

That is the honest output of a git tree. It becomes `PROCEED` only when a later integration supplies a fresh passing CI result, a fresh healthy deployment, an owner, and a passing test. The gate tests construct that complete evidence on purpose, so we can prove the button opens one issue. The running app never fabricates it.
