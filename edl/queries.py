"""Parameterized graph reads. Every agent-facing tool is one of these."""

from __future__ import annotations

from datetime import datetime

from edl.memory import MemoryGraph


def _parse(value: str) -> datetime:
    text = value.replace("Z", "+00:00")
    return datetime.fromisoformat(text)


def consumers(graph: MemoryGraph, service_id: str, hop_limit: int) -> tuple[dict[str, int], bool]:
    """Services that depend on service_id, following DEPENDS_ON backwards.

    An edge (A)-[:DEPENDS_ON]->(B) means A depends on B, so A is a consumer of B.
    Cycles and self-edges terminate. `truncated` is true when a node at the hop
    limit still has a consumer that was not entered.
    """
    seen = {service_id}
    found: dict[str, int] = {}
    frontier = [service_id]
    depth = 0
    while frontier and depth < hop_limit:
        nxt_frontier: list[str] = []
        for current in frontier:
            for edge in graph.edges_to(current, "DEPENDS_ON"):
                nxt = edge["src"]
                if nxt in seen:
                    continue
                seen.add(nxt)
                found[nxt] = depth + 1
                nxt_frontier.append(nxt)
        frontier = nxt_frontier
        depth += 1
    truncated = False
    for current in frontier:
        for edge in graph.edges_to(current, "DEPENDS_ON"):
            if edge["src"] not in seen:
                truncated = True
                break
        if truncated:
            break
    return found, truncated


def function_service(graph: MemoryGraph, function_id: str) -> dict | None:
    for edge in graph.edges_from(function_id, "PART_OF"):
        return graph.node(edge["dst"])
    return None


def callers(graph: MemoryGraph, function_id: str) -> list[str]:
    return [edge["src"] for edge in graph.edges_to(function_id, "CALLS")]


def callee_functions(graph: MemoryGraph, function_id: str) -> list[str]:
    return [edge["dst"] for edge in graph.edges_from(function_id, "CALLS")]


def owner_of(graph: MemoryGraph, service_id: str, now: datetime, owner_days: int) -> dict:
    edges = graph.edges_to(service_id, "OWNS")
    if not edges:
        return {
            "service_id": service_id,
            "missing": True,
            "stale": False,
            "team": None,
            "team_id": None,
        }
    edge = edges[0]
    stale = _edge_stale(edge, now, owner_days)
    team = graph.node(edge["src"])
    return {
        "service_id": service_id,
        "missing": False,
        "stale": stale,
        "team": None if team is None else team.get("name"),
        "team_id": None if team is None else team["id"],
        "valid_until": edge.get("valid_until"),
    }


def _edge_stale(edge: dict, now: datetime, owner_days: int) -> bool:
    until = edge.get("valid_until")
    if until and _parse(until) < now:
        return True
    observed = edge.get("observed_at")
    if observed and (now - _parse(observed)).days > owner_days:
        return True
    return False


def tests_covering(graph: MemoryGraph, function_id: str) -> list[dict]:
    found = []
    for edge in graph.edges_to(function_id, "COVERS"):
        node = graph.node(edge["src"])
        if node:
            found.append(node)
    return found


def deploy_of(graph: MemoryGraph, service_id: str) -> dict | None:
    for edge in graph.edges_to(service_id, "DEPLOYED_AS"):
        node = graph.node(edge["src"])
        if node:
            return node
    return None


def open_incidents(graph: MemoryGraph, service_id: str) -> list[dict]:
    found = []
    for edge in graph.edges_to(service_id, "AFFECTED"):
        node = graph.node(edge["src"])
        if node and node.get("status") == "open":
            found.append(node)
    return found


def prior_fixes(graph: MemoryGraph, function_id: str) -> list[dict]:
    """Resolved incidents whose fix modifies this function."""
    found = []
    for edge in graph.edges_to(function_id, "MODIFIES"):
        fix = graph.node(edge["src"])
        if not fix or fix.get("label") != "Fix":
            continue
        for resolved in graph.edges_to(fix["id"], "RESOLVED_BY"):
            incident = graph.node(resolved["src"])
            if incident and incident.get("status") == "resolved":
                found.append({"incident": incident, "fix": fix})
    return found


def test_status(graph: MemoryGraph, function_id: str) -> str:
    covering = tests_covering(graph, function_id)
    if not covering:
        return "missing"
    if any(item.get("status") == "fail" for item in covering):
        return "fail"
    if any(item.get("status") == "missing" for item in covering):
        return "missing"
    if any(item.get("status") == "pass" for item in covering):
        return "pass"
    if any(item.get("status") == "not-run" for item in covering):
        return "not-run"
    return "missing"
