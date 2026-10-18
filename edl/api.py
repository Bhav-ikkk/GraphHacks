"""HTTP API and the one-page UI."""

from __future__ import annotations

import mimetypes
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.sessions import SessionMiddleware

from edl import github_oauth
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
app.add_middleware(
    SessionMiddleware,
    secret_key=github_oauth.session_secret(),
    same_site="lax",
    https_only=False,
)
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
def index(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    """Serve the UI. Also handles OAuth callback when redirect_uri is http://127.0.0.1:8000/."""
    if error:
        return RedirectResponse(url=f"/?github_error={error}", status_code=302)
    if code:
        expected = request.session.get("oauth_state")
        if not state or not expected or state != expected:
            return RedirectResponse(url="/?github_error=state_mismatch", status_code=302)
        try:
            auth = github_oauth.exchange_code(code)
            github_oauth.store_session(request.session, auth)
            request.session.pop("oauth_state", None)
        except RuntimeError as exc:
            detail = urllib_quote(str(exc)[:120])
            return RedirectResponse(url=f"/?github_error={detail}", status_code=302)
        return RedirectResponse(url="/?github=connected", status_code=302)
    return FileResponse(WEB / "index.html")


def urllib_quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


@app.get("/auth/github/login")
def github_login(request: Request):
    if not github_oauth.oauth_configured():
        raise HTTPException(status_code=400, detail="GitHub OAuth is not configured in .env")
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    return RedirectResponse(url=github_oauth.authorize_url(state), status_code=302)


@app.get("/auth/github/callback")
def github_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    """Alternate callback path if you change the OAuth App redirect URI later."""
    return index(request, code=code, state=state, error=error)


@app.post("/auth/github/logout")
def github_logout(request: Request) -> dict:
    github_oauth.clear_session(request.session)
    return {"ok": True, "github": github_oauth.public_status(request.session)}


@app.get("/api/github/status")
def github_status(request: Request) -> dict:
    return github_oauth.public_status(request.session)


@app.get("/api/github/repos")
def github_repos(request: Request, q: str = "", limit: int = 40) -> dict:
    token = github_oauth.token_from_session(request.session)
    if not token:
        raise HTTPException(status_code=401, detail="Connect GitHub first.")
    try:
        repos = github_oauth.list_repos(token, limit=max(1, min(limit, 80)), query=q)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"repos": repos, "github": github_oauth.public_status(request.session)}


@app.get("/api/github/pulls")
def github_pulls(request: Request) -> dict:
    token = github_oauth.token_from_session(request.session)
    if not token:
        raise HTTPException(status_code=401, detail="Connect GitHub first.")
    from edl.github_pr import _repo_slug

    slug = _repo_slug(engine.snapshot().get("origin") or {})
    if not slug:
        return {
            "pulls": [],
            "detail": "Analyze a GitHub repository first, then pick a pull request.",
        }
    owner, name = slug
    try:
        pulls = github_oauth.list_pulls(token, owner, name)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "pulls": pulls,
        "repo": f"{owner}/{name}",
        "detail": None if pulls else f"No open pull requests on {owner}/{name}.",
    }


@app.get("/api/health")
def health() -> dict:
    snap = engine.snapshot()
    return {"ok": True, "backend": snap["backend"], "dataset": snap["dataset"]}


@app.get("/api/bootstrap")
def bootstrap(request: Request) -> dict:
    snap = engine.snapshot()
    try:
        from edl.llm_agent import llm_settings

        settings = llm_settings()
        snap["llm_ready"] = bool(settings.remote)
        snap["llm_model"] = settings.model if settings.remote else None
    except Exception:
        snap["llm_ready"] = False
        snap["llm_model"] = None
    snap["github"] = github_oauth.public_status(request.session)
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


class OpenDecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    change_id: str
    mode: str = "full"
    dry_run: bool = False


@app.post("/api/github/pr-comment")
def pr_comment(request: Request, body: PrCommentIn) -> dict:
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
            access_token=github_oauth.token_from_session(request.session),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result.get("status") == "blocked":
        raise HTTPException(status_code=400, detail=result.get("detail") or "Could not post the PR comment.")
    return {"github": result, "decision": record}


@app.post("/api/github/open-decision")
def open_decision(request: Request, body: OpenDecisionIn) -> dict:
    """Create a GitHub Issue + PR for the current decision when none exist."""
    from edl.github_pr import open_decision_issue_and_pr

    try:
        payload = engine.review(body.change_id, body.mode)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"No change named {body.change_id}.") from exc
    record = payload["runs"][body.mode]
    try:
        result = open_decision_issue_and_pr(
            engine,
            record,
            dry_run=body.dry_run,
            access_token=github_oauth.token_from_session(request.session),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result.get("status") == "blocked":
        raise HTTPException(
            status_code=400,
            detail=result.get("detail") or "Could not open the GitHub issue/PR.",
        )
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
