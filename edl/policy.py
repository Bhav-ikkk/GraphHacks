"""Deterministic policy evaluator. No model calls. Precedence is fixed."""

from __future__ import annotations

from pathlib import Path

import yaml

from edl.models import PRECEDENCE, STATE_FOR, is_payment_or_auth


class PolicyError(Exception):
    pass


def load_policy(path: Path) -> dict:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise PolicyError("policy file is not a mapping")
    for key in ("version", "freshness", "hop_limit", "incident_window_days", "hub_caller_min"):
        if key not in raw:
            raise PolicyError(f"policy file is missing {key}")
    freshness = raw["freshness"]
    if not isinstance(freshness, dict) or any(
        item not in freshness for item in ("ci_hours", "deploy_hours", "owner_days")
    ):
        raise PolicyError("freshness block is malformed")
    return raw


def decide(evidence: dict, policy: dict) -> dict:
    findings: list[dict] = []
    mode = evidence["mode"]

    if evidence["mapping"] != "resolved":
        findings.append(
            _finding(
                "insufficient",
                "P6",
                "Symbol-to-service mapping is "
                f"{evidence['mapping']}. Inferred or missing links are not proof.",
                "ev-mapping",
            )
        )

    if mode == "graph_off" and evidence.get("unresolved_refs"):
        refs = ", ".join(evidence["unresolved_refs"])
        findings.append(
            _finding(
                "insufficient",
                "P6",
                f"The diff references {refs}, and without the graph the direction of that link is unknown.",
                "ev-refs",
            )
        )

    if mode == "graph_off" and evidence.get("shared_name_in_diff"):
        findings.append(
            _finding(
                "insufficient",
                "P6",
                "This diff touches a shared library. Consumer blast radius was not resolved.",
                "ev-shared-off",
            )
        )

    if evidence.get("intent") == "rollback":
        fix = evidence.get("rollback_fix")
        if mode != "full" or not fix:
            findings.append(
                _finding(
                    "insufficient",
                    "R4",
                    "The change is a rollback, and the earlier fix is not in the evidence used for this mode.",
                    "ev-rollback",
                )
            )
        else:
            findings.append(
                _finding(
                    "review",
                    "R4",
                    f"Rollback of {fix['name']} on {fix['function']}.",
                    "ev-fix",
                )
            )

    ci = evidence["ci"]
    if not ci.get("present"):
        if ci.get("configured"):
            detail = "A CI workflow file is in the repository. The latest run result is not, so freshness cannot be checked."
        else:
            detail = "CI result is missing. No workflow file and no run result were found."
        findings.append(_finding("insufficient", "P5", detail, "ev-ci"))
    elif ci.get("age_hours") is not None and ci["age_hours"] > policy["freshness"]["ci_hours"]:
        findings.append(
            _finding(
                "insufficient",
                "P5",
                f"CI result is {ci['age_hours']}h old. The limit is {policy['freshness']['ci_hours']}h.",
                "ev-ci",
            )
        )

    if evidence.get("owners_checked"):
        for owner in evidence["owners"]:
            if owner["missing"]:
                findings.append(
                    _finding(
                        "insufficient",
                        "P5",
                        f"{owner['service']} has no owner record. No reviewer was assigned.",
                        f"ev-owner-{owner['service']}",
                    )
                )
            elif owner["stale"]:
                findings.append(
                    _finding(
                        "insufficient",
                        "P5",
                        f"Owner record for {owner['service']} is stale. No reviewer was assigned.",
                        f"ev-owner-{owner['service']}",
                    )
                )

    if evidence.get("deploys_checked"):
        limit = policy["freshness"]["deploy_hours"]
        for deploy in evidence["deploys"]:
            if not deploy["present"]:
                findings.append(
                    _finding(
                        "insufficient",
                        "P5",
                        f"No deployment record for {deploy['service']}.",
                        f"ev-deploy-{deploy['service']}",
                    )
                )
            elif deploy["age_hours"] is not None and deploy["age_hours"] > limit:
                findings.append(
                    _finding(
                        "insufficient",
                        "P5",
                        f"Deployment of {deploy['service']} is {deploy['age_hours']}h old. Limit is {limit}h.",
                        f"ev-deploy-{deploy['service']}",
                    )
                )
            if deploy.get("health") not in (None, "ok") and deploy.get("tier") == 0:
                conflict = evidence["ci"].get("result") == "pass"
                detail = f"Production health for {deploy['service']} is {deploy['health']}."
                if conflict:
                    detail += " CI is green, so the two signals conflict. Production health wins."
                findings.append(_finding("hold", "R3", detail, f"ev-health-{deploy['service']}"))

    if evidence.get("incidents_checked"):
        window = policy["incident_window_days"]
        affected_ids = {item["service"] for item in evidence["affected"]}
        for incident in evidence["incidents_open"]:
            if (
                incident["service"] in affected_ids
                and incident["tier"] == 0
                and incident["age_days"] <= window
            ):
                findings.append(
                    _finding(
                        "hold",
                        "P4",
                        f"Open incident {incident['name']} on {incident['service']} is {incident['age_days']} days old.",
                        f"ev-inc-{incident['name']}",
                    )
                )

    tier0 = [
        item
        for item in evidence["affected"]
        if evidence.get("tiers_known") and item.get("tier") == 0 and item["hops"] <= policy["hop_limit"]
    ]
    if tier0 and not evidence["approval"]["designated"]:
        names = ", ".join(item["service"] for item in tier0)
        findings.append(
            _finding(
                "review",
                "P1",
                f"Tier-0 service(s) inside {policy['hop_limit']} hops: {names}. No designated approval.",
                "ev-tier0",
            )
        )

    pay_auth = [
        item["service"]
        for item in evidence["affected"]
        if is_payment_or_auth(item["service"]) and item["hops"] <= policy["hop_limit"]
    ]
    if pay_auth and not evidence["approval"]["designated"]:
        failing = evidence["ci"].get("result") == "fail" or any(
            item["status"] == "fail" for item in evidence.get("tests", [])
        )
        names = ", ".join(pay_auth)
        if failing:
            findings.append(
                _finding(
                    "hold",
                    "P3",
                    f"Payment or auth path ({names}) has no designated approval, and a check is failing.",
                    "ev-p3",
                )
            )
        else:
            findings.append(
                _finding(
                    "review",
                    "P3",
                    f"Payment or auth path ({names}) has no designated approval.",
                    "ev-p3",
                )
            )

    if evidence.get("history_checked") and evidence.get("incidents_prior"):
        prior = evidence["incidents_prior"][0]
        findings.append(
            _finding(
                "review",
                "P7",
                f"Resolved incident {prior['incident']} was fixed in {prior['fix']} on this function.",
                "ev-prior",
            )
        )

    if mode == "full" and any(item["callers"] >= policy["hub_caller_min"] for item in evidence["functions"]):
        hot = [item for item in evidence["functions"] if item["callers"] >= policy["hub_caller_min"]]
        findings.append(
            _finding(
                "review",
                "R2",
                f"{hot[0]['name']} has {hot[0]['callers']} callers.",
                "ev-hub",
            )
        )

    if evidence.get("owners_checked"):
        teams = sorted(
            {
                owner["team"]
                for owner in evidence["direct_owners"]
                if owner.get("team") and not owner.get("missing") and not owner.get("stale")
            }
        )
        if len(teams) >= 2:
            findings.append(
                _finding(
                    "review",
                    "R1",
                    "Change spans owners " + " and ".join(teams) + ". Route it to both.",
                    "ev-route",
                )
            )

    if mode == "full" and evidence.get("hop_limit_hit"):
        findings.append(
            _finding(
                "guard",
                "G-HOP",
                f"Traversal stopped at {policy['hop_limit']} hops. At least one further dependent was not used as evidence.",
                "ev-hop",
            )
        )

    dependency = evidence.get("dependency")
    if dependency and dependency.get("major") and not dependency.get("checklist"):
        findings.append(
            _finding(
                "guard",
                "P8",
                f"Major bump of {dependency['name']} has no approved migration checklist.",
                "ev-dep",
            )
        )

    if evidence.get("consumer_tests_checked"):
        missing = [item for item in evidence["critical_consumer_tests"] if item["status"] != "pass"]
        if missing:
            names = ", ".join(item["function"] for item in missing)
            findings.append(
                _finding(
                    "guard",
                    "P2",
                    f"Critical consumers without a passing test: {names}.",
                    "ev-p2",
                )
            )

    if evidence.get("tests_checked"):
        for item in evidence["tests"]:
            if item["changed"] and item["status"] != "pass":
                findings.append(
                    _finding(
                        "guard",
                        "G-TEST",
                        f"{item['function']} has no passing test on this branch ({item['status']}).",
                        f"ev-test-{item['function']}",
                    )
                )

    if mode == "graph_off" and evidence.get("text_mentions_incident"):
        findings.append(
            _finding(
                "review",
                "P7",
                "The PR text mentions an incident. Without the graph it cannot be dismissed as a different service.",
                "ev-mention",
            )
        )

    state = _state_for(findings)
    triggered = [item["policy"] for item in findings]
    missing = [
        {"policy": item["policy"], "detail": item["detail"], "evidence_id": item["evidence_id"]}
        for item in findings
        if item["severity"] == "insufficient"
    ]
    return {
        "state": state,
        "findings": findings,
        "triggered": triggered,
        "missing": missing,
        "policy_version": policy["version"],
    }


def _state_for(findings: list[dict]) -> str:
    severities = {item["severity"] for item in findings}
    for level in PRECEDENCE:
        if level in severities:
            return STATE_FOR[level]
    return "PROCEED"


def _finding(severity: str, policy: str, detail: str, evidence_id: str) -> dict:
    return {
        "severity": severity,
        "policy": policy,
        "detail": detail,
        "evidence_id": evidence_id,
    }
