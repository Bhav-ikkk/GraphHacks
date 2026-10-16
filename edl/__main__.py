"""CLI: ingest a repository, scope one change, review, publish the graph, serve the UI."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from edl.change import ChangeError
from edl.engine import Engine
from edl.explore import (
    explain as explain_node,
    format_evidence,
    format_inspect,
    neighbors as neighbor_card,
    resolve_node_id,
    service_cards,
    shortest_path,
)
from edl.ingest import IngestError

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "policies" / "policies.yaml"
LOG = ROOT / "logs" / "trace.jsonl"

app = typer.Typer(add_completion=False, help="Syzygy engineering decision layer")
graph_app = typer.Typer(add_completion=False, help="Inspect the repository graph (graphify-style)")
app.add_typer(graph_app, name="graph")


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


@app.command("eval")
def evaluate(
    scenario: Optional[str] = typer.Option(
        None, help="Comma-separated scenario ids; default runs all"
    ),
) -> None:
    """Run the labeled evaluation harness (never loads fixtures into the live server)."""
    from edl.eval.harness import format_report, run_eval

    ids = [part.strip() for part in scenario.split(",")] if scenario else None
    result = run_eval(scenario_ids=ids)
    typer.echo(format_report(result))
    if not result["summary"]["pass"]:
        raise typer.Exit(code=1)


@app.command()
def agent(
    target: Optional[str] = typer.Argument(None, help="GitHub URL or local folder"),
    files: Optional[str] = typer.Option(None, help="Comma-separated changed paths"),
    base: Optional[str] = typer.Option(None, help="Git base ref"),
    head: Optional[str] = typer.Option(None, help="Git head ref"),
    scenario: Optional[str] = typer.Option(
        None, help="Offline eval scenario id (no network)"
    ),
    mode: str = typer.Option("full", help="full | one_hop | graph_off"),
    act: bool = typer.Option(False, help="Execute gated local issue when allowed"),
    hops: int = typer.Option(3, help="Blast-radius hop limit"),
) -> None:
    """Run the graph-backed change agent (tool selection → decide → gate)."""
    from edl.agent import format_agent_report, run_agent

    if not scenario and not target:
        typer.echo("Pass a repository target, or --scenario <id> for an offline fixture.")
        raise typer.Exit(code=1)
    result = run_agent(
        target,
        files=_split_files(files),
        base=base,
        head=head,
        mode=mode,
        execute_action=act,
        hops=hops,
        scenario=scenario,
    )
    typer.echo(format_agent_report(result))
    if not result.get("ok"):
        raise typer.Exit(code=1)


@app.command("mcp")
def mcp_serve() -> None:
    """Start the MCP stdio server (graph tools for Cursor / Claude / agents)."""
    from edl.mcp_server import main as mcp_main

    mcp_main()


@app.command()
def explore(
    target: str,
    base: Optional[str] = typer.Option(None, help="Git base ref"),
    head: Optional[str] = typer.Option(None, help="Git head ref"),
    files: Optional[str] = typer.Option(None, help="Comma-separated modified paths"),
    mode: str = typer.Option("full", help="full | one_hop | graph_off"),
    review: bool = typer.Option(True, help="Run review and dump evidence when a change is scoped"),
) -> None:
    """Inspect a real repo: services, scope, suggested files, and evidence (like graphify status)."""
    engine = _loaded(target, base=base, head=head, files=_split_files(files))
    snap = engine.snapshot()
    cards = service_cards(engine.graph)
    for card, service in zip(cards, snap["services"]):
        card["owner"] = service.get("owner")
    typer.echo(
        format_inspect(
            origin=snap.get("origin"),
            note=snap.get("note") or "",
            scope_note=snap.get("scope_note") or "",
            scope_error=snap.get("scope_error"),
            suggested_files=snap.get("suggested_files") or [],
            services=cards,
            changes=snap.get("changes") or [],
            backend=snap.get("backend") or "",
            nodes=snap.get("nodes") or 0,
            edges=snap.get("edges") or 0,
        )
    )
    if not review or not engine.changes:
        return
    selected = engine.changes[0]["id"]
    payload = engine.review(selected, mode)
    typer.echo("")
    typer.echo(format_evidence(payload["runs"][mode]))


@graph_app.command("status")
def graph_status(target: str) -> None:
    """Graph counts, Falkor status, and scoped change summary."""
    engine = _loaded(target, files=None)
    snap = engine.snapshot()
    remote = snap.get("falkor") or {}
    typer.echo(f"backend={snap['backend']}  ready={snap.get('graph_ready')}")
    typer.echo(remote.get("detail") or "")
    typer.echo(f"nodes={snap['nodes']}  edges={snap['edges']}  services={len(snap['services'])}")
    typer.echo(snap.get("scope_note") or "")
    if snap.get("suggested_files"):
        typer.echo("suggested_files: " + ", ".join(snap["suggested_files"][:10]))


@graph_app.command("services")
def graph_services(target: str) -> None:
    """List services with owners, deps, and sample source paths."""
    engine = _loaded(target, files=None)
    cards = service_cards(engine.graph)
    owners = {item["name"]: item.get("owner") for item in engine.services()}
    for card in cards:
        card["owner"] = owners.get(card["name"])
        owner = card.get("owner") or "no CODEOWNERS entry"
        paths = ", ".join(card.get("sample_paths") or []) or "(none)"
        deps = ", ".join(card.get("depends_on") or []) or "(none)"
        consumers = ", ".join(card.get("consumers") or []) or "(none)"
        typer.echo(f"{card['name']}\towner={owner}\tdeps={deps}\tconsumers={consumers}\tfiles={paths}")


@graph_app.command("neighbors")
def graph_neighbors(target: str, node: str) -> None:
    """Show outgoing and incoming edges for a service or function."""
    engine = _loaded(target, files=None)
    node_id = resolve_node_id(engine.graph, node)
    if not node_id:
        typer.echo(f"No node matching {node!r}.")
        raise typer.Exit(code=1)
    card = neighbor_card(engine.graph, node_id)
    typer.echo(f"{card['label']} {card['name']} ({card['id']})")
    for item in card["out"]:
        typer.echo(f"  -[{item['rel']}]-> {item['name'] or item['id']}")
    for item in card["inn"]:
        typer.echo(f"  <-[{item['rel']}]- {item['name'] or item['id']}")


@graph_app.command("path")
def graph_path(target: str, start: str, end: str) -> None:
    """Shortest path between two nodes (services/functions)."""
    engine = _loaded(target, files=None)
    start_id = resolve_node_id(engine.graph, start)
    end_id = resolve_node_id(engine.graph, end)
    if not start_id or not end_id:
        typer.echo(f"Could not resolve path ends: {start!r} -> {start_id}, {end!r} -> {end_id}")
        raise typer.Exit(code=1)
    result = shortest_path(engine.graph, start_id, end_id)
    if not result.get("ok"):
        typer.echo(result.get("detail") or "No path.")
        raise typer.Exit(code=1)
    bits = []
    nodes = result["nodes"]
    edges = result.get("edges") or []
    for index, node_id in enumerate(nodes):
        node = engine.graph.node(node_id) or {}
        bits.append(str(node.get("name") or node_id))
        if index < len(edges):
            bits.append(f"-[{edges[index]}]->")
    typer.echo(" ".join(bits))
    typer.echo(f"hops={result['hops']}")


@graph_app.command("explain")
def graph_explain(target: str, node: str) -> None:
    """Plain-language explanation of one graph node."""
    engine = _loaded(target, files=None)
    node_id = resolve_node_id(engine.graph, node)
    if not node_id:
        typer.echo(f"No node matching {node!r}.")
        raise typer.Exit(code=1)
    typer.echo(explain_node(engine.graph, node_id)["summary"])


@graph_app.command("evidence")
def graph_evidence(
    target: str,
    files: Optional[str] = typer.Option(None, help="Comma-separated modified paths"),
    base: Optional[str] = typer.Option(None, help="Git base ref"),
    head: Optional[str] = typer.Option(None, help="Git head ref"),
    mode: str = typer.Option("full", help="full | one_hop | graph_off"),
) -> None:
    """Run a review and print the evidence catalog + claims."""
    engine = _loaded(target, base=base, head=head, files=_split_files(files))
    if not engine.changes:
        snap = engine.snapshot()
        typer.echo(snap.get("scope_error") or "No change is scoped.")
        if snap.get("suggested_files"):
            typer.echo("Try --files " + ",".join(snap["suggested_files"][:4]))
        raise typer.Exit(code=1)
    payload = engine.review(engine.changes[0]["id"], mode)
    typer.echo(format_evidence(payload["runs"][mode]))


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
