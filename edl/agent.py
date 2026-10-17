"""Minimal engineering-change agent.

Tool selection is deliberate and graph-first: load/scope → multi-hop reads →
decide → gated action. No free-text Cypher. An LLM host can call the same tools
via MCP; this agent proves the loop without requiring a model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from edl.engine import Engine
from edl.tools import ToolContext, call_tool

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "policies" / "policies.yaml"
LOG = ROOT / "logs" / "agent.jsonl"


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

    # Probe a bounded set of symbols for the transcript. decide_change still
    # evaluates every scoped symbol through the review path.
    for symbol in symbols[:4]:
        function_id = f"fn:{symbol}"
        step("centrality_rank", {"function_id": function_id})
        step("get_test_coverage", {"function_id": function_id})
        step("find_similar_incidents", {"function_id": function_id})

    decision = step("decide_change", {"change_id": selected, "mode": mode})
    action = step(
        "propose_action",
        {"change_id": selected, "mode": mode, "execute": execute_action},
    )

    decision_result = decision.get("result") or {}
    action_result = (action.get("result") or {}).get("action") or {}
    explanation = _explain(trace, decision_result, action_result)
    return {
        "ok": bool(decision.get("ok")),
        "change_id": selected,
        "mode": mode,
        "state": decision_result.get("state"),
        "rationale": decision_result.get("rationale"),
        "action": action_result,
        "graph_backend": decision.get("backend"),
        "tool_selection": [item["tool"] for item in trace],
        "tool_trace": trace,
        "explanation": explanation,
    }


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
        "missing": decision.get("missing") or [],
        "evidence_ids": decision.get("evidence_ids") or [],
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
    lines = [
        f"Agent episode  change={result.get('change_id')}  mode={result.get('mode')}",
        f"backend={result.get('graph_backend')}  state={result.get('state')}"
        + (f"  planner={result.get('planner')}" if result.get("planner") else ""),
        "",
        "Tool selection:",
    ]
    for name in result.get("tool_selection") or []:
        lines.append(f"  - {name}")
    lines.append("")
    lines.append(f"Rationale: {result.get('rationale')}")
    explanation = result.get("explanation") or {}
    lines.append(f"Triggered: {', '.join(explanation.get('triggered') or []) or 'none'}")
    missing = explanation.get("missing") or result.get("missing") or []
    if missing:
        details = []
        for item in missing:
            if isinstance(item, dict):
                details.append(str(item.get("detail") or item.get("policy") or item))
            else:
                details.append(str(item))
        lines.append("Missing evidence: " + "; ".join(details))
    lines.append("Blast-radius paths:")
    for path in explanation.get("blast_radius_paths") or []:
        trail = " → ".join(path.get("path") or []) or "(none)"
        lines.append(f"  {path.get('from')}: {trail}")
    action = result.get("action") or {}
    lines.append(
        f"Action: {action.get('status')} - {action.get('reason')}"
    )
    if result.get("narration"):
        lines.append(f"Narration: {result.get('narration')}")
    if result.get("narration_conflict"):
        lines.append(
            "Authority: the policy state above stands. Narration cannot change it."
        )
    lines.append("")
    lines.append(explanation.get("how") or "")
    if result.get("graph_backend") == "falkordb":
        lines.append("Graph witness: FalkorDB backed every named tool read in this episode.")
    if result.get("error"):
        lines.append(f"Error: {result['error']}")
    return "\n".join(lines)
