"""HTTP API and the one-page UI."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from edl.change import ChangeError
from edl.engine import Engine
from edl.ingest import IngestError
from edl.policy import PolicyError

# Windows maps .js → text/plain; browsers then refuse ES module scripts.
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
POLICY = ROOT / "policies" / "policies.yaml"
LOG = ROOT / "logs" / "trace.jsonl"

try:
    engine = Engine(POLICY, LOG)
except PolicyError as exc:
    raise RuntimeError(f"policies/policies.yaml failed to load: {exc}") from exc

app = FastAPI(title="Syzygy EDL", version="0.1.0")
app.mount("/static", StaticFiles(directory=WEB), name="static")


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    change_id: str
    mode: str = "full"


class IngestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = Field(min_length=1)
    base: str | None = None
    head: str | None = None
    files: list[str] | None = None


class ScopeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base: str | None = None
    head: str | None = None
    files: list[str] | None = None
    title: str | None = None


class ActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    change_id: str
    mode: str = "full"


class NotifyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    change_id: str
    mode: str = "full"
    send: bool = False


class PrCommentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    change_id: str
    mode: str = "full"
    pr: int = Field(ge=1)
    check: bool = False
    dry_run: bool = False


class AgentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    change_id: str | None = None
    mode: str = "full"
    llm: bool = False
    hops: int = Field(default=3, ge=1, le=8)
    execute: bool = False


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


@app.get("/api/health")
def health() -> dict:
    snap = engine.snapshot()
    return {"ok": True, "backend": snap["backend"], "dataset": snap["dataset"]}


@app.get("/api/bootstrap")
def bootstrap() -> dict:
    snap = engine.snapshot()
    try:
        from edl.llm_agent import llm_settings

        settings = llm_settings()
        snap["llm_ready"] = bool(settings.remote)
        snap["llm_model"] = settings.model if settings.remote else None
    except Exception:
        snap["llm_ready"] = False
        snap["llm_model"] = None
    return snap


@app.post("/api/ingest")
def ingest_repo(body: IngestIn) -> dict:
    if _rejects_query(body.target):
        raise HTTPException(status_code=400, detail="Free-text Cypher is rejected. Only named tools run.")
    try:
        summary = engine.use_repository(
            body.target,
            base=body.base,
            head=body.head,
            files=body.files,
        )
    except IngestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {**summary, "snapshot": engine.snapshot()}


@app.post("/api/change")
def scope_change(body: ScopeIn) -> dict:
    try:
        return engine.scope_change(
            base=body.base,
            head=body.head,
            files=body.files,
            title=body.title,
        )
    except ChangeError as exc:
        snap = engine.snapshot()
        detail = str(exc)
        if snap.get("suggested_files") and "Suggested files" not in detail:
            shown = ", ".join(snap["suggested_files"][:6])
            detail = f"{detail} Suggested files from this tree: {shown}."
        raise HTTPException(status_code=400, detail=detail) from exc


@app.get("/api/suggest-files")
def suggest_files() -> dict:
    snap = engine.snapshot()
    return {
        "suggested_files": snap.get("suggested_files") or [],
        "scope_error": snap.get("scope_error"),
        "scope_note": snap.get("scope_note"),
        "changes": snap.get("changes") or [],
    }


@app.post("/api/review")
def review(body: ReviewIn) -> dict:
    if body.mode not in {"full", "one_hop", "graph_off"}:
        raise HTTPException(status_code=400, detail="mode must be full, one_hop, or graph_off.")
    try:
        payload = engine.review(body.change_id, body.mode)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"No change named {body.change_id}.") from exc
    return _enrich_review(payload)


@app.post("/api/actions")
def act(body: ActionIn) -> dict:
    try:
        return engine.act(body.change_id, body.mode)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"No change named {body.change_id}.") from exc


@app.post("/api/agent")
def agent_run(body: AgentIn) -> dict:
    """Run the graph-backed agent on the already-loaded repository change."""
    if engine.dataset == "empty" or not engine.changes:
        raise HTTPException(
            status_code=400,
            detail="Analyze a repository and scope a change before running the agent.",
        )
    if body.mode not in {"full", "one_hop", "graph_off"}:
        raise HTTPException(status_code=400, detail="mode must be full, one_hop, or graph_off.")
    change_id = body.change_id or engine.changes[0]["id"]
    try:
        engine.change(change_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"No change named {change_id}.") from exc

    if body.llm:
        from edl.llm_agent import run_llm_agent

        result = run_llm_agent(
            None,
            change_id=change_id,
            mode=body.mode,
            execute_action=body.execute,
            hops=body.hops,
            engine=engine,
        )
    else:
        from edl.agent import run_agent

        result = run_agent(
            None,
            change_id=change_id,
            mode=body.mode,
            execute_action=body.execute,
            hops=body.hops,
            engine=engine,
        )
    return result


@app.post("/api/notify")
def notify(body: NotifyIn) -> dict:
    from edl.notify import notify_owners

    try:
        payload = engine.review(body.change_id, body.mode)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"No change named {body.change_id}.") from exc
    record = payload["runs"][body.mode]
    return {"notification": notify_owners(engine, record, send=body.send), "decision": record}


@app.post("/api/github/pr-comment")
def pr_comment(body: PrCommentIn) -> dict:
    from edl.github_pr import post_decision_to_pr

    try:
        payload = engine.review(body.change_id, body.mode)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"No change named {body.change_id}.") from exc
    record = payload["runs"][body.mode]
    try:
        result = post_decision_to_pr(
            engine,
            record,
            pr_number=body.pr,
            create_check=body.check,
            dry_run=body.dry_run,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"github": result, "decision": record}


def _enrich_review(payload: dict) -> dict:
    """Attach Impact / next_steps so the UI matches the CLI report shape."""
    from edl.cli_report import next_steps_lines

    snap = engine.snapshot()
    origin = snap.get("origin") or {}
    for mode, record in (payload.get("runs") or {}).items():
        graph = record.get("graph") or {}
        record["impact"] = {
            "nodes": graph.get("nodes") or [],
            "edges": graph.get("edges") or [],
        }
        record["next_steps"] = [
            line.strip() for line in next_steps_lines(record, record.get("action") or {}, origin)
        ]
    return payload


def _rejects_query(value: str) -> bool:
    text = value.lower()
    return "cypher" in text or text.strip().startswith("match ") or text.strip().startswith("create ")
