"""Graphify-style inspection of the loaded repository graph and review evidence.

These reads are deterministic: status, services, neighbors, shortest path,
node explain, and evidence dump from a scoped review. No free-text Cypher.
"""

from __future__ import annotations

from collections import deque

from edl.memory import MemoryGraph


def resolve_node_id(graph: MemoryGraph, ref: str) -> str | None:
    """Accept svc:name, fn:symbol, bare service/function names, or exact ids."""
    text = (ref or "").strip()
    if not text:
        return None
    if graph.node(text):
        return text
    for prefix in ("svc:", "fn:", "team:", "change:", "dec:"):
        if not text.startswith(prefix) and graph.node(f"{prefix}{text}"):
            return f"{prefix}{text}"
    lowered = text.lower()
    for node in graph.nodes.values():
        name = str(node.get("name") or "")
        if name == text or name.lower() == lowered:
            return node["id"]
    return None


def neighbors(graph: MemoryGraph, node_id: str, *, limit: int = 40) -> dict:
    """Outgoing and incoming edges for one node."""
    node = graph.node(node_id)
    if node is None:
        return {"id": node_id, "missing": True, "out": [], "inn": []}
    out = []
    for edge in graph.edges_from(node_id):
        dst = graph.node(edge["dst"])
        out.append(
            {
                "rel": edge["rel"],
                "id": edge["dst"],
                "name": None if dst is None else dst.get("name"),
                "label": None if dst is None else dst.get("label"),
            }
        )
        if len(out) >= limit:
            break
    inn = []
    for edge in graph.edges_to(node_id):
        src = graph.node(edge["src"])
        inn.append(
            {
                "rel": edge["rel"],
                "id": edge["src"],
                "name": None if src is None else src.get("name"),
                "label": None if src is None else src.get("label"),
            }
        )
        if len(inn) >= limit:
            break
    return {
        "id": node_id,
        "missing": False,
        "name": node.get("name"),
        "label": node.get("label"),
        "out": out,
        "inn": inn,
    }


def explain(graph: MemoryGraph, node_id: str) -> dict:
    """Plain-language card for one node plus a short edge summary."""
    node = graph.node(node_id)
    if node is None:
        return {"id": node_id, "missing": True, "summary": f"No node named {node_id}."}
    hops = neighbors(graph, node_id)
    props = {
        key: value
        for key, value in node.items()
        if key not in {"id"} and value not in (None, "", [], {})
    }
    lines = [
        f"{node.get('label') or 'Node'} {node.get('name') or node_id}",
        f"id={node_id}",
    ]
    if node.get("sample_paths"):
        lines.append("sample paths: " + ", ".join(node["sample_paths"][:5]))
    if hops["out"]:
        lines.append(
            "out: "
            + ", ".join(f"-[{item['rel']}]-> {item['name'] or item['id']}" for item in hops["out"][:8])
        )
    if hops["inn"]:
        lines.append(
            "in: "
            + ", ".join(f"{item['name'] or item['id']} -[{item['rel']}]->" for item in hops["inn"][:8])
        )
    return {
        "id": node_id,
        "missing": False,
        "name": node.get("name"),
        "label": node.get("label"),
        "props": props,
        "neighbors": hops,
        "summary": "\n".join(lines),
    }


def shortest_path(graph: MemoryGraph, start_id: str, end_id: str) -> dict:
    """Undirected BFS over DEPENDS_ON / PART_OF / CALLS / TOUCHES / OWNS / COVERS."""
    if start_id == end_id:
        return {"ok": True, "nodes": [start_id], "hops": 0}
    allowed = {"DEPENDS_ON", "PART_OF", "CALLS", "TOUCHES", "OWNS", "COVERS", "AFFECTED", "IN_REPO"}
    prev: dict[str, tuple[str, str] | None] = {start_id: None}
    queue: deque[str] = deque([start_id])
    while queue:
        current = queue.popleft()
        for edge in graph.edges_from(current):
            if edge["rel"] not in allowed:
                continue
            nxt = edge["dst"]
            if nxt in prev:
                continue
            prev[nxt] = (current, edge["rel"])
            if nxt == end_id:
                return _rebuild_path(prev, start_id, end_id)
            queue.append(nxt)
        for edge in graph.edges_to(current):
            if edge["rel"] not in allowed:
                continue
            nxt = edge["src"]
            if nxt in prev:
                continue
            prev[nxt] = (current, edge["rel"])
            if nxt == end_id:
                return _rebuild_path(prev, start_id, end_id)
            queue.append(nxt)
    return {"ok": False, "nodes": [], "hops": None, "detail": f"No path between {start_id} and {end_id}."}


