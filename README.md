# Syzygy EDL

Engineering decision layer for one code change in a repository you choose.

1. Scan the tree → structural graph (services, deps, owners, functions, tests).
2. Publish that graph to **FalkorDB**.
3. Agents call **named tools** (blast radius, owner, incidents, …) — never free Cypher.
4. **Policy** returns one state: proceed, proceed with guards, needs review, hold, or insufficient evidence.
5. A **gate** blocks unsafe side effects.

A bare git tree does not contain CI runs, deploy health, or incidents. Those stay missing until a real source provides them (GitHub Actions via `GITHUB_TOKEN`, or an optional [`syzygy.evidence.yaml`](syzygy.evidence.yaml) you fill with observed facts). Eval fixtures under `edl/eval/` are for scoring only and never load into the live UI or `serve`.

**Teammate onboarding:** open [`docs/teammate-guide.html`](docs/teammate-guide.html).

## Install (Windows)

```powershell
cd C:\Users\HP\Desktop\Projects\big_projects\FlakorDB_Hackathon
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e .          # installs `syzygy` and `edl` commands
copy .env.example .env    # set FALKORDB_PASSWORD + Gemini key
```

Prefer the venv python (or `syzygy` after `pip install -e .`). On Windows, bare `py -m edl` can skip the venv — use:

```powershell
.\.venv\Scripts\python.exe -m edl --version
# or after editable install:
syzygy --version
```

## Product CLI (real repos)

```powershell
# 0) Product + Falkor readiness
python -m edl --version
python -m edl status

# 1) Publish a repo graph to FalkorDB (no decision yet)
python -m edl ingest .
python -m edl ingest https://github.com/owner/name

# 2) Full product report: graph → decide → claims → next steps
python -m edl report . --files edl/agent.py
python -m edl report C:\path\to\other-repo --files src/main.py
python -m edl report https://github.com/owner/name --files path/file.py

# 3) Agent on a real change
python -m edl agent . --files edl/agent.py

# 4) Owner notify draft + GitHub PR comment
python -m edl notify . --files edl/agent.py
python -m edl pr-comment . --pr 42 --files edl/agent.py --dry-run
# python -m edl pr-comment . --pr 42 --files edl/agent.py   # needs GITHUB_TOKEN

# 5) UI
python -m edl serve
# → http://127.0.0.1:8000
# Analyze → scope files → Run review (or Run agent)
# Compare Full graph / 1-hop / Graph off — click the mode chips after review
```

### Evidence modes (what the three chips mean)

| Mode | What it uses | Why it exists |
| --- | --- | --- |
| **Full graph** | Multi-hop dependents, owners, incidents, history | Real decision |
| **1-hop only** | Direct neighbors only | Ablation — hides deep blast radius |
| **Graph off** | Diff/CI text, no relationship walk | Proves *graph lift* when Full is stricter |

Policy still decides. The LLM/agent may choose tools and narrate; it cannot upgrade HOLD / REVIEW / INSUFFICIENT.

Exit codes for `report`: `0` proceed/guards, `2` hold/review/insufficient, `1` hard error.

Offline eval (fixture scoring only — not the live product):

```powershell
python -m edl eval
```

## Agent + MCP

```powershell
python -m edl mcp --check          # self-check; hosts start mcp without --check
python -m edl agent . --files edl/agent.py --llm
```

Project MCP configs (already in repo):

| Host | File |
|---|---|
| Cursor | `.cursor/mcp.json` |
| VS Code | `.vscode/mcp.json` |
| Claude Code | `.mcp.json` |
| Codex | `.codex/config.toml` |

In Cursor chat (with `syzygy-edl` enabled):

> Ingest this repository, scope `edl/agent.py`, run impact and incidents, then decide the change.

## Graphify + Syzygy

| | Graphify | Syzygy |
|---|---|---|
| Question | What is in this repo? | May this change ship? |
| Output | Subgraph / answer nodes | State + evidence ids + gate |
| Tokens | Small graph slice to the model | Structured tool envelopes |

```powershell
graphify .
graphify query "what depends on the edl package?"
python -m edl agent . --files edl/agent.py --llm
```

## Operational evidence

Structural facts come from the tree. CI / deploy / incident / tier / approval facts may be declared in:

- `syzygy.evidence.yaml`, or
- `.syzygy/evidence.yaml`

Without that file (or a live feed wired later), reviews correctly abstain with **insufficient evidence**.

## FalkorDB

Reviews read through named tools against FalkorDB. If FalkorDB is not ready, the product fails closed — it does not invent a safe decision from an in-memory copy.

Copy `.env.example` → `.env`. Leave `FALKORDB_USERNAME` empty. Set `FALKORDB_PASSWORD`. Do not set `EDL_ALLOW_MEMORY_GRAPH` for demos.

### Gemini (Google AI Studio)

In `.env` (already mapped for this project):

```text
SYZYGY_LLM_PROVIDER=gemini
SYZYGY_LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
SYZYGY_LLM_MODEL=gemini-3.8-flash
SYZYGY_LLM_API_KEY=<paste AI Studio key>
```

```powershell
python -m edl llm-check
python -m edl agent . --files edl/agent.py --llm
```

You want `planner=remote` (not `local`). The model picks tools; **`decide_change` + policy still set the state.**

### Live CI (GitHub Actions)

`.github/workflows/ci.yml` runs pytest. When the repo is on GitHub, ingest merges the latest Actions run into the same `ci{}` contract as `syzygy.evidence.yaml` (live result wins). Optional: `GITHUB_TOKEN` in `.env` for private repos.

### Call graph

Local Python ingest adds `CALLS` edges (AST name match) so `centrality_rank` / hub rules are not empty on this codebase.

## Cursor MCP demo (2 minutes)

1. Enable `syzygy-edl` from `.cursor/mcp.json` (Settings → MCP).
2. Chat: `Ingest this repository, scope edl/agent.py, check incidents, then decide_change.`
3. Confirm tools run and the state is HOLD while the open incident exists.
4. Screenshot tool calls + decision for submission.

## Limits

Call graphs are Python-only AST matches (not a full interprocedural analyzer). Deploy/incident feeds still come from `syzygy.evidence.yaml` or future adapters. Creating an issue stores a local record. The gate refuses when the state is hold, needs review, or insufficient evidence.
