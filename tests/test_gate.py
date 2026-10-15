from pathlib import Path

from edl.engine import Engine
from edl.gate import assess, execute
from edl.policy import load_policy
from edl.review import review_change

ROOT = Path(__file__).resolve().parents[1]
POLICY = load_policy(ROOT / "policies" / "policies.yaml")


def _complete_search_change():
    from edl.memory import MemoryGraph

    graph = MemoryGraph()
    graph.add_node({"id": "svc:search", "label": "Service", "name": "search", "tier": 2, "kind": "service"})
    graph.add_node({"id": "fn:search.lookup", "label": "Function", "name": "search.lookup", "service": "search"})
    graph.add_edge("fn:search.lookup", "PART_OF", "svc:search")
    graph.add_node({"id": "team:platform", "label": "Team", "name": "platform"})
    graph.add_edge(
        "team:platform",
        "OWNS",
        "svc:search",
        observed_at="2026-10-15T12:00:00Z",
        evidence_type="observed",
    )
    graph.add_node(
        {
            "id": "dep:search",
            "label": "Deployment",
            "name": "search-prod",
            "service": "search",
            "age_hours": 1,
            "health": "ok",
        }
    )
    graph.add_edge("dep:search", "DEPLOYED_AS", "svc:search")
    graph.add_node({"id": "test:search.lookup", "label": "Test", "name": "test_lookup", "status": "pass"})
    graph.add_edge("test:search.lookup", "COVERS", "fn:search.lookup")
    change = {
        "id": "fixture:search.lookup",
        "title": "search.lookup",
        "symbols": ["search.lookup"],
        "files": ["search/lookup.py"],
        "description": "lookup",
        "ci": {"present": True, "result": "pass", "age_hours": 1, "configured": True},
        "designated_approval": False,
        "mapping": "resolved",
        "authorized": True,
        "ticketing": "ok",
    }
    return graph, change


def test_unauthorized_action_is_blocked_and_decision_stays():
    graph, change = _complete_search_change()
    change["authorized"] = False
    record = review_change(graph, change, POLICY, "full")
    assert record["state"] == "PROCEED"
    outcome = assess(record, change)
    assert outcome["status"] == "blocked"
    assert "authorized" in outcome["reason"]


def test_insufficient_evidence_cannot_open_an_issue():
    engine = Engine(ROOT / "policies" / "policies.yaml")
    engine.use_repository(str(ROOT / "examples" / "mini-repo"))
    change = engine.changes[0]
    record = review_change(engine.graph, change, POLICY, "full")
    assert record["state"] == "INSUFFICIENT_EVIDENCE"
    assert assess(record, change)["status"] == "blocked"


def test_timeout_fails_once_without_a_retry_storm():
    graph, change = _complete_search_change()
    change["ticketing"] = "timeout"
    record = review_change(graph, change, POLICY, "full")
    outcome = execute(record, change, {})
    assert outcome["status"] == "failed"
    assert outcome["retries"] == 0


def test_duplicate_action_returns_the_same_issue():
    engine = Engine(ROOT / "policies" / "policies.yaml")
    graph, change = _complete_search_change()
    engine.graph = graph
    engine.changes = [change]
    first = engine.act(change["id"], "full")
    second = engine.act(change["id"], "full")
    assert first["action"]["status"] == "created"
    assert second["action"]["idempotent"] is True
    assert first["action"]["id"] == second["action"]["id"]


def test_rate_limit_retries_once():
    graph, change = _complete_search_change()
    change["ticketing"] = "429"
    record = review_change(graph, change, POLICY, "full")
    outcome = execute(record, change, {})
    assert outcome["status"] == "created"
    assert outcome["retries"] == 1