def _rebuild_path(prev: dict, start_id: str, end_id: str) -> dict:
    nodes = [end_id]
    edges: list[str] = []
    cursor = end_id
    while cursor != start_id:
        step = prev.get(cursor)
        if step is None:
            break
        parent, rel = step
        edges.append(rel)
        nodes.append(parent)
        cursor = parent
    nodes.reverse()
    edges.reverse()
    return {"ok": True, "nodes": nodes, "edges": edges, "hops": max(0, len(nodes) - 1)}


def service_cards(graph: MemoryGraph) -> list[dict]:
    rows = []
    for node in sorted(graph.by_label("Service"), key=lambda item: item.get("name") or ""):
        if node.get("name") == "__repo__":
            continue
        owner = None
        for edge in graph.edges_to(node["id"], "OWNS"):
            team = graph.node(edge["src"])
            if team:
                owner = team.get("name")
        deps = [
            (graph.node(edge["dst"]) or {}).get("name") or edge["dst"]
            for edge in graph.edges_from(node["id"], "DEPENDS_ON")
        ]
        consumers = [
            (graph.node(edge["src"]) or {}).get("name") or edge["src"]
            for edge in graph.edges_to(node["id"], "DEPENDS_ON")
        ]
        rows.append(
            {
                "id": node["id"],
                "name": node.get("name"),
                "owner": owner,
                "depends_on": deps,
                "consumers": consumers,
                "sample_paths": list(node.get("sample_paths") or [])[:5],
            }
        )
    return rows


def format_evidence(record: dict) -> str:
    """CLI dump of evidence rows that backed a decision."""
    lines = [
        f"{record.get('change_id')}  {record.get('mode')}  {record.get('state')}",
        record.get("rationale") or "",
        f"backend={record.get('graph_backend')}  revision={record.get('graph_revision')}  "
        f"evidence={len(record.get('evidence') or [])}",
        "",
        "Evidence:",
    ]
    for item in record.get("evidence") or []:
        fresh = item.get("freshness_ok")
        fresh_bit = "" if fresh is None else f"  fresh={fresh}"
        lines.append(
            f"  [{item.get('id')}] {item.get('evidence_type')}/{item.get('role')}  "
            f"{item.get('source_system')}{fresh_bit}"
        )
        if item.get("detail"):
            lines.append(f"      {item['detail']}")
        if item.get("limitations"):
            lines.append(f"      limitation: {item['limitations']}")
        if item.get("entity_id"):
            lines.append(f"      entity={item['entity_id']}")
    lines.append("")
    lines.append("Claims:")
    for claim in record.get("claims") or []:
        lines.append(f"  [{claim.get('evidence_id')}] {claim.get('text')}")
    missing = record.get("missing") or []
    if missing:
        lines.append("")
        lines.append("Missing / stale:")
        for item in missing:
            lines.append(f"  [{item.get('evidence_id') or item.get('policy')}] {item.get('detail')}")
    return "\n".join(lines)


def format_inspect(
    *,
    origin: dict | None,
    note: str,
    scope_note: str,
    scope_error: str | None,
    suggested_files: list[str],
    services: list[dict],
    changes: list[dict],
    backend: str,
    nodes: int,
    edges: int,
) -> str:
    lines = [
        f"backend={backend}  nodes={nodes}  edges={edges}",
        f"origin={origin}",
        note,
        scope_note,
    ]
    if scope_error:
        lines.append(f"scope_error: {scope_error}")
    if suggested_files:
        lines.append("suggested_files: " + ", ".join(suggested_files[:10]))
    lines.append("")
    lines.append(f"Services ({len(services)}):")
    for service in services:
        owner = service.get("owner") or "no CODEOWNERS entry"
        paths = service.get("sample_paths") or []
        path_bit = f"  files={', '.join(paths[:3])}" if paths else ""
        deps = service.get("depends_on") or []
        dep_bit = f"  depends_on={', '.join(deps)}" if deps else ""
        lines.append(f"  {service['name']}  owner={owner}{path_bit}{dep_bit}")
    lines.append("")
    if not changes:
        lines.append("No change scoped yet. Pass --files or click a suggested path in the UI.")
    for change in changes:
        lines.append(
            f"change {change.get('id')}  mapping={change.get('mapping')}  "
            f"symbols={', '.join(change.get('symbols') or []) or '(none)'}"
        )
        files = change.get("files") or (change.get("scope") or {}).get("files") or []
        if files:
            lines.append(f"  files: {', '.join(files)}")
        unresolved = change.get("unresolved_refs") or []
        if unresolved:
            lines.append(f"  unresolved: {', '.join(unresolved)}")
    return "\n".join(lines)
