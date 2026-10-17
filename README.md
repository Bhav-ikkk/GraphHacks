# Syzygy EDL

Engineering decision layer for a code change. You choose the repository. The scan keeps only facts that tree contains, and the policy returns one of five states: proceed, proceed with guards, needs review, hold, or insufficient evidence.

A git repository does not contain CI run results, deployment health, or incidents. Those stay missing, and the state stays **insufficient evidence**, until a real source is connected. The explanation text cannot upgrade the state.

**Teammate onboarding (what / why / how + diagrams):** open [`docs/teammate-guide.html`](docs/teammate-guide.html) in a browser.

## Run it

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m pytest
python -m edl serve
```

Open http://127.0.0.1:8000

The page starts empty. Paste a GitHub URL or a folder on this machine and press Analyze. That repository is the only data the review uses.

```powershell
python -m edl ingest https://github.com/owner/name --files path/to/changed.py
python -m edl review https://github.com/owner/name --files path/to/changed.py
python -m edl eval
```

Inspect a real repo the way you would with graphify (status, path, evidence):

```powershell
python -m edl explore C:\path\to\repo --files payments/charge.py
python -m edl graph status C:\path\to\repo
python -m edl graph services C:\path\to\repo
python -m edl graph path C:\path\to\repo checkout payments
python -m edl graph explain C:\path\to\repo payments
python -m edl graph evidence C:\path\to\repo --files payments/charge.py
```

In the UI: Analyze the repo → click a **suggested file** (or paste paths) → Run review. The Evidence panel lists every catalog row claims cite. Empty Rescope no longer silently 400s — it tells you what to pass.

`python -m edl eval` runs labeled scenarios (safe / risky / uncertain) and scores
**unjustified PROCEED** plus graph lift vs graph-off. Fixtures never load into the live server.

### Agent + MCP (Track 01)

```powershell
# Offline agent (graph tools → decide → gate), no network
python -m edl agent --scenario graph_necessary_incident_hold

# Agent on a local repo + scoped files
python -m edl agent C:\path\to\repo --files payments/charge.py

# MCP stdio server for Cursor / Claude Desktop
python -m edl mcp
```

Cursor MCP example (`mcp.json`):

```json
{
  "mcpServers": {
    "syzygy-edl": {
      "command": "python",
      "args": ["-m", "edl", "mcp"],
      "cwd": "C:\\Users\\HP\\Desktop\\Projects\\big_projects\\FlakorDB_Hackathon"
    }
  }
}
```

Named tools only (impact, owner, incidents, tests, deploy, callers, decide, action). No free-text Cypher.

## FalkorDB

FalkorDB is the decision backbone. Reviews read blast radius, owners, tests, deploys, and incidents through named Cypher tools. If FalkorDB is not connected, the product fails closed with **insufficient evidence** — it does not invent a safe decision from an in-memory copy.

Copy `.env.example` to `.env`. Leave `FALKORDB_USERNAME` empty. Set `FALKORDB_PASSWORD`. Do not set `EDL_ALLOW_MEMORY_GRAPH` for demos (tests set it automatically).

The app signs in at [the browser on port 3000](http://13.236.193.238:3000/login) and writes the graph named in `FALKORDB_GRAPH`. Open that same site with `falkor://:<password>@localhost:6379` and select the graph. Details are in [docs/04-falkordb.md](docs/04-falkordb.md).

## Limits

There is no language-wide call-graph parser, no live CI or deploy feed, and no model in the decision path. Creating an issue stores a local record. The gate refuses the action when the state is hold, needs review, or insufficient evidence.
