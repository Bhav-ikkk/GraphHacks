"""Optional operational evidence declared beside a repository.

The tree scan never invents CI runs, deploys, or incidents. A repo may ship a
`syzygy.evidence.yaml` (or `.syzygy/evidence.yaml`) that states those observed
facts. Eval fixtures in edl/eval/scenarios.py stay separate and never load here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from edl.memory import MemoryGraph

OBSERVED_AT = "2026-10-15T12:00:00Z"
CANDIDATES = ("syzygy.evidence.yaml", "syzygy.evidence.yml", ".syzygy/evidence.yaml")


def load_overlay(root: Path | None) -> dict[str, Any]:
    if root is None or not root.exists():
        return {}
    for relative in CANDIDATES:
        path = root / relative
        if not path.is_file():
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            return {}
        data["_source_path"] = str(path.resolve())
        return data
    return {}


def apply_to_graph(graph: MemoryGraph, overlay: dict[str, Any]) -> list[str]:
    """Attach deploy / incident / tier facts. Returns short notes for the UI."""
    if not overlay:
        return []
    notes: list[str] = []
    for name, tier in (overlay.get("tiers") or {}).items():
        node = graph.node(f"svc:{name}")
        if node is None:
            continue
        try:
            node["tier"] = int(tier)
        except (TypeError, ValueError):
            continue
        notes.append(f"tier:{name}={node['tier']}")
    for row in overlay.get("deploys") or []:
        if not isinstance(row, dict):
            continue
        service = str(row.get("service") or "").strip()
        if not service or graph.node(f"svc:{service}") is None:
            continue
        deploy_id = f"deploy:{service}"
        graph.add_node(
            {
                "id": deploy_id,
                "label": "Deployment",
                "name": f"{service}-prod",
                "service": service,
                "health": row.get("health") or "unknown",
                "age_hours": row.get("age_hours"),
                "source": "syzygy.evidence",
                "evidence_type": "observed",
                "method": "evidence-overlay",
                "observed_at": OBSERVED_AT,
                "confidence": 1,
            }
        )
        graph.add_edge(
            deploy_id,
            "DEPLOYED_AS",
            f"svc:{service}",
            source="syzygy.evidence",
            evidence_type="observed",
        )
        notes.append(f"deploy:{service}")
    for row in overlay.get("incidents") or []:
        if not isinstance(row, dict):
            continue
        service = str(row.get("service") or "").strip()
        if not service or graph.node(f"svc:{service}") is None:
            continue
        slug = str(row.get("id") or row.get("name") or f"{service}-incident").strip()
        incident_id = slug if slug.startswith("inc:") else f"inc:{slug}"
        graph.add_node(
            {
                "id": incident_id,
                "label": "Incident",
                "name": str(row.get("name") or slug),
                "title": row.get("title") or row.get("name") or slug,
                "status": row.get("status") or "open",
                "age_days": row.get("age_days"),
                "service": service,
                "source": "syzygy.evidence",
                "evidence_type": "observed",
                "method": "evidence-overlay",
                "observed_at": OBSERVED_AT,
                "confidence": 1,
            }
        )
        graph.add_edge(
            incident_id,
            "AFFECTED",
            f"svc:{service}",
            source="syzygy.evidence",
            evidence_type="observed",
        )
        notes.append(f"incident:{incident_id}")
    for row in overlay.get("tests") or []:
        if not isinstance(row, dict):
            continue
        function = str(row.get("function") or "").strip()
        status = str(row.get("status") or "pass").strip()
        if not function:
            continue
        fid = function if function.startswith("fn:") else f"fn:{function}"
        if graph.node(fid) is None:
            continue
        tid = f"test:{function.removeprefix('fn:')}"
        existing = graph.node(tid)
        if existing is None:
            graph.add_node(
                {
                    "id": tid,
                    "label": "Test",
                    "name": tid,
                    "status": status,
                    "source": "syzygy.evidence",
                    "evidence_type": "observed",
                    "method": "evidence-overlay",
                    "observed_at": OBSERVED_AT,
                    "confidence": 1,
                }
            )
            graph.add_edge(tid, "COVERS", fid, source="syzygy.evidence", evidence_type="observed")
        else:
            existing["status"] = status
            existing["source"] = "syzygy.evidence"
        notes.append(f"test:{function}={status}")
    return notes


def apply_to_change(change: dict, overlay: dict[str, Any]) -> dict:
    """Merge CI / approval fields into a scoped change."""
    if not overlay:
        return change
    updated = dict(change)
    ci_overlay = overlay.get("ci")
    if isinstance(ci_overlay, dict):
        ci = dict(updated.get("ci") or {})
        for key in ("present", "result", "age_hours", "configured"):
            if key in ci_overlay:
                ci[key] = ci_overlay[key]
        updated["ci"] = ci
    approvals = overlay.get("approvals")
    if isinstance(approvals, dict):
        if "designated" in approvals:
            updated["designated_approval"] = bool(approvals["designated"])
        if approvals.get("approver"):
            updated["approver"] = approvals["approver"]
    return updated
