"""Process-wide engine: a repository you choose, one scoped change, reviews, and actions."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from edl import change as change_scope
from edl import falkor, gate, ingest
from edl.change import ChangeError
from edl.graph_reader import GraphUnavailable, MemoryReader, allow_memory_graph
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
        self.ci_configured = False
        self.scope_note = "No change is scoped yet."
        self.records: dict[str, dict] = {}
        self.issued: dict[str, dict] = {}
        self.issue_seq = 0
        self._lock = threading.Lock()

    def use_repository(
        self,
        target: str,
        *,
        base: str | None = None,
        head: str | None = None,
        files: list[str] | None = None,
    ) -> dict:
        with self._lock:
            return self._use_repository(target, base=base, head=head, files=files)

    def _use_repository(
        self,
        target: str,
        *,
        base: str | None = None,
        head: str | None = None,
        files: list[str] | None = None,
    ) -> dict:
        graph, summary = ingest.ingest(target)
        self.graph = graph
        self.changes = []
        self.dataset = "repository"
        self.origin = summary["origin"]
        self.note = summary["note"]
        self.ci_configured = bool(summary.get("ci_configured"))
        self.issued = {}
        self.scope_note = "No change is scoped yet."
        scope_error = None
        try:
            self._scope_change(base=base, head=head, files=files)
        except ChangeError as exc:
            scope_error = str(exc)
            self.scope_note = scope_error
        nodes, edges = self.graph.counts()
        falkor.publish(self.graph)
        return {
            "dataset": self.dataset,
            "origin": self.origin,
            "note": self.note,
            "scope_note": self.scope_note,
            "scope_error": scope_error,
            "nodes": nodes,
            "edges": edges,
            "changes": len(self.changes),
            "services": self.services(),
        }

    def scope_change(
        self,
        *,
        base: str | None = None,
        head: str | None = None,
        files: list[str] | None = None,
        title: str | None = None,
    ) -> dict:
        with self._lock:
            scoped = self._scope_change(base=base, head=head, files=files, title=title)
            falkor.publish(self.graph)
            return scoped

    def _scope_change(
        self,
        *,
        base: str | None = None,
        head: str | None = None,
        files: list[str] | None = None,
        title: str | None = None,
    ) -> dict:
        if self.dataset != "repository" or self.origin is None:
            raise ChangeError("Analyze a repository before scoping a change.")

        explicit = [path.strip() for path in (files or []) if path and path.strip()]
        source = "files"
        if explicit:
            paths = explicit
            source = "files"
        elif self.origin.get("kind") == "path":
            paths, source = change_scope.list_changed_files(
                Path(self.origin["value"]),
                base=base,
                head=head,
            )
        elif base or head:
            raise ChangeError(
                "Git base/head scoping needs a local folder. "
                "For a GitHub URL, pass explicit files, or clone the repo and analyze the folder."
            )
        else:
            raise ChangeError(
                "No change scope yet. For a local git folder the working tree or HEAD~1..HEAD "
                "is used automatically when present. Otherwise pass files, or base and head."
            )

        change = change_scope.build_scoped_change(
            self.graph,
            paths,
            base=base,
            head=head,
            source=source,
            ci_configured=self.ci_configured,
            title=title,
        )
        change_scope.attach_change_node(self.graph, change)
        self.changes = [change]
        self.issued = {}
        self.scope_note = change.get("note") or "Change scoped."
        return {
            "change": _public_change(change),
            "scope_note": self.scope_note,
            "changes": len(self.changes),
            "snapshot": self.snapshot(),
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
        with self._lock:
            return self._review(change_id, mode)

    def _review(self, change_id: str, mode: str = "full") -> dict:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        try:
            policy = self.policy
        except PolicyError as exc:
            return _fail_closed(change_id, mode, str(exc))
        try:
            reader, backend = self._reader_for_review()
        except GraphUnavailable as exc:
            return _fail_no_graph(change_id, mode, str(exc))
        change = self.change(change_id)
        runs = {}
        for name in (mode, *[item for item in MODES if item != mode]):
            record = review_change(
                self.graph, change, policy, name, writeback=False, reader=reader
            )
            record["graph_backend"] = backend
            runs[name] = self._remember(record, change)
        return {"active": mode, "runs": runs}

    def act(self, change_id: str, mode: str = "full") -> dict:
        with self._lock:
            return self._act(change_id, mode)

    def _act(self, change_id: str, mode: str = "full") -> dict:
        try:
            reader, backend = self._reader_for_review()
        except GraphUnavailable as exc:
            closed = _fail_no_graph(change_id, mode, str(exc))
            return {"decision": closed["runs"][mode], "action": closed["runs"][mode]["action"]}
        change = self.change(change_id)
        record = review_change(
            self.graph, change, self.policy, mode, writeback=False, reader=reader
        )
        record["graph_backend"] = backend
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
                "graph_revision": public.get("graph_revision"),
                "evidence_count": len(public.get("evidence") or []),
                "source": "edl",
                "evidence_type": "observed",
            }
        )
        change_node = change["id"] if str(change["id"]).startswith("change:") else f"change:{change['id']}"
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

    def _reader_for_review(self):
        """FalkorDB is required in product mode. Memory is only a labeled test double."""
        if falkor.is_ready():
            return falkor.live_reader(), "falkordb"
        if allow_memory_graph():
            return MemoryReader(self.graph), "memory-test-double"
        detail = falkor.status().get("detail") or "FalkorDB is not connected"
        raise GraphUnavailable(
            "FalkorDB is the decision backbone and is not ready. "
            f"{detail}. Set FALKORDB_HOST/PASSWORD and publish a graph before review."
        )

    def snapshot(self) -> dict:
        nodes, edges = self.graph.counts()
        remote = falkor.status()
        return {
            "backend": falkor.backend_label(),
            "falkor": remote,
            "graph_ready": falkor.is_ready() or allow_memory_graph(),
            "dataset": self.dataset,
            "origin": self.origin,
            "note": self.note,
            "scope_note": self.scope_note,
            "nodes": nodes,
            "edges": edges,
            "policy_version": self.policy.get("version"),
            "changes": [_public_change(change) for change in self.changes],
            "services": self.services(),
            "modes": list(MODES),
        }


def _public_change(change: dict) -> dict:
    return {
        "id": change["id"],
        "title": change.get("title"),
        "expected": change.get("expected"),
        "gn": change.get("gn", False),
        "note": change.get("note"),
        "symbols": change.get("symbols", []),
        "files": change.get("files", []),
        "mapping": change.get("mapping"),
        "unresolved_refs": change.get("unresolved_refs", []),
        "scope": change.get("scope"),
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
        "evidence": [],
        "missing": [{"policy": "POLICY_FILE", "detail": reason, "evidence_id": "ev-policy"}],
        "policy_version": None,
        "graph_revision": None,
        "graph_backend": "unavailable",
        "tool_calls": [],
        "graph": {"nodes": [], "edges": []},
        "injection": [],
        "action": {"status": "blocked", "reason": "Policy file failed closed.", "retries": 0},
    }
    return {"active": mode, "runs": {mode: record}}


def _fail_no_graph(change_id: str, mode: str, reason: str) -> dict:
    """Fail closed when FalkorDB cannot back the review. Never invent a safe decision."""
    detail = (
        "FalkorDB did not back this review. The product will not decide from an "
        f"unverified in-memory copy. {reason}"
    )
    record = {
        "id": f"dec:{change_id}:{mode}:no-graph",
        "change_id": change_id,
        "mode": mode,
        "state": "INSUFFICIENT_EVIDENCE",
        "rationale": f"INSUFFICIENT_EVIDENCE. {detail}",
        "claims": [{"text": detail, "evidence_id": "ev-graph-backend", "policy": "P5"}],
        "rejected_claims": [],
        "findings": [
            {
                "severity": "insufficient",
                "policy": "P5",
                "detail": detail,
                "evidence_id": "ev-graph-backend",
            }
        ],
        "triggered": ["P5"],
        "evidence": [
            {
                "id": "ev-graph-backend",
                "source_system": "falkordb",
                "source_ref": None,
                "entity_id": None,
                "version_ref": None,
                "observed_at": None,
                "freshness_ok": False,
                "evidence_type": "observed",
                "role": "missing",
                "detail": detail,
                "limitations": "Without FalkorDB the graph is not the decision backbone.",
                "policy": "P5",
            }
        ],
        "missing": [{"policy": "P5", "detail": detail, "evidence_id": "ev-graph-backend"}],
        "policy_version": None,
        "graph_revision": None,
        "graph_backend": "unavailable",
        "contract_ok": True,
        "contract_errors": [],
        "tool_calls": [],
        "graph": {"nodes": [], "edges": []},
        "injection": [],
        "action": {
            "status": "blocked",
            "reason": "FalkorDB is required and was not ready.",
            "retries": 0,
        },
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
