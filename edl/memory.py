"""In-memory property graph. FalkorDB can replace this later; see edl/falkor.py."""

from __future__ import annotations

import hashlib


class MemoryGraph:
    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []

    def add_node(self, node: dict) -> None:
        self.nodes[node["id"]] = dict(node)

    def add_edge(self, src: str, rel: str, dst: str, **props) -> None:
        self.edges.append({"src": src, "rel": rel, "dst": dst, **props})

    def copy(self) -> "MemoryGraph":
        cloned = MemoryGraph()
        cloned.nodes = {key: dict(value) for key, value in self.nodes.items()}
        cloned.edges = [dict(edge) for edge in self.edges]
        return cloned

    def delete_node(self, node_id: str) -> None:
        self.nodes.pop(node_id, None)
        self.edges = [
            edge for edge in self.edges if edge["src"] != node_id and edge["dst"] != node_id
        ]

    def update_node(self, node_id: str, **props) -> None:
        self.nodes[node_id].update(props)

    def node(self, node_id: str) -> dict | None:
        return self.nodes.get(node_id)

    def by_label(self, label: str) -> list[dict]:
        return [node for node in self.nodes.values() if node.get("label") == label]

    def edges_from(self, src: str, rel: str | None = None) -> list[dict]:
        return [
            edge
            for edge in self.edges
            if edge["src"] == src and (rel is None or edge["rel"] == rel)
        ]

    def edges_to(self, dst: str, rel: str | None = None) -> list[dict]:
        return [
            edge
            for edge in self.edges
            if edge["dst"] == dst and (rel is None or edge["rel"] == rel)
        ]

    def counts(self) -> tuple[int, int]:
        return len(self.nodes), len(self.edges)

    def revision(self) -> str:
        """Deterministic fingerprint of the current nodes and edges."""
        parts = sorted(self.nodes.keys())
        edges = sorted(f"{edge['src']}|{edge['rel']}|{edge['dst']}" for edge in self.edges)
        raw = "\n".join(parts) + "\n" + "\n".join(edges)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
