"""Evidence and decision contracts.

Every material claim must cite an evidence id that exists in the assembled
catalog for that review. An explanation may not invent an id.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from edl.models import NOW, STATES

EVIDENCE_TYPES = ("observed", "inferred", "synthetic")
EVIDENCE_ROLES = ("supports", "contradicts", "missing", "context")

REQUIRED_EVIDENCE_FIELDS = (
    "id",
    "source_system",
    "evidence_type",
    "role",
    "detail",
)

REQUIRED_DECISION_FIELDS = (
    "id",
    "change_id",
    "mode",
    "state",
    "rationale",
    "claims",
    "rejected_claims",
    "findings",
    "evidence",
    "missing",
    "policy_version",
    "graph_revision",
)


def make_evidence(
    evidence_id: str,
    *,
    source_system: str,
    detail: str,
    role: str = "supports",
    evidence_type: str = "observed",
    source_ref: str | None = None,
    entity_id: str | None = None,
    version_ref: str | None = None,
    observed_at: str | None = None,
    freshness_ok: bool | None = None,
    limitations: str | None = None,
    policy: str | None = None,
) -> dict[str, Any]:
    if evidence_type not in EVIDENCE_TYPES:
        raise ValueError(f"evidence_type must be one of {EVIDENCE_TYPES}")
    if role not in EVIDENCE_ROLES:
        raise ValueError(f"role must be one of {EVIDENCE_ROLES}")
    return {
        "id": evidence_id,
        "source_system": source_system,
        "source_ref": source_ref,
        "entity_id": entity_id,
        "version_ref": version_ref,
        "observed_at": observed_at,
        "freshness_ok": freshness_ok,
        "evidence_type": evidence_type,
        "role": role,
        "detail": detail,
        "limitations": limitations,
        "policy": policy,
    }


def catalog_ids(records: list[dict]) -> set[str]:
    return {record["id"] for record in records if record.get("id")}


def reject_claim(claims: list[dict], catalog: set[str]) -> tuple[list[dict], list[dict]]:
    """Keep only claims whose evidence_id is in the assembled catalog."""
    kept: list[dict] = []
    rejected: list[dict] = []
    for claim in claims:
        if claim.get("evidence_id") in catalog:
            kept.append(claim)
        else:
            rejected.append(claim)
    return kept, rejected


def validate_evidence_record(record: dict) -> list[str]:
    errors = [field for field in REQUIRED_EVIDENCE_FIELDS if field not in record]
    if record.get("evidence_type") not in EVIDENCE_TYPES:
        errors.append("evidence_type")
    if record.get("role") not in EVIDENCE_ROLES:
        errors.append("role")
    return errors


def validate_decision_record(record: dict) -> list[str]:
    errors = [field for field in REQUIRED_DECISION_FIELDS if field not in record]
    if record.get("state") not in STATES:
        errors.append("state")
    evidence = record.get("evidence") or []
    catalog = catalog_ids(evidence)
    for claim in record.get("claims") or []:
        if claim.get("evidence_id") not in catalog:
            errors.append(f"claim:{claim.get('evidence_id')}")
    for item in evidence:
        errors.extend(f"evidence:{item.get('id')}:{err}" for err in validate_evidence_record(item))
    return errors


def assemble_evidence(
    bundle: dict,
    decision: dict,
    *,
    now: datetime = NOW,
    commit: str | None = None,
) -> list[dict]:
    """Build the evidence catalog for one review from tool results and findings."""
    observed_at = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    by_id: dict[str, dict] = {}

    def put(record: dict) -> None:
        by_id[record["id"]] = record

    ci = bundle.get("ci") or {}
    if not ci.get("present"):
        detail = (
            "CI workflow file is present; latest run result is not in the repository."
            if ci.get("configured")
            else "CI result is missing."
        )
        put(
            make_evidence(
                "ev-ci",
                source_system="repository",
                source_ref=".github/workflows" if ci.get("configured") else None,
                version_ref=commit,
                observed_at=observed_at,
                freshness_ok=False,
                role="missing",
                evidence_type="observed",
                detail=detail,
                limitations="A workflow file is not a run result.",
                policy="P5",
            )
        )
    else:
        age = ci.get("age_hours")
        put(
            make_evidence(
                "ev-ci",
                source_system="ci",
                source_ref="check-run",
                version_ref=commit,
                observed_at=observed_at,
                freshness_ok=bool(age is not None and ci.get("result") == "pass"),
                role="supports" if ci.get("result") == "pass" else "contradicts",
                evidence_type="observed",
                detail=f"CI result={ci.get('result')} age_hours={age}",
                policy="P5",
            )
        )

    for owner in bundle.get("owners") or []:
        service = owner.get("service") or "unknown"
        eid = f"ev-owner-{service}"
        if owner.get("missing"):
            put(
                make_evidence(
                    eid,
                    source_system="codeowners",
                    entity_id=f"svc:{service}",
                    observed_at=observed_at,
                    freshness_ok=False,
                    role="missing",
                    evidence_type="observed",
                    detail=f"{service} has no owner record.",
                    policy="P5",
                )
            )
        else:
            put(
                make_evidence(
                    eid,
                    source_system="codeowners",
                    entity_id=owner.get("team_id") or f"svc:{service}",
                    source_ref=owner.get("team"),
                    observed_at=observed_at,
                    freshness_ok=not owner.get("stale"),
                    role="missing" if owner.get("stale") else "supports",
                    evidence_type="observed",
                    detail=(
                        f"Owner for {service} is stale."
                        if owner.get("stale")
                        else f"Owner for {service} is {owner.get('team')}."
                    ),
                    policy="P5",
                )
            )

    for deploy in bundle.get("deploys") or []:
        service = deploy.get("service") or "unknown"
        put(
            make_evidence(
                f"ev-deploy-{service}",
                source_system="deploy",
                entity_id=f"svc:{service}",
                observed_at=observed_at,
                freshness_ok=bool(deploy.get("present")) and deploy.get("health") == "ok",
                role="missing" if not deploy.get("present") else "supports",
                evidence_type="observed",
                detail=(
                    f"No deployment record for {service}."
                    if not deploy.get("present")
                    else f"Deployment of {service}: age={deploy.get('age_hours')}h health={deploy.get('health')}."
                ),
                policy="P5",
            )
        )
        if deploy.get("health") not in (None, "ok") and deploy.get("tier") == 0:
            put(
                make_evidence(
                    f"ev-health-{service}",
                    source_system="deploy",
                    entity_id=f"svc:{service}",
                    observed_at=observed_at,
                    freshness_ok=False,
                    role="contradicts",
                    evidence_type="observed",
                    detail=f"Production health for {service} is {deploy.get('health')}.",
                    policy="R3",
                )
            )

    for item in bundle.get("tests") or []:
        put(
            make_evidence(
                f"ev-test-{item['function']}",
                source_system="repository",
                entity_id=f"fn:{item['function']}",
                observed_at=observed_at,
                freshness_ok=item.get("status") == "pass",
                role="supports" if item.get("status") == "pass" else "missing",
                evidence_type="observed",
                detail=f"{item['function']} test status is {item.get('status')}.",
                limitations="A test file is not a passing run unless status is pass.",
                policy="G-TEST",
            )
        )

    for incident in bundle.get("incidents_open") or []:
        put(
            make_evidence(
                f"ev-inc-{incident['name']}",
                source_system="incident",
                entity_id=f"inc:{incident['name']}",
                observed_at=observed_at,
                freshness_ok=True,
                role="contradicts",
                evidence_type="observed",
                detail=f"Open incident {incident['name']} on {incident['service']}.",
                policy="P4",
            )
        )

    if bundle.get("incidents_prior"):
        prior = bundle["incidents_prior"][0]
        put(
            make_evidence(
                "ev-prior",
                source_system="incident",
                entity_id=f"fix:{prior['fix']}",
                observed_at=observed_at,
                role="supports",
                evidence_type="observed",
                detail=f"Resolved incident {prior['incident']} fixed by {prior['fix']}.",
                policy="P7",
            )
        )

    mapping = bundle.get("mapping", "resolved")
    if mapping != "resolved":
        put(
            make_evidence(
                "ev-mapping",
                source_system="ingest",
                observed_at=observed_at,
                role="missing",
                evidence_type="inferred" if mapping == "inferred" else "observed",
                detail=f"Symbol-to-service mapping is {mapping}.",
                limitations="Inferred or missing links are not proof.",
                policy="P6",
            )
        )

    if bundle.get("unresolved_refs"):
        put(
            make_evidence(
                "ev-refs",
                source_system="change",
                observed_at=observed_at,
                role="missing",
                evidence_type="observed",
                detail="Diff references could not be resolved without graph direction.",
                policy="P6",
            )
        )

    if bundle.get("shared_name_in_diff"):
        put(
            make_evidence(
                "ev-shared-off",
                source_system="change",
                observed_at=observed_at,
                role="missing",
                evidence_type="observed",
                detail="Shared library change without consumer blast radius.",
                policy="P6",
            )
        )

    if bundle.get("intent") == "rollback":
        fix = bundle.get("rollback_fix")
        if fix:
            put(
                make_evidence(
                    "ev-fix",
                    source_system="graph",
                    entity_id=f"fix:{fix['name']}",
                    observed_at=observed_at,
                    role="supports",
                    evidence_type="observed",
                    detail=f"Rollback cites fix {fix['name']} on {fix['function']}.",
                    policy="R4",
                )
            )
        else:
            put(
                make_evidence(
                    "ev-rollback",
                    source_system="change",
                    observed_at=observed_at,
                    role="missing",
                    evidence_type="observed",
                    detail="Rollback intent without an earlier fix in evidence.",
                    policy="R4",
                )
            )

    if any(item.get("callers", 0) for item in bundle.get("functions") or []):
        hot = max(bundle["functions"], key=lambda item: item.get("callers", 0))
        put(
            make_evidence(
                "ev-hub",
                source_system="graph",
                entity_id=hot.get("id"),
                observed_at=observed_at,
                role="supports",
                evidence_type="observed",
                detail=f"{hot.get('name')} has {hot.get('callers')} callers.",
                policy="R2",
            )
        )

    if bundle.get("hop_limit_hit"):
        put(
            make_evidence(
                "ev-hop",
                source_system="graph",
                observed_at=observed_at,
                role="missing",
                evidence_type="observed",
                detail="Traversal stopped at the hop limit.",
                limitations="Dependents past the limit were not used as evidence.",
                policy="G-HOP",
            )
        )

    dependency = bundle.get("dependency")
    if dependency and dependency.get("major"):
        put(
            make_evidence(
                "ev-dep",
                source_system="change",
                observed_at=observed_at,
                role="missing" if not dependency.get("checklist") else "supports",
                evidence_type="observed",
                detail=f"Major bump of {dependency.get('name')}.",
                policy="P8",
            )
        )

    if bundle.get("critical_consumer_tests"):
        put(
            make_evidence(
                "ev-p2",
                source_system="graph",
                observed_at=observed_at,
                role="missing",
                evidence_type="observed",
                detail="Critical consumer test coverage checked.",
                policy="P2",
            )
        )

    if bundle.get("text_mentions_incident"):
        put(
            make_evidence(
                "ev-mention",
                source_system="change",
                observed_at=observed_at,
                role="context",
                evidence_type="observed",
                detail="PR text mentions an incident.",
                limitations="Text mention is not a graph incident fact.",
                policy="P7",
            )
        )

    pay_auth = [
        item["service"]
        for item in bundle.get("affected") or []
        if item.get("service")
    ]
    if pay_auth:
        put(
            make_evidence(
                "ev-p3",
                source_system="graph",
                observed_at=observed_at,
                role="context",
                evidence_type="observed",
                detail="Payment or auth path check against designated approval.",
                policy="P3",
            )
        )

    if any(item.get("tier") == 0 for item in bundle.get("affected") or []):
        put(
            make_evidence(
                "ev-tier0",
                source_system="graph",
                observed_at=observed_at,
                role="context",
                evidence_type="observed",
                detail="Tier-0 service present in blast radius.",
                policy="P1",
            )
        )

    teams = sorted(
        {
            owner["team"]
            for owner in bundle.get("direct_owners") or []
            if owner.get("team") and not owner.get("missing") and not owner.get("stale")
        }
    )
    if len(teams) >= 2:
        put(
            make_evidence(
                "ev-route",
                source_system="codeowners",
                observed_at=observed_at,
                role="supports",
                evidence_type="observed",
                detail="Change spans owners " + " and ".join(teams) + ".",
                policy="R1",
            )
        )

    if bundle.get("unrelated"):
        put(
            make_evidence(
                "ev-unrelated",
                source_system="graph",
                observed_at=observed_at,
                role="context",
                evidence_type="observed",
                detail="Incident(s) outside blast radius were not used: "
                + ", ".join(bundle["unrelated"]),
            )
        )

    if bundle.get("approval", {}).get("designated"):
        put(
            make_evidence(
                "ev-approval",
                source_system="change",
                observed_at=observed_at,
                role="supports",
                evidence_type="observed",
                detail=f"Designated approval from {bundle['approval'].get('approver') or 'a reviewer'}.",
            )
        )

    affected_names = {item["service"] for item in bundle.get("affected") or []}
    for ref in bundle.get("unresolved_refs") or []:
        service = ref.split(".")[0]
        if bundle.get("mode") == "full" and service not in affected_names:
            put(
                make_evidence(
                    "ev-direction",
                    source_system="graph",
                    observed_at=observed_at,
                    role="context",
                    evidence_type="observed",
                    detail=f"{ref} is outside the blast radius; dependency points outward.",
                )
            )
            break

    # Every finding must have a backing record, even if assembly missed a branch.
    for finding in decision.get("findings") or []:
        eid = finding["evidence_id"]
        if eid in by_id:
            by_id[eid]["policy"] = finding.get("policy") or by_id[eid].get("policy")
            continue
        severity = finding.get("severity")
        role = "missing" if severity == "insufficient" else "supports"
        if severity == "hold":
            role = "contradicts"
        put(
            make_evidence(
                eid,
                source_system="policy",
                observed_at=observed_at,
                role=role,
                evidence_type="synthetic",
                detail=finding.get("detail") or eid,
                policy=finding.get("policy"),
                limitations="Registered from finding because no tool record existed.",
            )
        )

    if not decision.get("findings"):
        put(
            make_evidence(
                "ev-clear",
                source_system="policy",
                observed_at=observed_at,
                role="supports",
                evidence_type="synthetic",
                detail="No hold, review, or guard rule triggered on assembled evidence.",
            )
        )

    return [by_id[key] for key in sorted(by_id)]
