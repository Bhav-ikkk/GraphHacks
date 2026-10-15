from pathlib import Path

from edl.ingest import build_graph, detect_from_paths, detect_local, parse_codeowners, parse_target
from edl.models import is_payment_or_auth

ROOT = Path(__file__).resolve().parents[1]
MINI = ROOT / "examples" / "mini-repo"


def test_payment_names_match_on_word_boundaries():
    assert is_payment_or_auth("payments")
    assert is_payment_or_auth("auth-service")
    assert is_payment_or_auth("billing")
    assert not is_payment_or_auth("author-service")
    assert not is_payment_or_auth("search")


def test_compose_dependencies_are_directional():
    detected = detect_from_paths(
        ["docker-compose.yml"],
        {"checkout": ["payments"], "payments": [], "search": []},
    )
    names = {service["name"] for service in detected["services"]}
    assert names == {"checkout", "payments", "search"}
    graph, changes = build_graph(detected)
    edges = [(edge["src"], edge["dst"]) for edge in graph.edges if edge["rel"] == "DEPENDS_ON"]
    assert ("svc:checkout", "svc:payments") in edges
    assert ("svc:payments", "svc:checkout") not in edges
    assert len(changes) == 3
    assert graph.by_label("Incident") == []
    assert graph.by_label("Deployment") == []
    assert all(node.get("source") != "synthetic-overlay" for node in graph.nodes.values())


def test_local_folder_reads_only_repository_facts():
    detected = detect_local(MINI)
    names = {service["name"] for service in detected["services"]}
    assert names == {"checkout", "payments", "search"}
    assert ("checkout", "payments") in detected["depends"]
    assert detected["ci_configured"] is True
    payments = next(service for service in detected["services"] if service["name"] == "payments")
    assert "Charge" in payments["functions"]
    checkout = next(service for service in detected["services"] if service["name"] == "checkout")
    assert "place_order" in checkout["functions"]
    graph, changes = build_graph({**detected, "origin": {"kind": "path", "value": str(MINI)}})
    assert graph.by_label("Incident") == []
    assert graph.node("team:acme-payments")["name"] == "acme/payments"
    owners = [edge["src"] for edge in graph.edges_to("svc:payments", "OWNS")]
    assert owners == ["team:acme-payments"]
    checkout_change = next(change for change in changes if change["symbols"] == ["checkout.place_order"])
    assert checkout_change["ci"]["present"] is False
    assert checkout_change["ci"]["configured"] is True
    assert graph.node("test:checkout.place_order")["status"] == "not-run"


def test_codeowners_last_match_wins():
    rules = parse_codeowners("* @acme/platform\n/payments/ @acme/payments\n")
    assert rules[0]["owners"] == ["@acme/platform"]
    assert rules[1]["pattern"] == "/payments/"


def test_local_folder_without_markers_reports_nothing(tmp_path: Path):
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    detected = detect_local(tmp_path)
    assert detected["services"] == []


def test_parse_target_accepts_a_url_or_a_folder(tmp_path: Path):
    kind, value = parse_target(str(tmp_path))
    assert kind == "path"
    kind, value = parse_target("https://github.com/example-org/some-service")
    assert kind == "github"
    assert value == "example-org/some-service"
