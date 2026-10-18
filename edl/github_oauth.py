"""GitHub OAuth App login for the Syzygy UI.

Uses a standard OAuth App (Client ID + Client Secret), not a GitHub App.
Tokens stay in the signed session cookie — never logged.
"""

from __future__ import annotations

import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from edl.config import load_env

load_env()

DEFAULT_REDIRECT = "http://127.0.0.1:8000/"
DEFAULT_SCOPES = "repo read:user"


def oauth_configured() -> bool:
    return bool(client_id() and client_secret())


def client_id() -> str:
    return os.environ.get("GITHUB_OAUTH_CLIENT_ID", "").strip()


def client_secret() -> str:
    return os.environ.get("GITHUB_OAUTH_CLIENT_SECRET", "").strip()


def redirect_uri() -> str:
    value = os.environ.get("GITHUB_OAUTH_REDIRECT_URI", "").strip()
    return value or DEFAULT_REDIRECT


def scopes() -> str:
    return os.environ.get("GITHUB_OAUTH_SCOPES", DEFAULT_SCOPES).strip() or DEFAULT_SCOPES


def session_secret() -> str:
    existing = os.environ.get("SYZYGY_SESSION_SECRET", "").strip()
    if existing:
        return existing
    # Ephemeral fallback so local serve works without extra setup.
    generated = secrets.token_urlsafe(32)
    os.environ["SYZYGY_SESSION_SECRET"] = generated
    return generated


def authorize_url(state: str) -> str:
    if not oauth_configured():
        raise RuntimeError("GitHub OAuth is not configured (set GITHUB_OAUTH_CLIENT_ID/SECRET).")
    query = urllib.parse.urlencode(
        {
            "client_id": client_id(),
            "redirect_uri": redirect_uri(),
            "scope": scopes(),
            "state": state,
            "allow_signup": "true",
        }
    )
    return f"https://github.com/login/oauth/authorize?{query}"


def exchange_code(code: str) -> dict[str, Any]:
    """Trade an authorization code for an access token + user profile."""
    if not oauth_configured():
        raise RuntimeError("GitHub OAuth is not configured.")
    payload = urllib.parse.urlencode(
        {
            "client_id": client_id(),
            "client_secret": client_secret(),
            "code": code,
            "redirect_uri": redirect_uri(),
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://github.com/login/oauth/access_token",
        data=payload,
        method="POST",
        headers={
            "Accept": "application/json",
            "User-Agent": "syzygy-edl",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"GitHub token exchange failed: {exc.code} {detail}") from exc

    token = (body.get("access_token") or "").strip()
    if not token:
        err = body.get("error_description") or body.get("error") or "no access_token"
        raise RuntimeError(f"GitHub OAuth denied: {err}")

    user = _github_user(token)
    return {
        "access_token": token,
        "scope": body.get("scope") or scopes(),
        "token_type": body.get("token_type") or "bearer",
        "login": user.get("login"),
        "name": user.get("name"),
        "avatar_url": user.get("avatar_url"),
        "html_url": user.get("html_url"),
    }


def _github_user(token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        "https://api.github.com/user",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "syzygy-edl",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def public_status(session: dict | None) -> dict[str, Any]:
    """Safe status blob for the UI (never includes the token)."""
    session = session or {}
    connected = bool(session.get("github_token"))
    return {
        "oauth_configured": oauth_configured(),
        "connected": connected,
        "login": session.get("github_login") if connected else None,
        "name": session.get("github_name") if connected else None,
        "avatar_url": session.get("github_avatar") if connected else None,
        "redirect_uri": redirect_uri(),
        "scopes": scopes(),
    }


def store_session(session: dict, auth: dict[str, Any]) -> None:
    session["github_token"] = auth["access_token"]
    session["github_login"] = auth.get("login")
    session["github_name"] = auth.get("name")
    session["github_avatar"] = auth.get("avatar_url")
    session["github_scope"] = auth.get("scope")


def clear_session(session: dict) -> None:
    for key in (
        "github_token",
        "github_login",
        "github_name",
        "github_avatar",
        "github_scope",
        "oauth_state",
    ):
        session.pop(key, None)


def token_from_session(session: dict | None) -> str | None:
    if not session:
        return None
    token = (session.get("github_token") or "").strip()
    return token or None


def list_repos(token: str, *, limit: int = 40, query: str = "") -> list[dict[str, Any]]:
    """Repos repos visible to the OAuth user (owned + collaborator + org)."""
    if not token:
        raise RuntimeError("Not connected to GitHub.")
    q = (query or "").strip().lower()
    collected: list[dict[str, Any]] = []
    page = 1
    while len(collected) < max(limit * 2, 80) and page <= 3:
        batch = _github_get(
            token,
            f"/user/repos?per_page=50&page={page}&sort=updated&affiliation=owner,collaborator,organization_member",
        )
        if not isinstance(batch, list) or not batch:
            break
        for row in batch:
            full = str(row.get("full_name") or "")
            if q and q not in full.lower() and q not in str(row.get("description") or "").lower():
                continue
            collected.append(
                {
                    "full_name": full,
                    "private": bool(row.get("private")),
                    "description": (row.get("description") or "")[:160],
                    "html_url": row.get("html_url") or f"https://github.com/{full}",
                    "language": row.get("language"),
                    "updated_at": row.get("updated_at"),
                    "default_branch": row.get("default_branch") or "main",
                }
            )
        if len(batch) < 50:
            break
        page += 1
    return collected[:limit]


def list_pulls(token: str, owner: str, name: str, *, limit: int = 20) -> list[dict[str, Any]]:
    if not token:
        raise RuntimeError("Not connected to GitHub.")
    rows = _github_get(token, f"/repos/{owner}/{name}/pulls?state=open&per_page={max(1, min(limit, 50))}")
    if not isinstance(rows, list):
        return []
    pulls = []
    for row in rows:
        number = row.get("number")
        if not number:
            continue
        pulls.append(
            {
                "number": int(number),
                "title": str(row.get("title") or "")[:80],
                "html_url": row.get("html_url") or "",
                "draft": bool(row.get("draft")),
            }
        )
    return pulls


def _github_get(token: str, path: str) -> Any:
    request = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "syzygy-edl",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        from edl.github_pr import explain_github_error

        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(explain_github_error(exc.code, detail)) from exc
