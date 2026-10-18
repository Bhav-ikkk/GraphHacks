"""Minimal engineering-change agent.

Tool selection is deliberate and graph-first: load/scope → multi-hop reads →
decide → gated action. No free-text Cypher. An LLM host can call the same tools
via MCP; this agent proves the loop without requiring a model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from edl.engine import Engine
from edl.progress import step as progress
from edl.tools import ToolContext, call_tool

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "policies" / "policies.yaml"
LOG = ROOT / "logs" / "agent.jsonl"

# Per-function Falkor round-trips add up fast on a remote host.
MAX_SYMBOL_PROBES = 6


def load_scenario(scenario_id: str, engine: Engine | None = None) -> Engine:
    """Load a labeled eval fixture into an engine (for offline agent demos/tests)."""
    import os

    from edl.eval.scenarios import all_scenarios

    match = next((item for item in all_scenarios() if item["id"] == scenario_id), None)
    if match is None:
        known = ", ".join(item["id"] for item in all_scenarios())
        raise KeyError(f"Unknown scenario {scenario_id!r}. Known: {known}")
    # Offline fixtures are not a live Falkor publish; label the memory double.
    os.environ.setdefault("EDL_ALLOW_MEMORY_GRAPH", "1")
    active = engine or Engine(POLICY, LOG)
    active.graph = match["graph"].copy()
    active.changes = [dict(match["change"])]
    active.dataset = "eval-fixture"
    active.origin = {"kind": "eval", "value": scenario_id}
    active.note = match.get("notes") or match["title"]
    active.scope_note = f"Loaded eval scenario {scenario_id}."
    active.ci_configured = bool((match["change"].get("ci") or {}).get("configured"))
    active.workflow_meta = {
        "id": match.get("id"),
        "title": match.get("title"),
        "persona": match.get("persona"),
        "story": match.get("story"),
        "family": match.get("family"),
    }
    return active


def run_agent(
    target: str | None = None,
    *,
    files: list[str] | None = None,
    base: str | None = None,
    head: str | None = None,
    change_id: str | None = None,
    mode: str = "full",
    execute_action: bool = False,
    engine: Engine | None = None,
    hops: int = 3,
    scenario: str | None = None,
) -> dict[str, Any]:
    """Run one agent episode and return an explainable transcript."""
    if scenario:
        active = load_scenario(scenario, engine)
    else:
        active = engine or Engine(POLICY, LOG)
    ctx = ToolContext(active)
    trace: list[dict] = []

    def step(name: str, arguments: dict | None = None) -> dict:
        progress(f"Agent tool: {name}")
        payload = call_tool(ctx, name, arguments)
        trace.append({"tool": name, "arguments": arguments or {}, "response": payload})
        return payload

    if scenario:
        pass
    elif target:
        step(
            "ingest_repository",
            {
                "target": target,
                "files": files,
                "base": base,
                "head": head,
            },
        )
    elif files or base or head:
        step("scope_change", {"files": files, "base": base, "head": head})

    listed = step("list_changes", {})
    changes = (listed.get("result") or {}).get("changes") or []
    if not changes and active.changes:
        changes = [
            {
                "id": item["id"],
                "symbols": item.get("symbols") or [],
                "title": item.get("title"),
            }
            for item in active.changes
        ]
    if not changes:
        return {
            "ok": False,
            "error": "No scoped change. Pass --files, a git base/head, or --scenario.",
            "tool_trace": trace,
        }

    selected = change_id or changes[0]["id"]
    change_meta = next((item for item in changes if item["id"] == selected), changes[0])
    symbols = list(change_meta.get("symbols") or [])

    # Derive direct services from symbols (svc:name from name.fn).
    services: list[str] = []
    for symbol in symbols:
        service = symbol.split(".", 1)[0]
        service_id = f"svc:{service}"
        if service_id not in services:
            services.append(service_id)

    for service_id in services:
        step("impact_analysis", {"service_id": service_id, "hops": hops})
        step("nearest_owner", {"service_id": service_id})
        step("find_similar_incidents", {"service_id": service_id})
        step("get_deploy_state", {"service_id": service_id})

    # Probe a bounded set of symbols — enough for multi-hop explainability
    # without N×3 remote Falkor queries on large diffs.
    probe_symbols = _priority_symbols(active, symbols)[:MAX_SYMBOL_PROBES]
    for symbol in probe_symbols:
        function_id = f"fn:{symbol}"
        step("centrality_rank", {"function_id": function_id})
        step("get_test_coverage", {"function_id": function_id})

    decision = step("decide_change", {"change_id": selected, "mode": mode})
    action = step(
        "propose_action",
        {"change_id": selected, "mode": mode, "execute": execute_action},
    )

    decision_result = decision.get("result") or {}
    action_result = (action.get("result") or {}).get("action") or {}
    explanation = _explain(trace, decision_result, action_result)
    workflow = getattr(active, "workflow_meta", None) or {}
    change_row = next((item for item in active.changes if item["id"] == selected), {})
    return {
        "ok": bool(decision.get("ok")),
        "change_id": selected,
        "change_title": change_row.get("title") or change_meta.get("title") or selected,
        "mode": mode,
        "state": decision_result.get("state"),
        "rationale": decision_result.get("rationale"),
        "triggered": decision_result.get("triggered") or explanation.get("triggered") or [],
        "claims": decision_result.get("claims") or [],
        "missing": decision_result.get("missing") or explanation.get("missing") or [],
        "evidence_ids": decision_result.get("evidence_ids") or explanation.get("evidence_ids") or [],
        "action": action_result,
        "graph_backend": decision.get("backend"),
        "tool_selection": [item["tool"] for item in trace],
        "tool_trace": trace,
        "explanation": explanation,
        "workflow": workflow,
        "symbols": symbols,
        "services": services,
    }


def _priority_symbols(engine: Engine, symbols: list[str]) -> list[str]:
    """Prefer hub functions (more CALLERS edges) so capped probes stay informative."""

    def score(symbol: str) -> tuple[int, str]:
        node_id = f"fn:{symbol}"
        callers = 0
        try:
            callers = len(engine.graph.edges_to(node_id, "CALLS"))
        except Exception:
            callers = 0
        return (-callers, symbol)

    return sorted(symbols, key=score)


def _explain(trace: list[dict], decision: dict, action: dict) -> dict:
    paths = []
    for item in trace:
        if item["tool"] != "impact_analysis":
            continue
        response = item.get("response") or {}
        result = response.get("result") or {}
        consumers = result.get("consumers") or []
        if not consumers:
            paths.append(
                {
                    "from": result.get("service_id"),
                    "path": [],
                    "note": "No consumers within hop limit.",
                }
            )
            continue
        paths.append(
            {
                "from": result.get("service_id"),
                "path": [
                    f"{row.get('name')}@{row.get('hops')}h" for row in consumers[:8]
                ],
                "truncated": result.get("truncated"),
            }
        )
    return {
        "summary": decision.get("rationale") or "No decision produced.",
        "triggered": decision.get("triggered") or [],
        "evidence_ids": decision.get("evidence_ids") or [],
        "missing": decision.get("missing") or [],
        "blast_radius_paths": paths,
        "action_status": action.get("status"),
        "action_reason": action.get("reason"),
        "how": (
            "The agent selected named graph tools (no free Cypher), assembled "
            "evidence via multi-hop reads, then applied deterministic policy and "
            "the action gate."
        ),
    }


def format_agent_report(result: dict) -> str:
    """PR-style decision card: impact → why → next steps (hackathon demo voice)."""
    state = str(result.get("state") or "UNKNOWN")
    workflow = result.get("workflow") or {}
    explanation = result.get("explanation") or {}
    action = result.get("action") or {}
    title = result.get("change_title") or result.get("change_id") or "change"

    lines = [
        f"{title}",
        f"state={state}  mode={result.get('mode')}  backend={result.get('graph_backend')}"
        + (f"  planner={result.get('planner')}" if result.get("planner") else ""),
    ]
    if workflow.get("persona"):
        lines.append(f"persona={workflow['persona']}")
    if workflow.get("story"):
        lines.append(f"story: {workflow['story']}")
    lines.append("")

    lines.append("Impact discovered")
    symbols = result.get("symbols") or []
    if symbols:
        for symbol in symbols:
            lines.append(f"  - {symbol} - directly modified")
    paths = explanation.get("blast_radius_paths") or []
    had_consumer = False
    for path in paths:
        trail = path.get("path") or []
        if not trail:
            continue
        had_consumer = True
        origin = path.get("from") or "service"
        lines.append(f"  - from {origin}:")
        for hop in trail:
            lines.append(f"      -> {hop}")
        if path.get("truncated"):
            lines.append("      (hop limit truncated)")
    if not had_consumer and not symbols:
        lines.append("  (no symbols / consumers recorded)")
    elif not had_consumer:
        lines.append("  (no downstream consumers within hop limit)")

    lines.append("")
    lines.append(f"Why this is {state}")
    lines.append(f"  {result.get('rationale') or explanation.get('summary') or '(none)'}")
    triggered = result.get("triggered") or explanation.get("triggered") or []
    if triggered:
        lines.append(f"  triggered: {', '.join(triggered)}")
    for claim in result.get("claims") or []:
        if isinstance(claim, dict):
            lines.append(f"  [{claim.get('evidence_id')}] {claim.get('text')}")
    missing = result.get("missing") or explanation.get("missing") or []
    if missing:
        lines.append("  Missing / stale:")
        for item in missing:
            if isinstance(item, dict):
                lines.append(f"    - {item.get('detail') or item}")
            else:
                lines.append(f"    - {item}")

    lines.append("")
    lines.append("Next steps")
    lines.extend(_next_steps_for_state(state, action))

    lines.append("")
    lines.append("Tool selection (auditable)")
    for name in result.get("tool_selection") or []:
        lines.append(f"  - {name}")

    lines.append("")
    lines.append(
        f"Action gate: {action.get('status') or 'n/a'} - {action.get('reason') or ''}"
    )
    if result.get("narration"):
        lines.append(f"Narration: {result.get('narration')}")
    if result.get("narration_conflict"):
        lines.append("Authority: the policy state above stands. Narration cannot change it.")
    lines.append("")
    lines.append(explanation.get("how") or "")
    if result.get("graph_backend") == "falkordb":
        lines.append("Graph witness: FalkorDB backed every named tool read in this episode.")
    if result.get("error"):
        lines.append(f"Error: {result['error']}")
    return "\n".join(lines)


def _next_steps_for_state(state: str, action: dict) -> list[str]:
    if state == "HOLD":
        return [
            "  1. Do not merge or release.",
            "  2. Resolve the open incident / contract break cited in the rationale.",
            "  3. Re-run the agent on the updated commit when fresh evidence exists.",
        ]
    if state == "NEEDS_REVIEW":
        return [
            "  1. Request review from the owners / teams named in the claims.",
            "  2. Verify the regression or payment path tests before merge.",
            "  3. Re-run after approval - the gate stays blocked until policy clears.",
        ]
    if state == "INSUFFICIENT_EVIDENCE":
        return [
            "  1. Do not invent a proceed. Supply missing CI / deploy / mapping evidence.",
            "  2. Distinguish unavailable results from failing results.",
            "  3. Re-evaluate when the evidence catalog is complete.",
        ]
    if state == "PROCEED_WITH_GUARDS":
        return [
            "  1. Run the missing / not-run verification (consumer or unit tests).",
            "  2. Inspect affected consumer behavior listed under Impact.",
            "  3. Re-run once those results are fresh and passing.",
        ]
    if state == "PROCEED":
        steps = ["  1. Safe to proceed under current policy + graph evidence."]
        if action.get("status") == "ready":
            steps.append("  2. Optional: create a gated tracking issue with --act.")
        return steps
    return ["  1. Re-scope the change with --files and re-run."]
