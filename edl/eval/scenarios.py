"""Labeled scenarios for the evaluation harness.

These graphs are fixtures for scoring decision quality. They are not sample
data for the live UI and must not be loaded by edl.api / Engine.serve.
"""

from __future__ import annotations

from edl.memory import MemoryGraph

# Fixed observation time matches edl.models.NOW (within owner freshness window).
OBSERVED = "2026-10-15T12:00:00Z"


def all_scenarios() -> list[dict]:
    """Eval-only fixtures for scoring. Never loaded by serve / ingest / UI."""
    return [
        missing_ci_must_abstain(),
        graph_necessary_incident_hold(),
        graph_adds_nothing_missing_ci(),
        safe_proceed_complete_evidence(),
        payment_path_needs_review(),
        multi_owner_needs_graph(),
    ]


def missing_ci_must_abstain() -> dict:
    """Uncertain case: workflow exists, run result does not. Must not PROCEED."""
    graph = MemoryGraph()
    _service(graph, "search", tier=2)
    _function(graph, "search", "lookup")
    _owner(graph, "search", "platform")
    _test(graph, "search.lookup", status="not-run")
    change = _change(
        "eval:missing-ci",
        symbols=["search.lookup"],
        files=["search/lookup.py"],
        ci={"present": False, "result": None, "age_hours": None, "configured": True},
    )
    return {
        "id": "missing_ci_must_abstain",
        "title": "Missing CI must abstain",
        "family": "uncertain",
        "graph_necessary": False,
        "graph": graph,
        "change": change,
        "expected": {
            "full": "INSUFFICIENT_EVIDENCE",
            "one_hop": "INSUFFICIENT_EVIDENCE",
            "graph_off": "INSUFFICIENT_EVIDENCE",
        },
        "baseline_llm": "PROCEED",
        "notes": "Lack of a CI run is not a pass. An LLM-only baseline might invent PROCEED.",
    }


def graph_necessary_incident_hold() -> dict:
    """Risky case: only the graph shows an open tier-0 incident. Graph-off wrongly PROCEEDs."""
    graph = MemoryGraph()
    _service(graph, "payments", tier=0)
    _function(graph, "payments", "charge")
    _owner(graph, "payments", "payments-team")
    _test(graph, "payments.charge", status="pass")
    _deploy(graph, "payments", age_hours=1, health="ok")
    graph.add_node(
        {
            "id": "inc:pay-outage",
            "label": "Incident",
            "name": "pay-outage",
            "status": "open",
            "age_days": 2,
            "title": "payments elevated errors",
            "service": "payments",
            "source": "eval-fixture",
            "evidence_type": "observed",
        }
    )
    graph.add_edge("inc:pay-outage", "AFFECTED", "svc:payments", evidence_type="observed")
    change = _change(
        "eval:incident-hold",
        symbols=["payments.charge"],
        files=["payments/charge.py"],
        ci={"present": True, "result": "pass", "age_hours": 1, "configured": True},
        designated_approval=True,
        approver="payments-lead",
    )
    return {
        "id": "graph_necessary_incident_hold",
        "title": "Open incident only visible with graph",
        "family": "risky",
        "graph_necessary": True,
        "graph": graph,
        "change": change,
        "expected": {
            "full": "HOLD",
            "one_hop": "PROCEED",  # history join skipped in one_hop
            "graph_off": "PROCEED",
        },
        "baseline_llm": "PROCEED",
        "notes": "Graph-off and LLM-only miss the open incident and would unjustifiably allow.",
    }


