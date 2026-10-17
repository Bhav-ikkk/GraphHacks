"""Syzygy CLI — ingest repos, publish FalkorDB graphs, decide changes, serve the UI."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from edl.branding import PRODUCT_LONG, VERSION, banner, usage
from edl.change import ChangeError
from edl.cli_report import format_decision_report, format_ingest_report, format_status
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

app = typer.Typer(
    add_completion=False,
    invoke_without_command=True,
    help=f"{PRODUCT_LONG} - graph-backed engineering decisions on FalkorDB",
)
graph_app = typer.Typer(
    add_completion=False,
    help="Inspect the published repository graph (services, paths, evidence)",
)
app.add_typer(graph_app, name="graph")


def _engine() -> Engine:
    return Engine(POLICY, LOG)


def _split_files(files: Optional[str]) -> list[str] | None:
    if not files:
        return None
    return [part.strip() for part in files.split(",") if part.strip()]


@app.callback()
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show product version and exit.",
        is_eager=True,
    ),
) -> None:
    """Syzygy EDL command line."""
    if version:
        typer.echo(f"{PRODUCT_LONG} {VERSION}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        typer.echo(banner())
        typer.echo(usage())
        raise typer.Exit()


@app.command()
def ingest(
    target: str,
    base: Optional[str] = typer.Option(None, help="Git base ref"),
    head: Optional[str] = typer.Option(None, help="Git head ref"),
    files: Optional[str] = typer.Option(None, help="Comma-separated modified paths"),
) -> None:
    """Scan a repo, publish its graph to FalkorDB, optionally scope a change."""
    engine = _engine()
    try:
        summary = engine.use_repository(
            target,
            base=base,
            head=head,
            files=_split_files(files),
        )
    except IngestError as exc:
        typer.echo(banner())
        typer.echo(str(exc))
        raise typer.Exit(code=1)
    typer.echo(format_ingest_report(engine, summary))
    if not engine.snapshot().get("graph_ready"):
        typer.echo("\nFalkorDB is not ready. Fix .env (host/password) then re-run ingest.")
        raise typer.Exit(code=1)


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
    """Decide one scoped change and print an explainable report."""
    engine = _loaded(target, base=base, head=head, files=_split_files(files))
    if not engine.changes:
        typer.echo(banner())
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
    typer.echo(
        format_decision_report(
            engine,
            record,
            action=record.get("action"),
            modes=payload.get("runs"),
        )
    )


@app.command()
def report(
    target: str,
    files: Optional[str] = typer.Option(None, help="Comma-separated changed paths"),
    base: Optional[str] = typer.Option(None, help="Git base ref"),
    head: Optional[str] = typer.Option(None, help="Git head ref"),
    mode: str = typer.Option("full", help="full | one_hop | graph_off"),
) -> None:
    """One-shot product command: publish graph → decide → explain → agent node pack."""
    engine = _loaded(target, base=base, head=head, files=_split_files(files))
    if not engine.changes:
        snap = engine.snapshot()
        typer.echo(format_ingest_report(engine, {
            "origin": snap.get("origin"),
            "nodes": snap.get("nodes"),
            "edges": snap.get("edges"),
            "services": snap.get("services") or [],
            "scope_error": snap.get("scope_error"),
            "suggested_files": snap.get("suggested_files") or [],
        }))
        typer.echo("\nPass --files path/to/changed.py (or --base/--head) to decide a change.")
        raise typer.Exit(code=1)
    selected = engine.changes[0]["id"]
    payload = engine.review(selected, mode)
    record = payload["runs"][mode]
    typer.echo(
        format_decision_report(
            engine,
            record,
            action=record.get("action"),
            modes=payload.get("runs"),
        )
    )
    if record.get("state") in {"HOLD", "NEEDS_REVIEW", "INSUFFICIENT_EVIDENCE"}:
        raise typer.Exit(code=2)


@app.command()
def status() -> None:
    """Show product + FalkorDB readiness (no repository required)."""
    from edl import falkor

    engine = _engine()
    typer.echo(format_status(engine))
    remote = falkor.status()
    typer.echo(f"  configured {remote.get('configured')}")
    typer.echo(f"  host       {remote.get('host')}:{remote.get('port')}")
    typer.echo(f"  browser    {remote.get('browser')}")


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
    llm: bool = typer.Option(
        False,
        "--llm",
        help="Let a model choose tools and narrate. Policy still sets the state.",
    ),
) -> None:
    """Run the graph-backed change agent (tool selection → decide → gate)."""
    from edl.agent import format_agent_report, run_agent

    if not scenario and not target:
        typer.echo("Pass a repository target, or --scenario <id> for an offline fixture.")
        raise typer.Exit(code=1)
    if llm:
        from edl.llm_agent import run_llm_agent

        result = run_llm_agent(
            target,
            files=_split_files(files),
            base=base,
            head=head,
            mode=mode,
            execute_action=act,
            hops=hops,
            scenario=scenario,
        )
    else:
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
    typer.echo(banner())
    typer.echo(format_agent_report(result))
    if not result.get("ok"):
        raise typer.Exit(code=1)


@app.command("mcp")
def mcp_serve(
    check: bool = typer.Option(
        False,
        "--check",
        help="Print a self-check and exit. A terminal does this automatically.",
    ),
) -> None:
    """Start the MCP stdio server (graph tools for Cursor / Claude / agents)."""
    from edl.mcp_server import main as mcp_main

    mcp_main(check=check)


@app.command("llm-check")
def llm_check() -> None:
    """Verify Gemini / OpenAI-compatible settings without running a full agent."""
    from edl.llm_agent import chat_completion, llm_settings

    settings = llm_settings()
    if not settings.remote:
        typer.echo(
            "No remote LLM configured. Set SYZYGY_LLM_PROVIDER=gemini and "
            "SYZYGY_LLM_API_KEY (AI Studio key), or SYZYGY_LLM_BASE_URL + key."
        )
        raise typer.Exit(code=1)
    typer.echo(
        f"provider={settings.provider or 'custom'}  model={settings.model}  "
        f"base={settings.base_url}"
    )
    message = chat_completion(
        [
            {
                "role": "user",
                "content": "Reply with exactly one word: pong.",
            }
        ],
        settings,
        with_tools=False,
    )
    text = str(message.get("content") or "").strip()
    typer.echo(f"assistant: {text[:200]}")
    if "pong" not in text.lower() and not message.get("tool_calls"):
        typer.echo("Warning: unexpected reply, but the endpoint answered.")
    else:
        typer.echo("LLM endpoint OK.")


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
    """Start the Syzygy UI. Analyze a repo, scope files, run the decision."""
    import uvicorn

    typer.echo(banner())
    typer.echo(f"UI          http://127.0.0.1:{port}")
    typer.echo("Flow        Analyze repo → scope files → Run review")
    typer.echo("Graph       Published to FalkorDB from .env on each Analyze")
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


def run() -> None:
    """Console-script entry point (`syzygy` / `python -m edl`)."""
    app()


if __name__ == "__main__":
    run()
