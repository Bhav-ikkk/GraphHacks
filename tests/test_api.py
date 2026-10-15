from pathlib import Path

import pytest
from fastapi import HTTPException

from edl.api import IngestIn, engine, health, ingest_repo

ROOT = Path(__file__).resolve().parents[1]
MINI = ROOT / "examples" / "mini-repo"


def test_ui_starts_empty_and_ingest_uses_the_chosen_folder():
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert "Use ShopStack" not in page
    assert "Analyze" in page
    assert health()["dataset"] == "empty"

    body = ingest_repo(IngestIn(target=str(MINI)))
    assert body["dataset"] == "repository"
    names = {service["name"] for service in body["services"]}
    assert names == {"checkout", "payments", "search"}
    payments = next(service for service in body["services"] if service["name"] == "payments")
    assert payments["owner"] == "acme/payments"
    assert payments["open_incident"] is None

    change_id = next(item["id"] for item in body["snapshot"]["changes"] if item["id"].startswith("repo:payments"))
    reviewed = engine.review(change_id, "full")
    assert reviewed["runs"]["full"]["state"] == "INSUFFICIENT_EVIDENCE"

    with pytest.raises(HTTPException) as caught:
        ingest_repo(IngestIn(target="MATCH (n) RETURN n"))
    assert caught.value.status_code == 400
