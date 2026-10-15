"""Action gate. The decision state is never changed here."""

from __future__ import annotations

ALLOWED = {"PROCEED", "PROCEED_WITH_GUARDS"}


def assess(record: dict, change: dict) -> dict:
    if not change.get("authorized", True):
        return {
            "status": "blocked",
            "reason": "Actor is not authorized to open an issue. The decision is unchanged.",
            "retries": 0,
        }
    if record["state"] not in ALLOWED:
        return {
            "status": "blocked",
            "reason": f"{record['state']} does not permit an action.",
            "retries": 0,
        }
    return {
        "status": "ready",
        "reason": "A designated click can open one issue for this change.",
        "retries": 0,
    }


def execute(record: dict, change: dict, issued: dict[str, dict]) -> dict:
    preview = assess(record, change)
    if preview["status"] != "ready":
        return preview
    existing = issued.get(change["id"])
    if existing:
        return {**existing, "idempotent": True}
    ticketing = change.get("ticketing", "ok")
    if ticketing == "timeout":
        return {"status": "failed", "reason": "Ticketing timed out. No retry.", "retries": 0}
    if ticketing == "401":
        return {"status": "failed", "reason": "Ticketing returned 401. No retry.", "retries": 0}
    retries = 1 if ticketing == "429" else 0
    issue = {
        "status": "created",
        "reason": "Issue recorded locally.",
        "retries": retries,
        "idempotent": False,
        "change_id": change["id"],
        "state": record["state"],
        "title": f"EDL {record['state']}: {change.get('title', change['id'])}",
    }
    issued[change["id"]] = issue
    return issue
