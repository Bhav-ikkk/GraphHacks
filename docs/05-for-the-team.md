# What is built, and what is left

Run this from the repository root:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m pytest
python -m edl ingest examples/mini-repo
python -m edl review examples/mini-repo repo:payments.Charge
python -m edl serve
```

Then open http://127.0.0.1:8000 and analyze a repository you choose. `examples/mini-repo` is there so you can try the page with no GitHub account. It is not loaded on startup.

Copy `.env.example` to `.env` if you do not already have one. Leave the username empty. Set the password yourself. Do not commit `.env`.

## Built

- Empty start. You choose the GitHub URL or the folder. No fictional shop is loaded, and there is no ShopStack button.
- Scan of compose dependencies, service folders, Dockerfiles, source function names, `.proto` RPCs, `CODEOWNERS`, test files, and workflow files.
- Directional blast radius. A consumer points at the dependency it uses.
- Payment and auth detected from the service name (`payments`, `auth`, `billing`, `ledger`), not from a hardcoded shop name. `author` is not treated as `auth`.
- Deterministic policy, five states, fixed precedence, three evidence modes, claims that must cite a real evidence id.
- Prompt-injection text in a description is logged and does not change the state.
- Issue gate: only proceed and proceed-with-guards can open a local issue; a second click returns the same id.
- FalkorDB client matching the documented API: `FalkorDB(...)`, `select_graph`, `query`, `ro_query`. The graph named in `.env` is replaced with the repository you analyzed, when the database port accepts a connection.
- Tests for the repository path, the gate, and the client. `python -m pytest` does not call the network.

## Left, in the order that unblocks a real demo

1. **Reach the database.** Open port 6379 on the box that serves the browser, or put the FalkorDB Cloud host and port into `.env`. Confirm with `python -m edl falkor examples/mini-repo`, then look at that graph name in the browser. Nobody else can finish the Cypher tools until this works.

2. **CI run results.** Read GitHub Checks (or your CI) for the branch under review. Write `present`, `result`, and `age_hours` onto the change. Until this exists, every git-only review correctly stops at insufficient evidence.

3. **Deployments.** Read the deploy system for each affected service: age in hours, health `ok` or a real failure. Write a `Deployment` node and a `DEPLOYED_AS` edge. Do not default health to `ok`.

4. **Incidents.** Read the tracker. An open incident needs `status`, `age_days`, and `AFFECTED`. A resolved one needs a `Fix` and `MODIFIES` before P7 can fire. An incident mentioned only in the PR text must not be treated as a graph fact.

5. **Tiers, if you want P1 and the tier-0 holds.** Add an explicit source (a file in the repo, or a value a person sets and that is labeled as a human entry). Do not infer tier from the service name. The old name-based guess is what made the shop scenarios lie about real repositories.

6. **Run the six tools as Cypher** against the graph you published, with parameters, not with a query box. Keep `edl/policy.py` as the thing that picks the state. Compare a few reviews against the in-memory result before you delete the Python reads.

7. **Callers.** A real `CALLS` edge needs a parser or a compiler plugin. Until then R2 cannot fire, and the trace shows 0 callers. That is preferable to a guessed call.

8. **Test execution.** A test file is `not-run`. Wire the CI test report so status can be `pass` or `fail`. G-TEST should keep firing while the status is anything else.

9. **CODEOWNERS age.** Use the commit time of the CODEOWNERS file, not the fixed clock, so a stale owner actually trips P5.

10. **GitHub issues.** Replace the local `LOCAL-00N` record with an issue on a repository you control. Keep the gate in front of that call.

11. **More of a GitHub tree.** The URL path downloads at most 12 source files for function names. Clone, or raise the cap with a token, when you review a large repo.

12. **An explanation model, only after the tools are stable.** It may paraphrase the tool trace. It must not be able to change the state. The claim check (unknown evidence ids are dropped) stays in front of it.

## How to split it

| Person | Starts with | Done when |
| --- | --- | --- |
| Whoever has the FalkorDB host | Port and `.env`, then `edl falkor` | The browser shows the mini-repo services and the checkout → payments edge |
| Ingest | GitHub Checks and, if you want, a shallow clone | A review of a real PR includes a real CI result and age |
| Policy | Stay with `policies/policies.yaml` | New facts flow in as tool results. Rules are not special-cased per repository |
| UI | The page already reviews a chosen repo | Show the new CI and deploy facts in the trace without adding a free-text query box |

## Commands

| Command | Effect |
| --- | --- |
| `python -m edl serve` | Page at http://127.0.0.1:8000, empty until you analyze |
| `python -m edl ingest <url-or-folder>` | Print services, change ids, and the FalkorDB chip |
| `python -m edl review <url-or-folder> <change-id>` | Print the state, the rationale, and the three modes |
| `python -m edl trace <url-or-folder> <change-id>` | Print the six tool summaries |
| `python -m edl falkor <url-or-folder>` | Publish that repository and exit 1 if the database was not reached |
| `python -m pytest` | Repository behavior and the gate. No network |
