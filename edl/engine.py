"""Process-wide engine: a repository you choose, reviews, and actions."""

from __future__ import annotations

import json
from pathlib import Path

from edl import falkor, gate, ingest
from edl.memory import MemoryGraph
from edl.models import MODES
from edl.policy import PolicyError, load_policy
from edl.review import public_record, review_change


class Engine:
    def __init__(self, policy_path: Path, log_path: Path | None = None) -> None:
        self.policy_path = policy_path
        self.log_path = log_path
        self.policy = load_policy(policy_path)
        self.graph = MemoryGraph()
        self.changes: list[dict] = []
        self.dataset = "empty"
        self.origin: dict | None = None
        self.note = "Paste a GitHub URL or a folder path. Nothing is loaded until you do."
        self.records: dict[str, dict] = {}
        self.issued: dict[str, dict] = {}
        self.issue_seq = 0

    def use_repository(self, target: str) -> dict:
        graph, changes, summary = ingest.ingest(target)
        self.graph = graph
        self.changes = changes
        self.dataset = "repository"
        self.origin = summary["origin"]
        self.note = summary["note"]
        self.issued = {}
        nodes, edges = self.graph.counts()
        falkor.publish(self.graph)
        return {
            "dataset": self.dataset,
            "origin": self.origin,
            "note": self.note,
            "nodes": nodes,
            "edges": edges,
            "changes": len(self.changes),
            "services": self.services(),
        }

    def services(self) -> list[dict]:
        incidents = {}
        for edge in self.graph.edges:
            if edge["rel"] != "AFFECTED":
                continue
            incident = self.graph.node(edge["src"])
            service = self.graph.node(edge["dst"])
            if incident and service and incident.get("status") == "open":
                incidents[service["name"]] = incident["name"]
        rows = []
        for node in self.graph.by_label("Service"):
            owner = None
            for edge in self.graph.edges_to(node["id"], "OWNS"):
                team = self.graph.node(edge["src"])
                if team:
                    owner = team.get("name")
            rows.append(
                {
                    "name": node["name"],
                    "tier": node.get("tier"),
                    "kind": node.get("kind"),
                    "source": node.get("source"),
                    "owner": owner,
                    "open_incident": incidents.get(node["name"]),
                }
            )
        return sorted(rows, key=lambda item: (item["tier"] if item["tier"] is not None else 9, item["name"]))

    def change(self, change_id: str) -> dict:
        for item in self.changes:
            if item["id"] == change_id:
                return item
        raise KeyError(change_id)

    def review(self, change_id: str, mode: str = "full") -> dict:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        try:
            policy = self.policy
        except PolicyError as exc:
            return _fail_closed(change_id, mode, str(exc))
        change = self.change(change_id)
        runs = {}
        for name in (mode, *[item for item in MODES if item != mode]):
            record = review_change(self.graph, change, policy, name, writeback=False)
            runs[name] = self._remember(record, change)
        return {"active": mode, "runs": runs}

    def act(self, change_id: str, mode: str = "full") -> dict:
        change = self.change(change_id)
        record = review_change(self.graph, change, self.policy, mode, writeback=False)
        outcome = gate.execute(record, change, self.issued)
        if outcome.get("status") == "created" and "id" not in outcome:
            self.issue_seq += 1
            outcome["id"] = f"LOCAL-{self.issue_seq:03d}"
            self.issued[change_id] = outcome
        self._log({"kind": "action", "change_id": change_id, "mode": mode, "outcome": outcome})
        return {"decision": public_record(record), "action": outcome}

    def _remember(self, record: dict, change: dict) -> dict:
        public = public_record(record)
        public["action"] = gate.assess(record, change)
        self.records[public["id"]] = public
        self.graph.add_node(
            {
                "id": public["id"],
                "label": "DecisionRecord",
                "state": public["state"],
                "change_id": public["change_id"],
                "mode": public["mode"],
                "policy_version": public["policy_version"],
                "source": "edl",
                "evidence_type": "observed",
            }
        )
        change_node = f"change:{public['change_id']}"
        if self.graph.node(change_node) is None:
            self.graph.add_node(
                {
                    "id": change_node,
                    "label": "Change",
                    "name": public["change_id"],
                    "source": "edl",
                    "evidence_type": "observed",
                }
            )
        self.graph.add_edge(public["id"], "DECIDES", change_node)
        self._log({"kind": "review", "record": _trace_row(public)})
        return public

    def _log(self, payload: dict) -> None:
        if self.log_path is None:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")

    def snapshot(self) -> dict:
        nodes, edges = self.graph.counts()
        return {
            "backend": falkor.backend_label(),
            "falkor": falkor.status(),
            "dataset": self.dataset,
            "origin": self.origin,
            "note": self.note,
            "nodes": nodes,
            "edges": edges,
            "policy_version": self.policy.get("version"),
            "changes": [
                {
                    "id": change["id"],
                    "title": change.get("title"),
                    "expected": change.get("expected"),
                    "gn": change.get("gn", False),
                    "note": change.get("note"),
                    "symbols": change.get("symbols", []),
                }
                for change in self.changes
            ],
            "services": self.services(),
            "modes": list(MODES),
        }


def _fail_closed(change_id: str, mode: str, reason: str) -> dict:
    record = {
        "id": f"dec:{change_id}:{mode}:closed",
        "change_id": change_id,
        "mode": mode,
        "state": "HOLD",
        "rationale": f"HOLD. Policy file is unusable ({reason}).",
        "claims": [{"text": reason, "evidence_id": "ev-policy", "policy": "POLICY_FILE"}],
        "findings": [],
        "triggered": ["POLICY_FILE"],
        "missing": [{"policy": "POLICY_FILE", "detail": reason, "evidence_id": "ev-policy"}],
        "tool_calls": [],
        "graph": {"nodes": [], "edges": []},
        "injection": [],
        "action": {"status": "blocked", "reason": "Policy file failed closed.", "retries": 0},
    }
    return {"active": mode, "runs": {mode: record}}


def _trace_row(record: dict) -> dict:
    return {
        "id": record["id"],
        "change_id": record["change_id"],
        "mode": record["mode"],
        "state": record["state"],
        "tools": [call["tool"] for call in record.get("tool_calls", [])],
    }
