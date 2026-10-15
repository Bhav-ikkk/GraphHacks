"""CLI: ingest a repository, scope one change, review, publish the graph, serve the UI."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from edl.change import ChangeError
from edl.engine import Engine
from edl.ingest import IngestError

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "policies" / "policies.yaml"
LOG = ROOT / "logs" / "trace.jsonl"

app = typer.Typer(add_completion=False, help="Syzygy engineering decision layer")


def _engine() -> Engine:
    return Engine(POLICY, LOG)


def _split_files(files: Optional[str]) -> list[str] | None:
    if not files:
        return None
    return [part.strip() for part in files.split(",") if part.strip()]


@app.command()
def ingest(
    target: str,
    base: Optional[str] = typer.Option(None, help="Git base ref"),
    head: Optional[str] = typer.Option(None, help="Git head ref"),
    files: Optional[str] = typer.Option(None, help="Comma-separated modified paths"),
) -> None:
    """Read a repository, then scope one change from git or explicit files."""
    engine = _engine()
    try:
        summary = engine.use_repository(
            target,
            base=base,
            head=head,
            files=_split_files(files),
        )
    except IngestError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1)
    typer.echo(
        f"Loaded {summary['origin']['value']}: {summary['nodes']} nodes, "
        f"{len(summary['services'])} services."
    )
    typer.echo(summary["note"])
    if summary.get("scope_error"):
        typer.echo(f"Change not scoped: {summary['scope_error']}")
    else:
        typer.echo(summary.get("scope_note") or "")
    for service in summary["services"]:
        owner = service.get("owner") or "no CODEOWNERS entry"
        typer.echo(f"  {service['name']}  owner: {owner}")
    for change in engine.changes:
        typer.echo(f"  change {change['id']}  mapping={change.get('mapping')}")
        typer.echo(f"    symbols: {', '.join(change.get('symbols') or []) or '(none)'}")
        unresolved = change.get("unresolved_refs") or []
        if unresolved:
            typer.echo(f"    unresolved: {', '.join(unresolved)}")
    remote = engine.snapshot()["falkor"]
    typer.echo(remote.get("detail") or "")


@app.command("change")
def scope(
    target: str,
    base: Optional[str] = typer.Option(None, help="Git base ref"),
    head: Optional[str] = typer.Option(None, help="Git head ref"),
    files: Optional[str] = typer.Option(None, help="Comma-separated modified paths"),
) -> None:
    """Re-scope the change for an already analyzable repository path/URL."""
    engine = _loaded(target, base=None, head=None, files=None)
    try:
        result = engine.scope_change(base=base, head=head, files=_split_files(files))
    except ChangeError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1)
    change = result["change"]
    typer.echo(f"{change['id']}  mapping={change.get('mapping')}")
    typer.echo(change.get("note") or "")
    typer.echo(f"symbols: {', '.join(change.get('symbols') or []) or '(none)'}")


@app.command()
def review(
    target: str,
    change_id: Optional[str] = typer.Argument(None),
    mode: str = "full",
    base: Optional[str] = typer.Option(None, help="Git base ref"),
    head: Optional[str] = typer.Option(None, help="Git head ref"),
    files: Optional[str] = typer.Option(None, help="Comma-separated modified paths"),
) -> None:
    """Review the scoped change (or scope first via --files / --base / --head)."""
    engine = _loaded(target, base=base, head=head, files=_split_files(files))
    if not engine.changes:
        typer.echo("No change is scoped. Pass --files or --base/--head for a local git folder.")
        raise typer.Exit(code=1)
    selected = change_id or engine.changes[0]["id"]
    try:
        payload = engine.review(selected, mode)
    except KeyError:
        typer.echo(f"No change named {selected}.")
        typer.echo("Changes found:")
        for change in engine.changes:
            typer.echo(f"  {change['id']}")
        raise typer.Exit(code=1)
    record = payload["runs"][mode]
    typer.echo(f"{record['change_id']}  {mode}  {record['state']}")
    typer.echo(record["rationale"])
    typer.echo(f"graph_revision={record.get('graph_revision')}  evidence={len(record.get('evidence') or [])}")
    for claim in record["claims"]:
        typer.echo(f"  [{claim['evidence_id']}] {claim['text']}")
    others = ", ".join(f"{name}={item['state']}" for name, item in payload["runs"].items())
    typer.echo(others)


@app.command()
def trace(
    target: str,
    change_id: Optional[str] = typer.Argument(None),
    mode: str = "full",
    files: Optional[str] = typer.Option(None, help="Comma-separated modified paths"),
) -> None:
    """Print the tool-call log for one review."""
    engine = _loaded(target, files=_split_files(files))
    if not engine.changes:
        typer.echo("No change is scoped.")
        raise typer.Exit(code=1)
    selected = change_id or engine.changes[0]["id"]
    try:
        payload = engine.review(selected, mode)
    except KeyError:
        typer.echo(f"No change named {selected}.")
        raise typer.Exit(code=1)
    for call in payload["runs"][mode]["tool_calls"]:
        typer.echo(f"{call['tool']}: {call['summary']}")


@app.command()
def falkor(target: str) -> None:
    """Load a repository into the FalkorDB graph named in .env."""
    from edl.falkor import publish
    from edl.ingest import ingest as read_repo

    try:
        graph, summary = read_repo(target)
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


def _loaded(
    target: str,
    *,
    base: str | None = None,
    head: str | None = None,
    files: list[str] | None = None,
) -> Engine:
    engine = _engine()
    try:
        engine.use_repository(target, base=base, head=head, files=files)
    except IngestError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1)
    return engine


if __name__ == "__main__":
    app()
