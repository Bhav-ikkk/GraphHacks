"""Named graph tools for the agent and MCP server.

Every tool is bounded, structured, and backed by the graph reader (FalkorDB in
product mode). Free-text Cypher is never accepted.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from edl.engine import Engine
from edl.gate import assess, execute
from edl.graph_reader import GraphUnavailable
from edl.models import NOW
from edl.review import public_record, review_change

ToolHandler = Callable[["ToolContext", dict], dict]


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _envelope(
    *,
    tool: str,
    backend: str,
    result: Any,
    partial: bool = False,
    limitations: str | None = None,
    evidence_type: str = "observed",
) -> dict:
    return {
        "tool": tool,
        "ok": True,
        "backend": backend,
        "evidence_type": evidence_type,
        "observed_at": _stamp(),
        "partial": partial,
        "limitations": limitations,
        "result": result,
    }


TOOL_SPECS: list[dict] = [
    {
        "name": "ingest_repository",
        "description": (
            "Load a GitHub URL or local folder into the engineering graph and "
            "optionally scope a change from files or git base/head."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string"},
                "files": {"type": "array", "items": {"type": "string"}},
                "base": {"type": "string"},
                "head": {"type": "string"},
            },
            "required": ["target"],
        },
    },
    {
        "name": "scope_change",
        "description": "Scope one engineering change from modified files or a git range.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "files": {"type": "array", "items": {"type": "string"}},
                "base": {"type": "string"},
                "head": {"type": "string"},
                "title": {"type": "string"},
            },
        },
    },
    {
        "name": "list_changes",
        "description": "List scoped changes available for review.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "impact_analysis",
        "description": (
            "Multi-hop blast radius: services that depend on the given service "
            "via DEPENDS_ON (walked backwards)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "service_id": {
                    "type": "string",
                    "description": "Graph id such as svc:payments",
                },
                "hops": {"type": "integer", "minimum": 1, "maximum": 8, "default": 3},
            },
            "required": ["service_id"],
        },
    },
    {
        "name": "nearest_owner",
        "description": "Owner team/engineer for a service from OWNS edges (CODEOWNERS).",
        "inputSchema": {
            "type": "object",
            "properties": {"service_id": {"type": "string"}},
            "required": ["service_id"],
        },
    },
    {
        "name": "find_similar_incidents",
        "description": "Open incidents affecting a service and prior fixes on a function.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "service_id": {"type": "string"},
                "function_id": {"type": "string"},
            },
        },
    },
    {
        "name": "get_test_coverage",
        "description": "Test nodes covering a function and their status.",
        "inputSchema": {
            "type": "object",
            "properties": {"function_id": {"type": "string"}},
            "required": ["function_id"],
        },
    },
    {
        "name": "get_deploy_state",
        "description": "Deployment record for a service, if present.",
        "inputSchema": {
            "type": "object",
            "properties": {"service_id": {"type": "string"}},
            "required": ["service_id"],
        },
    },
    {
        "name": "centrality_rank",
        "description": "Caller count for a function via CALLS edges.",
        "inputSchema": {
            "type": "object",
            "properties": {"function_id": {"type": "string"}},
            "required": ["function_id"],
        },
    },
    {
        "name": "decide_change",
        "description": (
            "Run the full decision lifecycle on a scoped change: graph tools + "
            "deterministic policy. Returns state, evidence, and claims."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "change_id": {"type": "string"},
                "mode": {
                    "type": "string",
                    "enum": ["full", "one_hop", "graph_off"],
                    "default": "full",
                },
            },
            "required": ["change_id"],
        },
    },
    {
        "name": "propose_action",
        "description": (
            "Gate a follow-up action from a decision. By default only assesses; "
            "set execute=true to create a local issue when permitted."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "change_id": {"type": "string"},
                "mode": {"type": "string", "default": "full"},
                "execute": {"type": "boolean", "default": False},
            },
            "required": ["change_id"],
        },
    },
]


class ToolContext:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def reader_and_backend(self):
        return self.engine._reader_for_review()


class ToolError(Exception):
    pass


def list_tools() -> list[dict]:
    return [dict(spec) for spec in TOOL_SPECS]


def call_tool(ctx: ToolContext, name: str, arguments: dict | None = None) -> dict:
    args = dict(arguments or {})
    handler = _HANDLERS.get(name)
    if handler is None:
        raise ToolError(f"Unknown tool: {name}")
    try:
        return handler(ctx, args)
    except GraphUnavailable as exc:
        return {
            "tool": name,
            "ok": False,
            "backend": "unavailable",
            "evidence_type": "observed",
            "observed_at": _stamp(),
            "partial": True,
            "limitations": str(exc),
            "result": {"error": str(exc)},
        }
    except Exception as exc:
        return {
            "tool": name,
            "ok": False,
            "backend": "error",
            "evidence_type": "synthetic",
            "observed_at": _stamp(),
            "partial": True,
            "limitations": str(exc),
            "result": {"error": str(exc)},
        }


def _ingest(ctx: ToolContext, args: dict) -> dict:
    target = str(args.get("target") or "").strip()
    if not target:
        raise ToolError("target is required")
    files = args.get("files")
    summary = ctx.engine.use_repository(
        target,
        base=args.get("base"),
        head=args.get("head"),
        files=files if isinstance(files, list) else None,
    )
    snap = ctx.engine.snapshot()
    return _envelope(
        tool="ingest_repository",
        backend=snap["backend"],
        result={
            "origin": summary.get("origin"),
            "nodes": summary.get("nodes"),
            "edges": summary.get("edges"),
            "change_count": summary.get("changes"),
            "changes": snap.get("changes") or [],
            "scope_note": summary.get("scope_note"),
            "services": [row["name"] for row in summary.get("services") or []],
            "evidence_notes": list(getattr(ctx.engine, "evidence_notes", []) or []),
        },
        limitations=summary.get("scope_error"),
        partial=bool(summary.get("scope_error")),
    )


def _scope(ctx: ToolContext, args: dict) -> dict:
    result = ctx.engine.scope_change(
        base=args.get("base"),
        head=args.get("head"),
        files=args.get("files") if isinstance(args.get("files"), list) else None,
        title=args.get("title"),
    )
    return _envelope(
        tool="scope_change",
        backend=ctx.engine.snapshot()["backend"],
        result=result.get("change") or result,
    )


def _list_changes(ctx: ToolContext, _args: dict) -> dict:
    snap = ctx.engine.snapshot()
    return _envelope(
        tool="list_changes",
        backend=snap["backend"],
        result={"changes": snap.get("changes") or []},
    )


def _impact(ctx: ToolContext, args: dict) -> dict:
    reader, backend = ctx.reader_and_backend()
    service_id = _require_id(args, "service_id")
    hops = int(args.get("hops") or 3)
    found, truncated = reader.consumers(service_id, hops)
    consumers = []
    for sid, distance in sorted(found.items(), key=lambda item: (item[1], item[0])):
        node = reader.node(sid)
        consumers.append(
            {
                "service_id": sid,
                "name": None if node is None else node.get("name"),
                "tier": None if node is None else node.get("tier"),
                "hops": distance,
            }
        )
    return _envelope(
        tool="impact_analysis",
        backend=backend,
        result={"service_id": service_id, "hops": hops, "consumers": consumers, "truncated": truncated},
        partial=truncated,
        limitations="Traversal stopped at hop limit." if truncated else None,
    )


def _owner(ctx: ToolContext, args: dict) -> dict:
    reader, backend = ctx.reader_and_backend()
    service_id = _require_id(args, "service_id")
    owner = reader.owner_of(service_id, NOW, ctx.engine.policy["freshness"]["owner_days"])
    return _envelope(
        tool="nearest_owner",
        backend=backend,
        result=owner,
        partial=bool(owner.get("missing") or owner.get("stale")),
        limitations="Owner missing or stale." if owner.get("missing") or owner.get("stale") else None,
    )


def _incidents(ctx: ToolContext, args: dict) -> dict:
    reader, backend = ctx.reader_and_backend()
    service_id = args.get("service_id")
    function_id = args.get("function_id")
    open_rows = reader.open_incidents(service_id) if service_id else []
    prior = reader.prior_fixes(function_id) if function_id else []
    return _envelope(
        tool="find_similar_incidents",
        backend=backend,
        result={
            "open": [
                {"id": item.get("id"), "name": item.get("name"), "age_days": item.get("age_days")}
                for item in open_rows
            ],
            "prior": [
                {
                    "incident": pair["incident"].get("name"),
                    "fix": pair["fix"].get("name"),
                }
                for pair in prior
            ],
        },
        partial=not service_id and not function_id,
        limitations=None if service_id or function_id else "Provide service_id and/or function_id.",
    )


def _tests(ctx: ToolContext, args: dict) -> dict:
    reader, backend = ctx.reader_and_backend()
    function_id = _require_id(args, "function_id")
    covering = reader.tests_covering(function_id)
    status = reader.test_status(function_id)
    return _envelope(
        tool="get_test_coverage",
        backend=backend,
        result={
            "function_id": function_id,
            "status": status,
            "tests": [
                {"id": item.get("id"), "name": item.get("name"), "status": item.get("status")}
                for item in covering
            ],
        },
        partial=status != "pass",
        limitations="A test file is not a passing run unless status is pass.",
    )


def _deploy(ctx: ToolContext, args: dict) -> dict:
    reader, backend = ctx.reader_and_backend()
    service_id = _require_id(args, "service_id")
    node = reader.deploy_of(service_id)
    if node is None:
        return _envelope(
            tool="get_deploy_state",
            backend=backend,
            result={"service_id": service_id, "present": False},
            partial=True,
            limitations="No deployment record in the graph.",
        )
    return _envelope(
        tool="get_deploy_state",
        backend=backend,
        result={
            "service_id": service_id,
            "present": True,
            "age_hours": node.get("age_hours"),
            "health": node.get("health"),
            "name": node.get("name"),
        },
    )


def _centrality(ctx: ToolContext, args: dict) -> dict:
    reader, backend = ctx.reader_and_backend()
    function_id = _require_id(args, "function_id")
    callers = reader.callers(function_id)
    return _envelope(
        tool="centrality_rank",
        backend=backend,
        result={"function_id": function_id, "callers": len(callers), "caller_ids": callers},
    )


def _decide(ctx: ToolContext, args: dict) -> dict:
    change_id = str(args.get("change_id") or "").strip()
    mode = str(args.get("mode") or "full")
    reader, backend = ctx.reader_and_backend()
    change = ctx.engine.change(change_id)
    record = review_change(
        ctx.engine.graph, change, ctx.engine.policy, mode, reader=reader
    )
    record["graph_backend"] = backend
    public = public_record(record)
    public["action"] = assess(record, change)
    return _envelope(
        tool="decide_change",
        backend=backend,
        result={
            "state": public["state"],
            "rationale": public["rationale"],
            "triggered": public.get("triggered"),
            "claims": public.get("claims"),
            "missing": public.get("missing"),
            "evidence_ids": [item["id"] for item in public.get("evidence") or []],
            "graph_revision": public.get("graph_revision"),
            "action": public.get("action"),
            "graph": public.get("graph"),
        },
    )


def _propose(ctx: ToolContext, args: dict) -> dict:
    change_id = str(args.get("change_id") or "").strip()
    mode = str(args.get("mode") or "full")
    do_execute = bool(args.get("execute"))
    reader, backend = ctx.reader_and_backend()
    change = ctx.engine.change(change_id)
    record = review_change(
        ctx.engine.graph, change, ctx.engine.policy, mode, reader=reader
    )
    if do_execute:
        outcome = execute(record, change, ctx.engine.issued)
        if outcome.get("status") == "created" and "id" not in outcome:
            ctx.engine.issue_seq += 1
            outcome["id"] = f"LOCAL-{ctx.engine.issue_seq:03d}"
            ctx.engine.issued[change_id] = outcome
    else:
        outcome = assess(record, change)
    return _envelope(
        tool="propose_action",
        backend=backend,
        result={"decision_state": record["state"], "action": outcome, "executed": do_execute},
    )


def _require_id(args: dict, key: str) -> str:
    value = str(args.get(key) or "").strip()
    if not value:
        raise ToolError(f"{key} is required")
    return value


_HANDLERS: dict[str, ToolHandler] = {
    "ingest_repository": _ingest,
    "scope_change": _scope,
    "list_changes": _list_changes,
    "impact_analysis": _impact,
    "nearest_owner": _owner,
    "find_similar_incidents": _incidents,
    "get_test_coverage": _tests,
    "get_deploy_state": _deploy,
    "centrality_rank": _centrality,
    "decide_change": _decide,
    "propose_action": _propose,
}
