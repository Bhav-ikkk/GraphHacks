"""Owner notification drafts (and optional SMTP send) from a decision record.

Never invents recipients. Addresses come from Team nodes / CODEOWNERS-derived
owners already on the graph, or SYZYGY_NOTIFY_TO for an explicit override.
"""

from __future__ import annotations

import os
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DRAFT_DIR = ROOT / "logs" / "notifications"


def notify_owners(engine, record: dict, *, send: bool = False) -> dict[str, Any]:
    """Write a markdown draft; optionally SMTP-send when configured."""
    recipients = _recipients(engine, record)
    subject, body = _compose(engine, record, recipients)
    DRAFT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    draft_path = DRAFT_DIR / f"notify-{record.get('change_id', 'change').replace(':', '_')}-{stamp}.md"
    draft_path.write_text(
        f"# {subject}\n\nTo: {', '.join(recipients) or '(none)'}\n\n{body}\n",
        encoding="utf-8",
    )
    result: dict[str, Any] = {
        "status": "drafted",
        "to": recipients,
        "subject": subject,
        "draft_path": str(draft_path),
        "detail": "Notification drafted locally. No email sent.",
    }
    if not send:
        return result
    if not recipients:
        result["status"] = "blocked"
        result["detail"] = "No owner emails on the graph and SYZYGY_NOTIFY_TO is unset."
        return result
    smtp = _smtp_settings()
    if not smtp:
        result["status"] = "drafted"
        result["detail"] = (
            "Draft saved. Set SYZYGY_SMTP_HOST / SYZYGY_SMTP_USER / SYZYGY_SMTP_PASSWORD "
            "to send, or keep --send off for mock drafts."
        )
        return result
    try:
        _smtp_send(smtp, recipients, subject, body)
    except OSError as exc:
        result["status"] = "failed"
        result["detail"] = f"SMTP failed: {exc}"
        return result
    result["status"] = "sent"
    result["detail"] = f"Sent via {smtp['host']} to {len(recipients)} recipient(s)."
    return result


def _recipients(engine, record: dict) -> list[str]:
    override = os.environ.get("SYZYGY_NOTIFY_TO", "").strip()
    if override:
        return [part.strip() for part in override.split(",") if part.strip()]
    emails: list[str] = []
    for service in engine.services():
        owner = service.get("owner")
        if not owner:
            continue
        # CODEOWNERS often uses @org/team — map to mailto only when it looks like an email
        if "@" in owner and " " not in owner and not owner.startswith("@"):
            if owner not in emails:
                emails.append(owner)
    # Team nodes may carry email props from evidence overlays
    for node in engine.graph.by_label("Team"):
        email = node.get("email")
        if email and email not in emails:
            emails.append(str(email))
    return emails


def _compose(engine, record: dict, recipients: list[str]) -> tuple[str, str]:
    snap = engine.snapshot()
    origin = (snap.get("origin") or {}).get("value") or "repository"
    state = record.get("state") or "UNKNOWN"
    subject = f"[Syzygy EDL] {state}: {record.get('change_id')}"
    lines = [
        f"Syzygy EDL decision for {origin}",
        f"State: {state}",
        f"Change: {record.get('change_id')}",
        f"Backend: {record.get('graph_backend')}",
        f"Policy: {record.get('policy_version')}",
        "",
        "Rationale:",
        str(record.get("rationale") or ""),
        "",
        "Triggered:",
        ", ".join(record.get("triggered") or []) or "none",
        "",
        "Claims:",
    ]
    for claim in record.get("claims") or []:
        if isinstance(claim, dict):
            lines.append(f"- [{claim.get('evidence_id')}] {claim.get('text')}")
    lines.append("")
    lines.append("Next steps:")
    from edl.cli_report import next_steps_lines

    lines.extend(next_steps_lines(record, record.get("action") or {}, snap.get("origin") or {}))
    if recipients:
        lines.append("")
        lines.append("Intended recipients: " + ", ".join(recipients))
    else:
        lines.append("")
        lines.append(
            "No email addresses on owner teams. Set SYZYGY_NOTIFY_TO=you@example.com for demos."
        )
    return subject, "\n".join(lines)


def _smtp_settings() -> dict | None:
    host = os.environ.get("SYZYGY_SMTP_HOST", "").strip()
    if not host:
        return None
    return {
        "host": host,
        "port": int(os.environ.get("SYZYGY_SMTP_PORT", "587") or "587"),
        "user": os.environ.get("SYZYGY_SMTP_USER", "").strip(),
        "password": os.environ.get("SYZYGY_SMTP_PASSWORD", ""),
        "from_addr": os.environ.get("SYZYGY_SMTP_FROM", "").strip()
        or os.environ.get("SYZYGY_SMTP_USER", "").strip(),
        "use_tls": os.environ.get("SYZYGY_SMTP_TLS", "1").strip() not in {"0", "false", "no"},
    }


def _smtp_send(smtp: dict, recipients: list[str], subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp["from_addr"] or smtp["user"]
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)
    with smtplib.SMTP(smtp["host"], smtp["port"], timeout=30) as client:
        if smtp["use_tls"]:
            client.starttls()
        if smtp["user"]:
            client.login(smtp["user"], smtp["password"])
        client.send_message(msg)
