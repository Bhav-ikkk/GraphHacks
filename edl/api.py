"""HTTP API and the one-page UI."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from edl.change import ChangeError
from edl.engine import Engine
from edl.ingest import IngestError
from edl.policy import PolicyError

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


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


@app.get("/api/health")
def health() -> dict:
    snap = engine.snapshot()
    return {"ok": True, "backend": snap["backend"], "dataset": snap["dataset"]}


@app.get("/api/bootstrap")
def bootstrap() -> dict:
    return engine.snapshot()


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
        return engine.review(body.change_id, body.mode)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"No change named {body.change_id}.") from exc


@app.post("/api/actions")
def act(body: ActionIn) -> dict:
    try:
        return engine.act(body.change_id, body.mode)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"No change named {body.change_id}.") from exc


def _rejects_query(value: str) -> bool:
    text = value.lower()
    return "cypher" in text or text.strip().startswith("match ") or text.strip().startswith("create ")
