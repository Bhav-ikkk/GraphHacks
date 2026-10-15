from edl.config import falkor_settings
from edl.falkor import _cypher_value, edge_groups, node_groups, publish
from edl.memory import MemoryGraph


def test_settings_keep_an_empty_username(monkeypatch):
    monkeypatch.setenv("FALKORDB_HOST", "13.236.193.238")
    monkeypatch.setenv("FALKORDB_PORT", "6379")
    monkeypatch.setenv("FALKORDB_USERNAME", "")
    monkeypatch.setenv("FALKORDB_PASSWORD", "secret")
    monkeypatch.setenv("FALKORDB_GRAPH", "edl")
    settings = falkor_settings()
    assert settings.host == "13.236.193.238"
    assert settings.port == 6379
    assert settings.username == ""
    assert settings.password == "secret"
    assert settings.graph == "edl"
    assert settings.browser == "http://13.236.193.238:3000"


def test_rows_keep_plain_properties_only():
    graph = MemoryGraph()
    graph.add_node(
        {
            "id": "svc:payments",
            "label": "Service",
            "name": "payments",
            "tier": 0,
            "shared": False,
            "extra": {"nope": True},
            "missing": None,
        }
    )
    graph.add_node({"id": "svc:search", "label": "Service", "name": "search", "tier": 2})
    graph.add_edge("svc:search", "DEPENDS_ON", "svc:payments", evidence_type="observed")
    graph.add_edge("svc:search", "NOT_A_REAL_REL", "svc:payments")
    services = node_groups(graph)["Service"]
    assert services[0]["id"] == "svc:payments"
    assert services[0]["tier"] == 0
    assert services[0]["shared"] is False
    assert "extra" not in services[0]
    assert "missing" not in services[0]
    assert "label" not in services[0]
    assert list(edge_groups(graph)) == ["DEPENDS_ON"]


def test_browser_rows_may_be_objects():
    from edl.falkor import _QueryResult, _first_number, _cell

    assert _cell({"n.name": "payments"}) == "payments"
    assert _first_number(_QueryResult([{"count(n)": 4}])) == 4


def test_cypher_values_are_quoted():
    assert _cypher_value("a'b") == "'a\\'b'"
    assert _cypher_value(True) == "true"
    assert _cypher_value(2) == "2"


def test_pytest_does_not_open_a_connection(monkeypatch):
    monkeypatch.setenv("FALKORDB_HOST", "127.0.0.1")
    result = publish(MemoryGraph())
    assert result["skipped"] is True
    assert result["ok"] is False
