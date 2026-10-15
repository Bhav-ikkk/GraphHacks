"""In-process stand-in for FalkorDB ro_query used only in tests.

Executes the fixed Cypher shapes that FalkorReader emits against a MemoryGraph,
so Cypher tool semantics can be compared to the memory tools without a network.
"""

from __future__ import annotations

import re

from edl.memory import MemoryGraph


class MirrorSelected:
    def __init__(self, graph: MemoryGraph) -> None:
        self.graph = graph

    def ro_query(self, cypher: str):
        text = " ".join(cypher.split())
        rows = self._dispatch(text)
        return _Result(rows)

    def _dispatch(self, cypher: str) -> list:
        if m := re.search(
            r"MATCH \(c\)-\[:DEPENDS_ON\]->\(t \{id: '([^']+)'\}\) RETURN DISTINCT c\.id",
            cypher,
        ):
            target = m.group(1)
            return [[edge["src"]] for edge in self.graph.edges_to(target, "DEPENDS_ON")]

        if m := re.search(
            r"MATCH \(n \{id: '([^']+)'\}\) RETURN n\.id, n\.name, n\.tier, n\.kind",
            cypher,
        ):
            node = self.graph.node(m.group(1))
            return [] if node is None else [_node_row(node)]

        if m := re.search(r"MATCH \(n:([A-Za-z][A-Za-z0-9_]*)\) RETURN n\.id, n\.name", cypher):
            return [_node_row(node) for node in self.graph.by_label(m.group(1))]

        if m := re.search(
            r"MATCH \(f \{id: '([^']+)'\}\)-\[:PART_OF\]->\(s\) RETURN s\.id, s\.name, s\.tier, s\.kind, labels\(s\)\[0\]",
            cypher,
        ):
            for edge in self.graph.edges_from(m.group(1), "PART_OF"):
                node = self.graph.node(edge["dst"])
                if node:
                    return [[node["id"], node.get("name"), node.get("tier"), node.get("kind"), node.get("label")]]
            return []

        if m := re.search(
            r"MATCH \(c\)-\[:CALLS\]->\(f \{id: '([^']+)'\}\) RETURN c\.id",
            cypher,
        ):
            return [[edge["src"]] for edge in self.graph.edges_to(m.group(1), "CALLS")]

        if m := re.search(
            r"MATCH \(owner\)-\[e:OWNS\]->\(s \{id: '([^']+)'\}\) RETURN owner\.id, owner\.name, e\.observed_at, e\.valid_until",
            cypher,
        ):
            edges = self.graph.edges_to(m.group(1), "OWNS")
            if not edges:
                return []
            edge = edges[0]
            owner = self.graph.node(edge["src"])
            return [
                [
                    None if owner is None else owner["id"],
                    None if owner is None else owner.get("name"),
                    edge.get("observed_at"),
                    edge.get("valid_until"),
                ]
            ]

        if m := re.search(
            r"MATCH \(t\)-\[:COVERS\]->\(f \{id: '([^']+)'\}\) RETURN t\.id, t\.name, t\.status, labels\(t\)\[0\]",
            cypher,
        ):
            rows = []
            for edge in self.graph.edges_to(m.group(1), "COVERS"):
                node = self.graph.node(edge["src"])
                if node:
                    rows.append([node["id"], node.get("name"), node.get("status"), node.get("label")])
            return rows

        if m := re.search(
            r"MATCH \(d\)-\[:DEPLOYED_AS\]->\(s \{id: '([^']+)'\}\) RETURN d\.id, d\.name, d\.age_hours, d\.health",
            cypher,
        ):
            for edge in self.graph.edges_to(m.group(1), "DEPLOYED_AS"):
                node = self.graph.node(edge["src"])
                if node:
                    return [
                        [
                            node["id"],
                            node.get("name"),
                            node.get("age_hours"),
                            node.get("health"),
                            node.get("service"),
                            node.get("label"),
                        ]
                    ]
            return []

        if m := re.search(
            r"MATCH \(i\)-\[:AFFECTED\]->\(s \{id: '([^']+)'\}\) WHERE i\.status = 'open'",
            cypher,
        ):
            rows = []
            for edge in self.graph.edges_to(m.group(1), "AFFECTED"):
                node = self.graph.node(edge["src"])
                if node and node.get("status") == "open":
                    rows.append(
                        [
                            node["id"],
                            node.get("name"),
                            node.get("status"),
                            node.get("age_days"),
                            node.get("title"),
                            node.get("label"),
                        ]
                    )
            return rows

        if "RESOLVED_BY" in cypher and "MODIFIES" in cypher:
            m = re.search(r"f \{id: '([^']+)'\}", cypher)
            if not m:
                return []
            rows = []
            for edge in self.graph.edges_to(m.group(1), "MODIFIES"):
                fix = self.graph.node(edge["src"])
                if not fix or fix.get("label") != "Fix":
                    continue
                for resolved in self.graph.edges_to(fix["id"], "RESOLVED_BY"):
                    incident = self.graph.node(resolved["src"])
                    if incident and incident.get("status") == "resolved":
                        rows.append(
                            [
                                incident["id"],
                                incident.get("name"),
                                incident.get("status"),
                                fix["id"],
                                fix.get("name"),
                            ]
                        )
            return rows

        if m := re.search(
            r"MATCH \(a \{id: '([^']+)'\}\)-\[e:([A-Za-z][A-Za-z0-9_]*)\]->\(b\) RETURN a\.id, type\(e\), b\.id",
            cypher,
        ):
            return [
                [edge["src"], edge["rel"], edge["dst"], edge.get("observed_at"), edge.get("valid_until")]
                for edge in self.graph.edges_from(m.group(1), m.group(2))
            ]

        if m := re.search(
            r"MATCH \(a\)-\[e:([A-Za-z][A-Za-z0-9_]*)\]->\(b \{id: '([^']+)'\}\) RETURN a\.id, type\(e\), b\.id",
            cypher,
        ):
            return [
                [edge["src"], edge["rel"], edge["dst"], edge.get("observed_at"), edge.get("valid_until")]
                for edge in self.graph.edges_to(m.group(2), m.group(1))
            ]

        raise AssertionError(f"MirrorSelected has no handler for Cypher: {cypher}")


def _node_row(node: dict) -> list:
    return [
        node.get("id"),
        node.get("name"),
        node.get("tier"),
        node.get("kind"),
        node.get("status"),
        node.get("age_days"),
        node.get("age_hours"),
        node.get("health"),
        node.get("title"),
        node.get("service"),
        node.get("fname"),
        node.get("granularity"),
        node.get("label"),
    ]


class _Result:
    def __init__(self, rows: list) -> None:
        self.result_set = rows
