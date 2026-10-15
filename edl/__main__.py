"""CLI: ingest a repository you choose, review one change, publish the graph, serve the UI."""

from __future__ import annotations

from pathlib import Path

import typer

from edl.engine import Engine
from edl.ingest import IngestError

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "policies" / "policies.yaml"
LOG = ROOT / "logs" / "trace.jsonl"

app = typer.Typer(add_completion=False, help="Syzygy engineering decision layer")


def _engine() -> Engine:
    return Engine(POLICY, LOG)


@app.command()
def ingest(target: str) -> None:
    """Read a GitHub repo or a local folder. You choose which one."""
    engine = _engine()
    try:
        summary = engine.use_repository(target)
    except IngestError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1)
    typer.echo(
        f"Loaded {summary['origin']['value']}: {summary['nodes']} nodes, "
        f"{len(summary['services'])} services, {summary['changes']} reviewable changes."
    )
    typer.echo(summary["note"])
    for change in engine.changes:
        typer.echo(f"  {change['id']}")
    remote = engine.snapshot()["falkor"]
    typer.echo(remote.get("detail") or "")


@app.command()
def review(target: str, change_id: str, mode: str = "full") -> None:
    """Review one change from a repository you choose."""
    engine = _loaded(target)
    try:
        payload = engine.review(change_id, mode)
    except KeyError:
        typer.echo(f"No change named {change_id}.")
        typer.echo("Changes found:")
        for change in engine.changes:
            typer.echo(f"  {change['id']}")
        raise typer.Exit(code=1)
    record = payload["runs"][mode]
    typer.echo(f"{record['change_id']}  {mode}  {record['state']}")
    typer.echo(record["rationale"])
    for claim in record["claims"]:
        typer.echo(f"  [{claim['evidence_id']}] {claim['text']}")
    others = ", ".join(f"{name}={item['state']}" for name, item in payload["runs"].items())
    typer.echo(others)


@app.command()
def trace(target: str, change_id: str, mode: str = "full") -> None:
    """Print the tool-call log for one review."""
    engine = _loaded(target)
    try:
        payload = engine.review(change_id, mode)
    except KeyError:
        typer.echo(f"No change named {change_id}.")
        raise typer.Exit(code=1)
    for call in payload["runs"][mode]["tool_calls"]:
        typer.echo(f"{call['tool']}: {call['summary']}")


@app.command()
def falkor(target: str) -> None:
    """Load a repository into the FalkorDB graph named in .env."""
    from edl.falkor import publish
    from edl.ingest import ingest as read_repo

    try:
        graph, _changes, summary = read_repo(target)
    except IngestError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1)
    result = publish(graph, force=True)
    typer.echo(summary["origin"]["value"])
    typer.echo(result["detail"])
    if not result.get("ok"):
        raise typer.Exit(code=1)


@app.command()
def serve(port: int = 8000) -> None:
    """Start the review UI. It stays empty until you analyze a repository."""
    import uvicorn

    typer.echo(f"Syzygy EDL at http://127.0.0.1:{port}")
    uvicorn.run("edl.api:app", host="127.0.0.1", port=port, reload=False)


def _loaded(target: str) -> Engine:
    engine = _engine()
    try:
        engine.use_repository(target)
    except IngestError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1)
    return engine


if __name__ == "__main__":
    app()
