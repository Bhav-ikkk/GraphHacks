"""Project the working graph into FalkorDB with the official Python client.

    client = FalkorDB(host, port, username=None, password=password)
    graph = client.select_graph(name)
    graph.query(...)
    graph.ro_query(...).result_set

Decisions are still computed in Python. When the database accepts a connection,
the same nodes and edges are written to the graph named in FALKORDB_GRAPH so
they show up in FalkorDB Browser. The client never sends free-text Cypher.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from edl.config import FalkorSettings, falkor_settings
from edl.memory import MemoryGraph

BROWSER_URL = "http://13.236.193.238:3000/login"

LABELS = {
    "Repository",
    "Team",
    "Engineer",
    "Service",
    "Function",
    "Test",
    "Deployment",
    "Incident",
    "Fix",
    "Policy",
    "Change",
    "UnresolvedPath",
    "DecisionRecord",
    "Outcome",
    "EvidenceSource",
}
RELATIONS = {
    "MEMBER_OF",
    "IN_REPO",
    "OWNS",
    "PART_OF",
    "COVERS",
    "DEPLOYED_AS",
    "DEPENDS_ON",
    "AFFECTED",
    "RESOLVED_BY",
    "MODIFIES",
    "CALLS",
    "TOUCHES",
    "UNRESOLVED",
    "DECIDES",
    "SUPPORTED_BY",
    "GOVERNS",
}
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*")

_state: dict[str, Any] = {
    "ok": False,
    "skipped": False,
    "detail": "in-memory",
    "error": None,
    "nodes": 0,
    "edges": 0,
}


def backend_label() -> str:
    return str(_state.get("detail") or "in-memory")


def status() -> dict[str, Any]:
    settings = falkor_settings()
    return {
        "ok": bool(_state.get("ok")),
        "configured": settings.configured,
        "host": settings.host,
        "port": settings.port,
        "graph": settings.graph,
        "username": settings.username,
        "browser": settings.browser or BROWSER_URL,
        "detail": backend_label(),
        "error": _state.get("error"),
        "nodes": _state.get("nodes") or 0,
        "edges": _state.get("edges") or 0,
    }


def publish(graph: MemoryGraph, *, force: bool = False) -> dict[str, Any]:
    """Replace the remote graph with this one. Safe to call when unconfigured."""
    if not force and os.environ.get("PYTEST_CURRENT_TEST"):
        _state.update(ok=False, skipped=True, detail="in-memory (tests do not call FalkorDB)", error=None)
        return dict(_state)
    settings = falkor_settings()
    if not settings.configured:
        _state.update(ok=False, skipped=False, detail="in-memory (set FALKORDB_HOST in .env)", error=None)
        return dict(_state)
    if not settings.password:
        _state.update(
            ok=False,
            skipped=False,
            detail="in-memory (put the database password in FALKORDB_PASSWORD and leave the username empty)",
            error="password missing",
            nodes=0,
            edges=0,
        )
        return dict(_state)
    errors: list[str] = []
    if settings.browser:
        try:
            return _published(settings, _connect_browser(settings), graph, via="browser")
        except Exception as exc:
            errors.append(_explain(exc, settings))
    try:
        return _published(settings, _connect(settings), graph, via="redis")
    except Exception as exc:
        errors.append(_explain(exc, settings))
    detail = "in-memory; FalkorDB not reachable (" + "; ".join(errors) + ")"
    _state.update(ok=False, skipped=False, detail=detail, error=detail, nodes=0, edges=0)
    return dict(_state)


def _published(settings: FalkorSettings, remote, graph: MemoryGraph, *, via: str) -> dict[str, Any]:
    written = replace_graph(remote, settings.graph, graph)
    names = written.get("services") or []
    shown = ", ".join(names[:6]) if names else "no Service nodes were read back"
    where = settings.browser if via == "browser" else f"{settings.host}:{settings.port}"
    # detail = (
    #     f"FalkorDB {where} / {settings.graph} "
    #     f"({written['nodes']} nodes, {written['edges']} edges). Read back: {shown}."
    # )
    _state.update(ok=True, skipped=False, detail=detail, error=None, nodes=written["nodes"], edges=written["edges"])
    return dict(_state)


def node_groups(graph: MemoryGraph) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for node in graph.nodes.values():
        label = str(node.get("label") or "")
        if label not in LABELS or not _NAME.fullmatch(label):
            continue
        groups.setdefault(label, []).append(_props(node))
    return groups


def edge_groups(graph: MemoryGraph) -> dict[str, list[dict]]:
    known = set(graph.nodes)
    groups: dict[str, list[dict]] = {}
    for edge in graph.edges:
        rel = str(edge.get("rel") or "")
        if rel not in RELATIONS or not _NAME.fullmatch(rel):
            continue
        if edge["src"] not in known or edge["dst"] not in known:
            continue
        groups.setdefault(rel, []).append(
            {"src": edge["src"], "dst": edge["dst"], "props": _props(edge, skip={"src", "rel", "dst"})}
        )
    return groups


def replace_graph(remote, graph_name: str, graph: MemoryGraph) -> dict[str, int]:
    selected = remote.select_graph(graph_name)
    try:
        selected.delete()
    except Exception:
        pass
    if hasattr(selected, "ensure"):
        selected.ensure()
    node_count = 0
    for label, rows in node_groups(graph).items():
        for chunk in _chunks(rows, 15):
            selected.query(f"UNWIND {_literal_rows(chunk)} AS row CREATE (n:{label}) SET n = row")
            node_count += len(chunk)
    edge_count = 0
    for rel, rows in edge_groups(graph).items():
        for chunk in _chunks(rows, 15):
            selected.query(
                f"UNWIND {_literal_rows(chunk)} AS row "
                "MATCH (a {id: row.src}), (b {id: row.dst}) "
                f"CREATE (a)-[e:{rel}]->(b) "
                "SET e = row.props"
            )
            edge_count += len(chunk)
    remote_nodes, remote_edges = _counts(selected)
    services = _service_names(selected)
    return {
        "nodes": remote_nodes or node_count,
        "edges": remote_edges or edge_count,
        "services": services,
    }


def _literal_rows(rows: list[dict]) -> str:
    return "[" + ", ".join(_map_literal(row) for row in rows) + "]"


def _map_literal(row: dict) -> str:
    parts = []
    for key, value in row.items():
        if not _NAME.fullmatch(str(key)):
            continue
        parts.append(f"{key}: {_cypher_value(value)}")
    return "{" + ", ".join(parts) + "}"


def _cypher_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, dict):
        return _map_literal(value)
    if value is None:
        return "null"
    text = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return "'" + text + "'"


def _connect_browser(settings: FalkorSettings):
    token = _browser_token(settings)
    return _BrowserClient(settings.browser, token, settings)


def _browser_token(settings: FalkorSettings) -> str:
    usernames = [settings.username] if settings.username else ["default", ""]
    last: Exception | None = None
    for username in usernames:
        try:
            return _request_token(settings, username)
        except Exception as exc:
            last = exc
    raise last or RuntimeError("FalkorDB browser login failed")


def _request_token(settings: FalkorSettings, username: str) -> str:
    payload = {
        "username": username,
        "password": settings.password,
        "host": "localhost",
        "port": str(settings.port),
        "tls": "false",
        "name": "syzygy-edl",
    }
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        settings.browser + "/api/auth/tokens/credentials",
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "syzygy-edl"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = _redact(exc.read().decode("utf-8", errors="replace"), settings)
        raise RuntimeError(f"browser login returned {exc.code}: {detail}") from exc
    token = data.get("token") if isinstance(data, dict) else None
    if not token:
        raise RuntimeError("browser login did not return a token")
    return str(token)


class _BrowserClient:
    def __init__(self, base: str, token: str, settings: FalkorSettings) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.settings = settings

    def select_graph(self, name: str) -> "_BrowserGraph":
        if not _NAME.fullmatch(name):
            raise ValueError(f"Graph name {name!r} must be a plain word, such as Test.")
        return _BrowserGraph(self, name)


class _QueryResult:
    def __init__(self, rows: list) -> None:
        self.result_set = rows


class _BrowserGraph:
    def __init__(self, client: _BrowserClient, name: str) -> None:
        self.client = client
        self.name = name

    def delete(self) -> None:
        self._request("DELETE", f"/api/graph/{urllib.parse.quote(self.name)}")

    def ensure(self) -> None:
        self._request("POST", f"/api/graph/{urllib.parse.quote(self.name)}")

    def query(self, cypher: str, params: dict | None = None) -> _QueryResult:
        if params:
            raise ValueError("This path sends Cypher text only.")
        return self._query(cypher)

    def ro_query(self, cypher: str) -> _QueryResult:
        return self._query(cypher)

    def _query(self, cypher: str) -> _QueryResult:
        query = urllib.parse.urlencode({"query": cypher, "timeout": 30000})
        path = f"/api/graph/{urllib.parse.quote(self.name)}?{query}"
        text = self._request("GET", path)
        rows = _rows_from_sse(text)
        return _QueryResult(rows)

    def _request(self, method: str, path: str) -> str:
        request = urllib.request.Request(
            self.client.base + path,
            headers={
                "Authorization": f"Bearer {self.client.token}",
                "Accept": "application/json, text/event-stream",
                "User-Agent": "syzygy-edl",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = _redact(exc.read().decode("utf-8", errors="replace"), self.client.settings)
            raise RuntimeError(f"browser {method} {path.split('?', 1)[0]} returned {exc.code}: {detail}") from exc


def _rows_from_sse(text: str) -> list:
    if not text.strip():
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if payload is not None:
        return _rows_from_payload(payload)
    events = []
    for block in text.split("\n\n"):
        event = "message"
        data_lines = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
        if not data_lines:
            continue
        raw = "\n".join(data_lines)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = raw
        events.append((event, parsed))
    errors = [payload for event, payload in events if event in {"error", "errors"}]
    if errors:
        raise RuntimeError(str(errors[0])[:300])
    for _event, payload in events:
        rows = _rows_from_payload(payload)
        if rows:
            return rows
    return []


def _rows_from_payload(payload: Any) -> list:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("data", "result", "result_set", "records"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = _rows_from_payload(value)
            if nested:
                return nested
    return []


def _service_names(selected) -> list[str]:
    try:
        result = selected.ro_query("MATCH (n:Service) RETURN n.name")
    except Exception:
        return []
    names = []
    for row in getattr(result, "result_set", None) or []:
        value = _cell(row)
        if isinstance(value, dict):
            value = value.get("name") or next(iter(value.values()), "")
        if value:
            names.append(str(value))
    return sorted(names)


def _redact(text: str, settings: FalkorSettings) -> str:
    if settings.password:
        text = text.replace(settings.password, "***")
    compact = " ".join(text.split())
    return compact[:300]


def _connect(settings: FalkorSettings):
    from falkordb import FalkorDB

    return FalkorDB(
        host=settings.host,
        port=settings.port,
        username=settings.username or None,
        password=settings.password or None,
        socket_connect_timeout=5,
        socket_timeout=20,
    )


def _counts(selected) -> tuple[int, int]:
    nodes = selected.ro_query("MATCH (n) RETURN count(n)")
    edges = selected.ro_query("MATCH ()-[e]->() RETURN count(e)")
    return _first_number(nodes), _first_number(edges)


def _first_number(result) -> int:
    rows = getattr(result, "result_set", None) or []
    if not rows:
        return 0
    value = _cell(rows[0])
    if isinstance(value, dict):
        value = next(iter(value.values()), 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _cell(row):
    if isinstance(row, dict):
        return next(iter(row.values()), None)
    if isinstance(row, (list, tuple)):
        return row[0] if row else None
    return row


def _props(item: dict, skip: set[str] | None = None) -> dict:
    ignored = {"label"} if skip is None else skip
    clean = {}
    for key, value in item.items():
        if key in ignored or not _NAME.fullmatch(str(key)):
            continue
        if isinstance(value, bool):
            clean[key] = value
        elif isinstance(value, (int, float, str)):
            clean[key] = value
    return clean


def _chunks(rows: list, size: int):
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def _explain(exc: Exception, settings: FalkorSettings) -> str:
    text = str(exc)
    lowered = text.lower()
    if "timeout" in lowered or "timed out" in lowered:
        return (
            f"timed out connecting to {settings.host}:{settings.port}. "
            "Port 3000 is only the browser website. The database port, usually 6379, "
            "has to be open to this machine. Put that port in FALKORDB_PORT."
        )
    if "auth" in lowered or "password" in lowered or "noauth" in lowered:
        return "the server rejected the login. Set FALKORDB_PASSWORD and leave FALKORDB_USERNAME empty."
    first = text.splitlines()[0].strip()
    return first[:280] or exc.__class__.__name__