def graph_adds_nothing_missing_ci() -> dict:
    """Graph present but missing CI still dominates. Full and graph-off agree."""
    graph = MemoryGraph()
    _service(graph, "checkout", tier=2)
    _service(graph, "payments", tier=1)
    _function(graph, "checkout", "place_order")
    _owner(graph, "checkout", "checkout-team")
    graph.add_edge("svc:checkout", "DEPENDS_ON", "svc:payments", evidence_type="observed")
    change = _change(
        "eval:graph-noop-ci",
        symbols=["checkout.place_order"],
        files=["checkout/checkout.py"],
        ci={"present": False, "result": None, "age_hours": None, "configured": True},
    )
    return {
        "id": "graph_adds_nothing_missing_ci",
        "title": "Graph present; missing CI still abstains",
        "family": "uncertain",
        "graph_necessary": False,
        "graph": graph,
        "change": change,
        "expected": {
            "full": "INSUFFICIENT_EVIDENCE",
            "one_hop": "INSUFFICIENT_EVIDENCE",
            "graph_off": "INSUFFICIENT_EVIDENCE",
        },
        "baseline_llm": "PROCEED_WITH_GUARDS",
        "notes": "Graph finds consumers but cannot upgrade missing CI into safety.",
    }


def safe_proceed_complete_evidence() -> dict:
    """Safe case: complete fresh evidence, non-sensitive service."""
    graph = MemoryGraph()
    _service(graph, "search", tier=2)
    _function(graph, "search", "lookup")
    _owner(graph, "search", "platform")
    _test(graph, "search.lookup", status="pass")
    _deploy(graph, "search", age_hours=1, health="ok")
    change = _change(
        "eval:safe-proceed",
        symbols=["search.lookup"],
        files=["search/lookup.py"],
        ci={"present": True, "result": "pass", "age_hours": 1, "configured": True},
    )
    return {
        "id": "safe_proceed_complete_evidence",
        "title": "Complete evidence allows proceed",
        "family": "safe",
        "graph_necessary": False,
        "graph": graph,
        "change": change,
        "expected": {
            "full": "PROCEED",
            "one_hop": "PROCEED",
            # graph_off does not check owners/deploys/tests; CI alone is enough here
            "graph_off": "PROCEED",
        },
        "baseline_llm": "PROCEED",
        "notes": "Control case: justified PROCEED when evidence is complete.",
    }


def payment_path_needs_review() -> dict:
    """Payment path without designated approval → NEEDS_REVIEW when evidence is otherwise complete."""
    graph = MemoryGraph()
    _service(graph, "payments", tier=2)
    _function(graph, "payments", "charge")
    _owner(graph, "payments", "payments-team")
    _test(graph, "payments.charge", status="pass")
    _deploy(graph, "payments", age_hours=1, health="ok")
    change = _change(
        "eval:payment-review",
        symbols=["payments.charge"],
        files=["payments/charge.py"],
        ci={"present": True, "result": "pass", "age_hours": 1, "configured": True},
        designated_approval=False,
    )
    return {
        "id": "payment_path_needs_review",
        "title": "Payment path without approval needs review",
        "family": "risky",
        "graph_necessary": False,
        "graph": graph,
        "change": change,
        "expected": {
            "full": "NEEDS_REVIEW",
            "one_hop": "NEEDS_REVIEW",
            # graph_off cannot see service name from blast radius the same way —
            # affected is inferred from file paths: payments/ → payments in claimed
            "graph_off": "NEEDS_REVIEW",
        },
        "baseline_llm": "PROCEED",
        "notes": "Name-based payment rule should fire; LLM-only often skips policy.",
    }


