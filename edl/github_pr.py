"""Post Syzygy decisions onto a real GitHub pull request.

Requires GITHUB_TOKEN (or GH_TOKEN) with pull-requests:write.
Optional check runs need checks:write.

When the repo has no open PR, open_decision_issue_and_pr() creates an Issue,
a branch with a decision file, and a PR that links the issue.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any

from edl.cli_report import next_steps_lines


def post_decision_to_pr(
    engine,
    record: dict,
    *,
    pr_number: int,
    create_check: bool = False,
    dry_run: bool = False,
    access_token: str | None = None,
) -> dict[str, Any]:
    snap = engine.snapshot()
    origin = snap.get("origin") or {}
    slug = _repo_slug(origin)
    if not slug:
        return {
            "status": "blocked",
            "detail": (
                "Origin is not a GitHub repository. Ingest a github.com URL "
                "(or a local clone with origin pointing at GitHub)."
            ),
        }
    owner, name = slug
    body = format_pr_markdown(engine, record)
    if dry_run:
        return {
            "status": "dry_run",
            "detail": f"Would comment on {owner}/{name}#{pr_number}",
            "body_preview": body,
        }
    token = (access_token or "").strip() or _token()
    if not token:
        return {
            "status": "blocked",
            "detail": (
                "Connect GitHub in the UI (OAuth) or set GITHUB_TOKEN / GH_TOKEN in .env "
                "to post PR comments."
            ),
            "body_preview": body,
        }
    comment = _api(
        "POST",
        f"/repos/{owner}/{name}/issues/{pr_number}/comments",
        token,
        {"body": body},
    )
    result: dict[str, Any] = {
        "status": "posted",
        "comment_url": comment.get("html_url"),
        "detail": f"Commented on {owner}/{name}#{pr_number}",
        "body_preview": body[:500],
    }
    if create_check:
        head_sha = _pr_head_sha(owner, name, pr_number, token)
        if head_sha:
            check = _create_check(owner, name, head_sha, record, token)
            result["check_url"] = check.get("html_url")
            result["detail"] += " and opened a Check Run."
        else:
            result["detail"] += " (check skipped: could not resolve PR head SHA)."
    return result


def open_decision_issue_and_pr(
    engine,
    record: dict,
    *,
    dry_run: bool = False,
    access_token: str | None = None,
) -> dict[str, Any]:
    """Create a GitHub Issue + PR when the repo has nothing to comment on yet."""
    snap = engine.snapshot()
    origin = snap.get("origin") or {}
    slug = _repo_slug(origin)
    if not slug:
        return {
            "status": "blocked",
            "detail": (
                "Origin is not a GitHub repository. Analyze a github.com URL first."
            ),
        }
    owner, name = slug
    body = format_pr_markdown(engine, record)
    state = str(record.get("state") or "UNKNOWN")
    change_id = str(record.get("change_id") or "change")
    title = f"Syzygy EDL: {state} · {change_id}"
    if dry_run:
        return {
            "status": "dry_run",
            "detail": f"Would open issue + PR on {owner}/{name}",
            "body_preview": body,
        }
    token = (access_token or "").strip() or _token()
    if not token:
        return {
            "status": "blocked",
            "detail": (
                "Connect GitHub in the UI (OAuth) or set GITHUB_TOKEN / GH_TOKEN in .env."
            ),
            "body_preview": body,
        }

    issue = _api(
        "POST",
        f"/repos/{owner}/{name}/issues",
        token,
        {"title": title, "body": body},
    )
    issue_number = issue.get("number")
    issue_url = issue.get("html_url")

    meta = _api("GET", f"/repos/{owner}/{name}", token)
    base = str(meta.get("default_branch") or "main")
    ref = _api("GET", f"/repos/{owner}/{name}/git/ref/heads/{base}", token)
    base_sha = ((ref.get("object") or {}).get("sha")) or ""
    if not base_sha:
        return {
            "status": "partial",
            "detail": f"Opened issue #{issue_number}, but could not read default branch {base}.",
            "issue_url": issue_url,
            "issue_number": issue_number,
        }

    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", change_id).strip("-")[:40] or "change"
    branch = f"syzygy/edl-{safe}-{stamp}"
    _api(
        "POST",
        f"/repos/{owner}/{name}/git/refs",
        token,
        {"ref": f"refs/heads/{branch}", "sha": base_sha},
    )
    path = f".syzygy/decision-{stamp}.md"
    content = base64.b64encode(body.encode("utf-8")).decode("ascii")
    _api(
        "PUT",
        f"/repos/{owner}/{name}/contents/{path}",
        token,
        {
            "message": f"chore(syzygy): record {state} decision for {change_id}",
            "content": content,
            "branch": branch,
        },
    )
    pr_body = (
        f"Opened automatically by Syzygy EDL after a graph-backed review.\n\n"
        f"Related issue: #{issue_number}\n\n"
        f"{body}"
    )
    pull = _api(
        "POST",
        f"/repos/{owner}/{name}/pulls",
        token,
        {
            "title": title,
            "head": branch,
            "base": base,
            "body": pr_body,
            "draft": False,
        },
    )
    return {
        "status": "opened",
        "detail": f"Opened issue #{issue_number} and PR #{pull.get('number')} on {owner}/{name}",
        "issue_url": issue_url,
        "issue_number": issue_number,
        "pr_url": pull.get("html_url"),
        "pr_number": pull.get("number"),
        "branch": branch,
        "body_preview": body[:500],
    }


def format_pr_markdown(engine, record: dict) -> str:
    snap = engine.snapshot()
    origin = snap.get("origin") or {}
    state = record.get("state") or "UNKNOWN"
    lines = [
        "## Syzygy EDL decision",
        "",
        f"**State:** `{state}`  ",
        f"**Change:** `{record.get('change_id')}`  ",
        f"**Mode:** `{record.get('mode')}`  ",
        f"**Backend:** `{record.get('graph_backend')}`  ",
        f"**Policy:** `{record.get('policy_version')}`  ",
        f"**Graph revision:** `{record.get('graph_revision')}`",
        "",
        "### Rationale",
        str(record.get("rationale") or ""),
        "",
        "### Triggered policies",
        ", ".join(f"`{item}`" for item in (record.get("triggered") or [])) or "_none_",
        "",
        "### Claims",
    ]
    claims = record.get("claims") or []
    if not claims:
        lines.append("_none_")
    for claim in claims:
        if isinstance(claim, dict):
            lines.append(f"- `{claim.get('evidence_id')}` — {claim.get('text')}")
    missing = record.get("missing") or []
    if missing:
        lines.append("")
        lines.append("### Missing / stale")
        for item in missing:
            if isinstance(item, dict):
                lines.append(f"- {item.get('detail') or item}")
            else:
                lines.append(f"- {item}")
    graph = record.get("graph") or {}
    nodes = graph.get("nodes") or []
    if nodes:
        lines.append("")
        lines.append("### Impact nodes used")
        for node in nodes[:20]:
            lines.append(
                f"- `{node.get('id')}` ({node.get('role') or node.get('kind') or 'node'})"
            )
    lines.append("")
    lines.append("### Next steps")
    for step in next_steps_lines(record, record.get("action") or {}, origin):
        lines.append(step.strip())
    lines.append("")
    lines.append(
        "_Generated by Syzygy EDL. Policy is the judge; FalkorDB is the witness. "
        "Text cannot upgrade the state._"
    )
    return "\n".join(lines)


def _repo_slug(origin: dict) -> tuple[str, str] | None:
    kind = origin.get("kind")
    value = str(origin.get("value") or "")
    if kind == "github":
        parts = value.split("/")
        if len(parts) >= 2:
            return parts[0], parts[1]
    if "github.com" in value:
        # local path may still have been ingested as path; try remote via git later
        cleaned = value.rstrip("/").split("github.com/")[-1]
        parts = cleaned.replace(".git", "").split("/")
        if len(parts) >= 2:
            return parts[0], parts[1]
    if kind == "path":
        return _git_remote_slug(value)
    return None


def _git_remote_slug(path: str) -> tuple[str, str] | None:
    import subprocess

    try:
        result = subprocess.run(
            ["git", "-C", path, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    url = (result.stdout or "").strip()
    if "github.com" not in url:
        return None
    cleaned = url.replace("git@github.com:", "").replace("https://github.com/", "")
    cleaned = cleaned.replace("ssh://git@github.com/", "").removesuffix(".git")
    parts = cleaned.split("/")
    if len(parts) >= 2:
        return parts[0], parts[1]
    return None


def explain_github_error(code: int, detail: str = "") -> str:
    text = (detail or "").lower()
    if code in {401, 403}:
        return "GitHub refused this session. Disconnect and Connect GitHub again, then retry."
    if code == 404:
        return "That pull request was not found on this repository. Pick an open PR from the list."
    if code == 422:
        return "GitHub rejected the comment. The pull request may be locked, or the number is invalid."
    if "bad credentials" in text:
        return "GitHub credentials expired. Disconnect and Connect GitHub again."
    return f"GitHub could not post that comment ({code})."


def _token() -> str:
    return (
        os.environ.get("GITHUB_TOKEN", "").strip()
        or os.environ.get("GH_TOKEN", "").strip()
        or os.environ.get("SYZYGY_GITHUB_TOKEN", "").strip()
    )


def _api(method: str, path: str, token: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.github.com{path}",
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "syzygy-edl",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(explain_github_error(exc.code, detail)) from exc


def _pr_head_sha(owner: str, name: str, pr_number: int, token: str) -> str | None:
    try:
        pr = _api("GET", f"/repos/{owner}/{name}/pulls/{pr_number}", token)
    except RuntimeError:
        return None
    return ((pr.get("head") or {}).get("sha")) or None


def _create_check(owner: str, name: str, head_sha: str, record: dict, token: str) -> dict:
    state = str(record.get("state") or "")
    conclusion = {
        "PROCEED": "success",
        "PROCEED_WITH_GUARDS": "neutral",
        "NEEDS_REVIEW": "neutral",
        "HOLD": "failure",
        "INSUFFICIENT_EVIDENCE": "neutral",
    }.get(state, "neutral")
    payload = {
        "name": "Syzygy EDL",
        "head_sha": head_sha,
        "status": "completed",
        "conclusion": conclusion,
        "output": {
            "title": f"Syzygy EDL: {state}",
            "summary": str(record.get("rationale") or state)[:65535],
        },
    }
    return _api("POST", f"/repos/{owner}/{name}/check-runs", token, payload)
