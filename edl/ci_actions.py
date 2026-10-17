"""Pull the latest GitHub Actions run into the CI evidence contract.

Structural ingest only knows whether a workflow file exists. This module asks
the GitHub API for the newest workflow run when a token and a GitHub remote
(or origin) are available. Results merge into the same ci{} shape used by
syzygy.evidence.yaml and policy P5.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_GITHUB_REMOTE = re.compile(
    r"(?:https://github\.com/|git@github\.com:)([^/]+)/([^/.]+?)(?:\.git)?$"
)


def github_token() -> str:
    return (
        os.environ.get("GITHUB_TOKEN", "").strip()
        or os.environ.get("GH_TOKEN", "").strip()
    )


def resolve_github_slug(origin: dict | None, root: Path | None = None) -> str | None:
    if origin and origin.get("kind") == "github":
        return str(origin.get("value") or "").strip() or None
    if origin and origin.get("kind") == "path":
        root = Path(origin["value"])
    if root is None:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    match = _GITHUB_REMOTE.search((result.stdout or "").strip())
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}"


def fetch_latest_actions_run(slug: str, *, token: str | None = None) -> dict[str, Any] | None:
    """Return a ci{} fragment from the newest Actions run, or None."""
    auth = token if token is not None else github_token()
    url = f"https://api.github.com/repos/{slug}/actions/runs?per_page=1"
    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("User-Agent", "syzygy-edl")
    if auth:
        request.add_header("Authorization", f"Bearer {auth}")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403, 404}:
            return None
        return None
    except urllib.error.URLError:
        return None
    runs = payload.get("workflow_runs") or []
    if not runs:
        return {
            "present": False,
            "result": None,
            "age_hours": None,
            "configured": True,
            "source": "github-actions",
            "detail": f"No workflow runs yet for {slug}.",
        }
    run = runs[0]
    conclusion = (run.get("conclusion") or "").lower()
    status = (run.get("status") or "").lower()
    if status != "completed":
        result = None
        present = False
    elif conclusion == "success":
        result = "pass"
        present = True
    elif conclusion in {"failure", "timed_out", "cancelled"}:
        result = "fail"
        present = True
    else:
        result = None
        present = False
    age_hours = _age_hours(run.get("updated_at") or run.get("created_at"))
    return {
        "present": present,
        "result": result,
        "age_hours": age_hours,
        "configured": True,
        "source": "github-actions",
        "run_id": run.get("id"),
        "html_url": run.get("html_url"),
        "workflow": (run.get("name") or ""),
        "detail": f"GitHub Actions run {run.get('id')} conclusion={conclusion or status}.",
    }


def merge_ci_into_overlay(overlay: dict, live_ci: dict | None) -> dict:
    """Prefer a live Actions run over a static evidence-file CI block."""
    merged = dict(overlay or {})
    if not live_ci:
        return merged
    existing = merged.get("ci") if isinstance(merged.get("ci"), dict) else {}
    # Live run with a conclusion wins. Otherwise keep the file when it has a result.
    if live_ci.get("present") and live_ci.get("result"):
        merged["ci"] = {
            "present": live_ci["present"],
            "result": live_ci["result"],
            "age_hours": live_ci.get("age_hours"),
            "configured": True,
        }
        merged["_ci_source"] = live_ci.get("source") or "github-actions"
        merged["_ci_detail"] = live_ci.get("detail")
        return merged
    if existing.get("present") and existing.get("result"):
        return merged
    merged["ci"] = {
        "present": bool(live_ci.get("present")),
        "result": live_ci.get("result"),
        "age_hours": live_ci.get("age_hours"),
        "configured": bool(live_ci.get("configured", True)),
    }
    merged["_ci_source"] = live_ci.get("source") or "github-actions"
    merged["_ci_detail"] = live_ci.get("detail")
    return merged


def enrich_overlay_with_actions(overlay: dict, origin: dict | None) -> tuple[dict, list[str]]:
    notes: list[str] = []
    slug = resolve_github_slug(origin)
    if not slug:
        return overlay, notes
    live = fetch_latest_actions_run(slug)
    if live is None:
        notes.append(f"actions:{slug}:unavailable")
        return overlay, notes
    merged = merge_ci_into_overlay(overlay, live)
    if live.get("result"):
        notes.append(f"actions:{slug}:{live['result']}")
    else:
        notes.append(f"actions:{slug}:no-result")
    return merged, notes


def _age_hours(stamp: str | None) -> float | None:
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Live Actions age uses wall clock. Eval fixtures keep the fixed edl.models.NOW.
    now = datetime.now(timezone.utc)
    delta = now - when.astimezone(timezone.utc)
    hours = delta.total_seconds() / 3600.0
    if hours < 0:
        return 0.0
    return round(hours, 2)