def multi_owner_needs_graph() -> dict:
    """Two direct owners on a multi-service change → R1. Graph-off cannot see OWNS edges."""
    graph = MemoryGraph()
    _service(graph, "checkout", tier=2)
    _service(graph, "payments", tier=1)
    _function(graph, "payments", "charge")
    _function(graph, "checkout", "place_order")
    _owner(graph, "payments", "payments-team")
    _owner(graph, "checkout", "checkout-team")
    _test(graph, "payments.charge", status="pass")
    _test(graph, "checkout.place_order", status="pass")
    _deploy(graph, "payments", age_hours=1, health="ok")
    _deploy(graph, "checkout", age_hours=1, health="ok")
    graph.add_edge("svc:checkout", "DEPENDS_ON", "svc:payments", evidence_type="observed")
    change = _change(
        "eval:multi-owner",
        symbols=["payments.charge", "checkout.place_order"],
        files=["payments/charge.py", "checkout/checkout.py"],
        ci={"present": True, "result": "pass", "age_hours": 1, "configured": True},
        designated_approval=True,
        approver="payments-lead",
    )
    return {
        "id": "multi_owner_needs_graph",
        "title": "Multi-owner change needs graph",
        "family": "risky",
        "graph_necessary": True,
        "graph": graph,
        "change": change,
        "expected": {
            "full": "NEEDS_REVIEW",
            "one_hop": "NEEDS_REVIEW",
            "graph_off": "PROCEED",
        },
        "baseline_llm": "PROCEED",
        "notes": "Ownership edges are graph facts; graph-off unjustifiably allows.",
    }


def _change(
    change_id: str,
    *,
    symbols: list[str],
    files: list[str],
    ci: dict,
    designated_approval: bool = False,
    approver: str | None = None,
    title: str | None = None,
) -> dict:
    return {
        "id": change_id,
        "title": title or change_id,
        "symbols": symbols,
        "files": files,
        "description": f"Eval change {change_id}",
        "ci": ci,
        "designated_approval": designated_approval,
        "approver": approver,
        "mapping": "resolved",
        "dependency": None,
        "unresolved_refs": [],
        "text_mentions_incident": False,
        "intent": None,
        "authorized": True,
        "ticketing": "ok",
        "commit": f"sha-{change_id}",
    }


def _service(graph: MemoryGraph, name: str, *, tier: int | None) -> None:
    graph.add_node(
        {
            "id": f"svc:{name}",
            "label": "Service",
            "name": name,
            "tier": tier,
            "kind": "service",
            "source": "eval-fixture",
            "evidence_type": "observed",
        }
    )


def _function(graph: MemoryGraph, service: str, fname: str, *, kind: str | None = None) -> None:
    symbol = f"{service}.{fname}"
    node = {
        "id": f"fn:{symbol}",
        "label": "Function",
        "name": symbol,
        "service": service,
        "fname": fname,
        "granularity": "function",
        "source": "eval-fixture",
        "evidence_type": "observed",
    }
    if kind:
        node["kind"] = kind
    graph.add_node(node)
    graph.add_edge(f"fn:{symbol}", "PART_OF", f"svc:{service}", evidence_type="observed")


def _owner(graph: MemoryGraph, service: str, team: str) -> None:
    team_id = f"team:{team}"
    if graph.node(team_id) is None:
        graph.add_node(
            {
                "id": team_id,
                "label": "Team",
                "name": team,
                "source": "eval-fixture",
                "evidence_type": "observed",
            }
        )
    graph.add_edge(
        team_id,
        "OWNS",
        f"svc:{service}",
        observed_at=OBSERVED,
        evidence_type="observed",
    )


def _test(graph: MemoryGraph, symbol: str, *, status: str) -> None:
    graph.add_node(
        {
            "id": f"test:{symbol}",
            "label": "Test",
            "name": f"test_{symbol}",
            "status": status,
            "source": "eval-fixture",
            "evidence_type": "observed",
        }
    )
    graph.add_edge(f"test:{symbol}", "COVERS", f"fn:{symbol}", evidence_type="observed")


def _deploy(graph: MemoryGraph, service: str, *, age_hours: int, health: str) -> None:
    graph.add_node(
        {
            "id": f"dep:{service}",
            "label": "Deployment",
            "name": f"{service}-prod",
            "service": service,
            "age_hours": age_hours,
            "health": health,
            "source": "eval-fixture",
            "evidence_type": "observed",
        }
    )
    graph.add_edge(f"dep:{service}", "DEPLOYED_AS", f"svc:{service}", evidence_type="observed")
