"""Graph read API used by the decision engine.

Production reads go through FalkorDB. Memory is only a test double when
EDL_ALLOW_MEMORY_GRAPH is set (pytest sets this). No silent fallback in product use.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any, Protocol

from edl import queries
from edl.memory import MemoryGraph

_ID = re.compile(r"^[A-Za-z0-9_.:/@+-]+$")


class GraphUnavailable(Exception):
    """FalkorDB is required and not ready."""


class GraphReader(Protocol):
    backend: str

    def node(self, node_id: str) -> dict | None: ...

    def by_label(self, label: str) -> list[dict]: ...

    def edges_from(self, src: str, rel: str | None = None) -> list[dict]: ...

    def edges_to(self, dst: str, rel: str | None = None) -> list[dict]: ...

    def consumers(self, service_id: str, hop_limit: int) -> tuple[dict[str, int], bool]: ...

    def function_service(self, function_id: str) -> dict | None: ...

    def callers(self, function_id: str) -> list[str]: ...

    def owner_of(self, service_id: str, now: datetime, owner_days: int) -> dict: ...

    def tests_covering(self, function_id: str) -> list[dict]: ...

    def deploy_of(self, service_id: str) -> dict | None: ...

    def open_incidents(self, service_id: str) -> list[dict]: ...

    def prior_fixes(self, function_id: str) -> list[dict]: ...

    def test_status(self, function_id: str) -> str: ...


def allow_memory_graph() -> bool:
    flag = os.environ.get("EDL_ALLOW_MEMORY_GRAPH", "").strip().lower()
    return flag in {"1", "true", "yes"} or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def qid(value: str) -> str:
    if not _ID.fullmatch(value):
        raise ValueError(f"Refusing unsafe graph id: {value!r}")
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


class MemoryReader:
    """Test double. Same tool semantics as FalkorReader, against MemoryGraph."""

    backend = "memory-test-double"

    def __init__(self, graph: MemoryGraph) -> None:
        self.graph = graph

    def node(self, node_id: str) -> dict | None:
        return self.graph.node(node_id)

    def by_label(self, label: str) -> list[dict]:
        return self.graph.by_label(label)

    def edges_from(self, src: str, rel: str | None = None) -> list[dict]:
        return self.graph.edges_from(src, rel)

    def edges_to(self, dst: str, rel: str | None = None) -> list[dict]:
        return self.graph.edges_to(dst, rel)

    def consumers(self, service_id: str, hop_limit: int) -> tuple[dict[str, int], bool]:
        return queries.consumers(self.graph, service_id, hop_limit)

    def function_service(self, function_id: str) -> dict | None:
        return queries.function_service(self.graph, function_id)

    def callers(self, function_id: str) -> list[str]:
        return queries.callers(self.graph, function_id)

    def owner_of(self, service_id: str, now: datetime, owner_days: int) -> dict:
        return queries.owner_of(self.graph, service_id, now, owner_days)

    def tests_covering(self, function_id: str) -> list[dict]:
        return queries.tests_covering(self.graph, function_id)

    def deploy_of(self, service_id: str) -> dict | None:
        return queries.deploy_of(self.graph, service_id)

    def open_incidents(self, service_id: str) -> list[dict]:
        return queries.open_incidents(self.graph, service_id)

    def prior_fixes(self, function_id: str) -> list[dict]:
        return queries.prior_fixes(self.graph, function_id)

    def test_status(self, function_id: str) -> str:
        return queries.test_status(self.graph, function_id)


class FalkorReader:
    """Named graph tools executed as parameterized Cypher against FalkorDB."""

    backend = "falkordb"

    def __init__(self, selected: Any) -> None:
        self.selected = selected

    def _rows(self, cypher: str) -> list:
        try:
            result = self.selected.ro_query(cypher)
            return list(getattr(result, "result_set", None) or [])
        except Exception as exc:
            from edl import falkor

            if not falkor.is_timeout(exc):
                raise
            self.selected = falkor.reconnect()
            result = self.selected.ro_query(cypher)
            return list(getattr(result, "result_set", None) or [])

    def node(self, node_id: str) -> dict | None:
        cypher = (
            f"MATCH (n {{id: {qid(node_id)}}}) "
            "RETURN n.id, n.name, n.tier, n.kind, n.status, n.age_days, n.age_hours, "
            "n.health, n.title, n.service, n.fname, n.granularity, labels(n)[0]"
        )
        rows = self._rows(cypher)
        if not rows:
            return None
        return _node_from_row(rows[0])

    def by_label(self, label: str) -> list[dict]:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", label):
            raise ValueError(f"Refusing unsafe label: {label!r}")
        cypher = (
            f"MATCH (n:{label}) "
            "RETURN n.id, n.name, n.tier, n.kind, n.status, n.age_days, n.age_hours, "
            "n.health, n.title, n.service, n.fname, n.granularity, labels(n)[0]"
        )
        return [_node_from_row(row) for row in self._rows(cypher) if _cell(row, 0)]

    def edges_from(self, src: str, rel: str | None = None) -> list[dict]:
        if rel is not None and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", rel):
            raise ValueError(f"Refusing unsafe relationship: {rel!r}")
        if rel:
            cypher = (
                f"MATCH (a {{id: {qid(src)}}})-[e:{rel}]->(b) "
                "RETURN a.id, type(e), b.id, e.observed_at, e.valid_until"
            )
        else:
            cypher = (
                f"MATCH (a {{id: {qid(src)}}})-[e]->(b) "
                "RETURN a.id, type(e), b.id, e.observed_at, e.valid_until"
            )
        return self._edge_rows(cypher)

    def edges_to(self, dst: str, rel: str | None = None) -> list[dict]:
        if rel is not None and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", rel):
            raise ValueError(f"Refusing unsafe relationship: {rel!r}")
        if rel:
            cypher = (
                f"MATCH (a)-[e:{rel}]->(b {{id: {qid(dst)}}}) "
                "RETURN a.id, type(e), b.id, e.observed_at, e.valid_until"
            )
        else:
            cypher = (
                f"MATCH (a)-[e]->(b {{id: {qid(dst)}}}) "
                "RETURN a.id, type(e), b.id, e.observed_at, e.valid_until"
            )
        return self._edge_rows(cypher)

    def _edge_rows(self, cypher: str) -> list[dict]:
        edges = []
        for row in self._rows(cypher):
            edges.append(
                {
                    "src": _cell(row, 0),
                    "rel": _cell(row, 1),
                    "dst": _cell(row, 2),
                    "observed_at": _cell(row, 3),
                    "valid_until": _cell(row, 4),
                }
            )
        return edges

    def consumers(self, service_id: str, hop_limit: int) -> tuple[dict[str, int], bool]:
        """BFS over DEPENDS_ON backwards, hop-limited. Same contract as queries.consumers."""
        if hop_limit < 0:
            raise ValueError("hop_limit must be >= 0")
        seen = {service_id}
        found: dict[str, int] = {}
        frontier = [service_id]
        depth = 0
        while frontier and depth < hop_limit:
            nxt_frontier: list[str] = []
            for current in frontier:
                for dependent in self._dependents_of(current):
                    if dependent in seen:
                        continue
                    seen.add(dependent)
                    found[dependent] = depth + 1
                    nxt_frontier.append(dependent)
            frontier = nxt_frontier
            depth += 1
        truncated = False
        for current in frontier:
            for dependent in self._dependents_of(current):
                if dependent not in seen:
                    truncated = True
                    break
            if truncated:
                break
        return found, truncated

    def _dependents_of(self, service_id: str) -> list[str]:
        cypher = (
            f"MATCH (c)-[:DEPENDS_ON]->(t {{id: {qid(service_id)}}}) "
            "RETURN DISTINCT c.id"
        )
        return [str(_cell(row, 0)) for row in self._rows(cypher) if _cell(row, 0)]

    def function_service(self, function_id: str) -> dict | None:
        cypher = (
            f"MATCH (f {{id: {qid(function_id)}}})-[:PART_OF]->(s) "
            "RETURN s.id, s.name, s.tier, s.kind, labels(s)[0]"
        )
        rows = self._rows(cypher)
        if not rows:
            return None
        row = rows[0]
        return {
            "id": _cell(row, 0),
            "name": _cell(row, 1),
            "tier": _cell(row, 2),
            "kind": _cell(row, 3),
            "label": _cell(row, 4) or "Service",
        }

    def callers(self, function_id: str) -> list[str]:
        cypher = f"MATCH (c)-[:CALLS]->(f {{id: {qid(function_id)}}}) RETURN c.id"
        return [str(_cell(row, 0)) for row in self._rows(cypher) if _cell(row, 0)]

    def owner_of(self, service_id: str, now: datetime, owner_days: int) -> dict:
        cypher = (
            f"MATCH (owner)-[e:OWNS]->(s {{id: {qid(service_id)}}}) "
            "RETURN owner.id, owner.name, e.observed_at, e.valid_until "
            "LIMIT 1"
        )
        rows = self._rows(cypher)
        if not rows:
            return {
                "service_id": service_id,
                "missing": True,
                "stale": False,
                "team": None,
                "team_id": None,
            }
        row = rows[0]
        edge = {"observed_at": _cell(row, 2), "valid_until": _cell(row, 3)}
        stale = queries._edge_stale(edge, now, owner_days)
        return {
            "service_id": service_id,
            "missing": False,
            "stale": stale,
            "team": _cell(row, 1),
            "team_id": _cell(row, 0),
            "valid_until": _cell(row, 3),
        }

    def tests_covering(self, function_id: str) -> list[dict]:
        cypher = (
            f"MATCH (t)-[:COVERS]->(f {{id: {qid(function_id)}}}) "
            "RETURN t.id, t.name, t.status, labels(t)[0]"
        )
        found = []
        for row in self._rows(cypher):
            found.append(
                {
                    "id": _cell(row, 0),
                    "name": _cell(row, 1),
                    "status": _cell(row, 2),
                    "label": _cell(row, 3) or "Test",
                }
            )
        return found

    def deploy_of(self, service_id: str) -> dict | None:
        cypher = (
            f"MATCH (d)-[:DEPLOYED_AS]->(s {{id: {qid(service_id)}}}) "
            "RETURN d.id, d.name, d.age_hours, d.health, d.service, labels(d)[0] "
            "LIMIT 1"
        )
        rows = self._rows(cypher)
        if not rows:
            return None
        row = rows[0]
        return {
            "id": _cell(row, 0),
            "name": _cell(row, 1),
            "age_hours": _cell(row, 2),
            "health": _cell(row, 3),
            "service": _cell(row, 4),
            "label": _cell(row, 5) or "Deployment",
        }

    def open_incidents(self, service_id: str) -> list[dict]:
        cypher = (
            f"MATCH (i)-[:AFFECTED]->(s {{id: {qid(service_id)}}}) "
            "WHERE i.status = 'open' "
            "RETURN i.id, i.name, i.status, i.age_days, i.title, labels(i)[0]"
        )
        found = []
        for row in self._rows(cypher):
            found.append(
                {
                    "id": _cell(row, 0),
                    "name": _cell(row, 1),
                    "status": _cell(row, 2),
                    "age_days": _cell(row, 3),
                    "title": _cell(row, 4),
                    "label": _cell(row, 5) or "Incident",
                }
            )
        return found

    def prior_fixes(self, function_id: str) -> list[dict]:
        cypher = (
            f"MATCH (inc)-[:RESOLVED_BY]->(fix:Fix)-[:MODIFIES]->(f {{id: {qid(function_id)}}}) "
            "WHERE inc.status = 'resolved' "
            "RETURN inc.id, inc.name, inc.status, fix.id, fix.name"
        )
        found = []
        for row in self._rows(cypher):
            found.append(
                {
                    "incident": {
                        "id": _cell(row, 0),
                        "name": _cell(row, 1),
                        "status": _cell(row, 2),
                        "label": "Incident",
                    },
                    "fix": {
                        "id": _cell(row, 3),
                        "name": _cell(row, 4),
                        "label": "Fix",
                    },
                }
            )
        return found

    def test_status(self, function_id: str) -> str:
        covering = self.tests_covering(function_id)
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


def _cell(row, index: int = 0):
    if isinstance(row, dict):
        values = list(row.values())
        return values[index] if index < len(values) else None
    if isinstance(row, (list, tuple)):
        return row[index] if index < len(row) else None
    return row if index == 0 else None


def _node_from_row(row) -> dict:
    label = _cell(row, 12) or "Node"
    return {
        "id": _cell(row, 0),
        "name": _cell(row, 1),
        "tier": _cell(row, 2),
        "kind": _cell(row, 3),
        "status": _cell(row, 4),
        "age_days": _cell(row, 5),
        "age_hours": _cell(row, 6),
        "health": _cell(row, 7),
        "title": _cell(row, 8),
        "service": _cell(row, 9),
        "fname": _cell(row, 10),
        "granularity": _cell(row, 11),
        "label": label,
    }
