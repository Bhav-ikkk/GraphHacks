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

The page starts empty. Paste a GitHub URL or a folder and press Analyze. `examples/mini-repo` is a small compose tree you can use offline. It is not loaded for you.

```powershell
python -m edl ingest examples/mini-repo
python -m edl review examples/mini-repo repo:payments.Charge
python -m edl falkor examples/mini-repo
```

## FalkorDB

Copy `.env.example` to `.env` if you need a fresh file. Leave `FALKORDB_USERNAME` empty. The only value you set is `FALKORDB_PASSWORD`.

The app signs in at [the browser on port 3000](http://13.236.193.238:3000/login) and writes the graph named in `FALKORDB_GRAPH`. Open that same site with `falkor://:<password>@localhost:6379` and select the graph. Details are in [docs/04-falkordb.md](docs/04-falkordb.md).

## Limits

There is no language-wide call-graph parser, no live CI or deploy feed, and no model in the decision path. Creating an issue stores a local record. The gate refuses the action when the state is hold, needs review, or insufficient evidence.
