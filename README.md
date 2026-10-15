# Syzygy EDL

Engineering decision layer for a code change. You choose the repository. The scan keeps only facts that tree contains, and the policy returns one of five states: proceed, proceed with guards, needs review, hold, or insufficient evidence.

A git repository does not contain CI run results, deployment health, or incidents. Those stay missing, and the state stays **insufficient evidence**, until a real source is connected. The explanation text cannot upgrade the state.

The full walkthrough is in [docs/README.md](docs/README.md).

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
python -m edl ingest https://github.com/owner/name
python -m edl review https://github.com/owner/name repo:service.function
```

## FalkorDB

FalkorDB is the decision backbone. Reviews read blast radius, owners, tests, deploys, and incidents through named Cypher tools. If FalkorDB is not connected, the product fails closed with **insufficient evidence** — it does not invent a safe decision from an in-memory copy.

Copy `.env.example` to `.env`. Leave `FALKORDB_USERNAME` empty. Set `FALKORDB_PASSWORD`. Do not set `EDL_ALLOW_MEMORY_GRAPH` for demos (tests set it automatically).

The app signs in at [the browser on port 3000](http://13.236.193.238:3000/login) and writes the graph named in `FALKORDB_GRAPH`. Open that same site with `falkor://:<password>@localhost:6379` and select the graph. Details are in [docs/04-falkordb.md](docs/04-falkordb.md).

## Limits

There is no language-wide call-graph parser, no live CI or deploy feed, and no model in the decision path. Creating an issue stores a local record. The gate refuses the action when the state is hold, needs review, or insufficient evidence.
