"""Judge, evaluate, verify. The state comes from policy. Text only explains it."""

from __future__ import annotations

from datetime import datetime, timezone

from edl.evidence import assemble_evidence, catalog_ids, reject_claim, validate_decision_record
from edl.graph_reader import GraphReader, MemoryReader
from edl.memory import MemoryGraph
from edl.models import NOW
from edl.policy import decide

INJECTION_MARKERS = (
    "ignore policy",
    "ignore previous",
    "approve this",
    "disregard the policy",
    "disregard policy",
)


def review_change(
    graph: MemoryGraph,
    change: dict,
    policy: dict,
    mode: str = "full",
    *,
    now: datetime = NOW,
    writeback: bool = False,
    reader: GraphReader | None = None,
) -> dict:
    working = graph.copy()
    active = reader or MemoryReader(working)
    trace: list[dict] = []
    injection = _injection(change.get("description", ""))
    evidence = _evaluate(active, change, policy, mode, trace, now)
    decision = decide(evidence, policy)
    commit = change.get("commit", f"sha-{change['id'].lower()}")
    evidence_records = assemble_evidence(evidence, decision, now=now, commit=commit)
    catalog = catalog_ids(evidence_records)
    claims = _claims(decision, evidence)
    claims, rejected = reject_claim(claims, catalog)
    record_id = f"dec:{change['id']}:{mode}:{_stamp()}"
    record = {
        "id": record_id,
        "change_id": change["id"],
        "title": change.get("title", change["id"]),
        "commit": commit,
        "mode": mode,
        "state": decision["state"],
        "rationale": _rationale(decision),
        "claims": claims,
        "rejected_claims": rejected,
        "findings": decision["findings"],
        "triggered": decision["triggered"],
        "evidence": evidence_records,
        "missing": decision["missing"],
        "policy_version": decision["policy_version"],
        "graph_revision": working.revision(),
        "graph_backend": getattr(active, "backend", "unknown"),
        "tool_calls": trace,
        "injection": injection,
        "graph": _view(evidence, decision),
        "approval": evidence["approval"],
        "expected": change.get("expected"),
        "freshness": evidence["freshness"],
        "routes": evidence.get("routes", []),
        "note": change.get("note"),
    }
    contract_errors = validate_decision_record(record)
    record["contract_ok"] = not contract_errors
    record["contract_errors"] = contract_errors
    if writeback:
        _write_back(working, record)
        record["written_back"] = True
    else:
        record["written_back"] = False
    record["_graph"] = working
    return record


def public_record(record: dict) -> dict:
    return {key: value for key, value in record.items() if key != "_graph"}


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%H%M%S%f")


def _injection(description: str) -> list[str]:
    text = description.lower()
    return [marker for marker in INJECTION_MARKERS if marker in text]


def _call(trace: list[dict], tool: str, args: dict, result) -> None:
    summary = _summarize(result)
    trace.append(
        {
            "tool": tool,
            "args": args,
            "result": result,
            "summary": summary,
            "ok": True,
            "at": datetime.now(timezone.utc).isoformat(),
        }
    )


def _summarize(result) -> str:
    if isinstance(result, dict) and result.get("skipped"):
        return result.get("reason") or "empty (graph off)"
    if isinstance(result, list):
        return f"{len(result)} row(s)"
    if isinstance(result, dict):
        parts = []
        for key, value in result.items():
            if isinstance(value, list):
                names = [str(item.get("name")) for item in value if isinstance(item, dict) and item.get("name")]
                parts.append(f"{key}: {', '.join(names[:3])}" if names else f"{key}: {len(value)}")
            else:
                parts.append(f"{key}={value}")
        return ", ".join(parts[:6])
    return str(result)


def _evaluate(reader: GraphReader, change, policy, mode, trace, now) -> dict:
    hop_limit = 1 if mode == "one_hop" else policy["hop_limit"]
    graph_on = mode != "graph_off"
    mapping = change.get("mapping", "resolved")
    functions = []
    # Always resolve declared symbols. mapping (resolved/partial/unresolved) is about
    # whether every modified path mapped; it must not hide known symbols.
    for symbol in change.get("symbols", []):
        node = reader.node(f"fn:{symbol}")
        if node:
            functions.append(node)
    claimed = _services_from_paths(change.get("files", []))
    shared_name = any("utils" in name or name.startswith("shared") for name in claimed) or any(
        "/shared/" in path for path in change.get("files", [])
    )

    if not graph_on:
        _call(trace, "impact_analysis", {"symbols": change.get("symbols", []), "hops": 0}, {"skipped": True, "consumers": []})
        _call(trace, "nearest_owner", {"services": claimed}, {"skipped": True, "owners": []})
        _call(trace, "find_similar_incidents", {"symbols": change.get("symbols", [])}, {"skipped": True, "incidents": []})
        _call(trace, "get_test_coverage", {"symbols": change.get("symbols", [])}, {"skipped": True, "tests": []})
        _call(trace, "get_deploy_state", {"services": claimed}, {"skipped": True, "deploys": []})
        _call(trace, "centrality_rank", {"symbols": change.get("symbols", [])}, {"skipped": True, "scores": []})
        affected = [{"service": name, "tier": None, "hops": 0, "kind": "direct"} for name in claimed]
        return _bundle(
            change,
            mode,
            functions=[
                {"id": f"fn:{symbol}", "name": symbol, "service": symbol.split(".")[0], "callers": 0}
                for symbol in change.get("symbols", [])
            ],
            affected=affected,
            outgoing=[],
            owners=[],
            direct_owners=[],
            incidents_open=[],
            incidents_prior=[],
            tests=[],
            deploys=[],
            critical_consumer_tests=[],
            owners_checked=False,
            deploys_checked=False,
            incidents_checked=False,
            history_checked=False,
            tests_checked=False,
            consumer_tests_checked=False,
            tiers_known=False,
            hop_limit_hit=False,
            shared_name_in_diff=shared_name,
            rollback_fix=None,
            routes=[],
            freshness=[{"item": "CI", "detail": _ci_text(change), "ok": _ci_ok(change, policy)}],
            unrelated=[],
        )

    resolved = []
    for node in functions:
        service = reader.function_service(node["id"])
        caller_ids = reader.callers(node["id"])
        resolved.append(
            {
                "id": node["id"],
                "name": node["name"],
                "service": None if service is None else service["name"],
                "service_id": None if service is None else service["id"],
                "tier": None if service is None else service.get("tier"),
                "kind": None if service is None else service.get("kind"),
                "callers": len(caller_ids),
            }
        )
    _call(
        trace,
        "centrality_rank",
        {"functions": [item["name"] for item in resolved]},
        [{"name": item["name"], "callers": item["callers"]} for item in resolved] if mode == "full" else {"skipped": True, "reason": "centrality is a full-graph algorithm"},
    )
    if mode != "full":
        for item in resolved:
            item["callers"] = 0

    # Every service touched by the change is direct. Consumers are expanded after,
    # so a dependency between two changed services cannot demote either to hops>0.
    affected = []
    seen_services = set()
    hop_limit_hit = False
    direct_ids: list[str] = []
    for item in resolved:
        if not item["service"] or item["service"] in seen_services:
            continue
        seen_services.add(item["service"])
        affected.append(
            {"service": item["service"], "tier": item["tier"], "hops": 0, "kind": "direct"}
        )
        if item.get("service_id"):
            direct_ids.append(item["service_id"])
    for service_id in direct_ids:
        found, truncated = reader.consumers(service_id, hop_limit)
        if mode == "full" and truncated:
            hop_limit_hit = True
        for consumer_id, hops in found.items():
            node = reader.node(consumer_id)
            if node is None or node["name"] in seen_services:
                continue
            seen_services.add(node["name"])
            affected.append(
                {"service": node["name"], "tier": node.get("tier"), "hops": hops, "kind": "consumer"}
            )
    outgoing = []
    outgoing_seen: set[str] = set()
    for item in resolved:
        if not item.get("service_id"):
            continue
        for edge in reader.edges_from(item["service_id"], "DEPENDS_ON"):
            node = reader.node(edge["dst"])
            if node is None or node["name"] in outgoing_seen or node["name"] in seen_services:
                continue
            outgoing_seen.add(node["name"])
            outgoing.append({"service": node["name"], "tier": node.get("tier"), "from": item["service"]})
    _call(
        trace,
        "impact_analysis",
        {"functions": [item["name"] for item in resolved], "hops": hop_limit},
        [{"service": item["service"], "hops": item["hops"], "tier": item["tier"]} for item in affected],
    )

    owners = []
    direct_owners = []
    for item in affected:
        service_id = f"svc:{item['service']}"
        if reader.node(service_id) is None:
            continue
        owner = reader.owner_of(service_id, now, policy["freshness"]["owner_days"])
        owner["service"] = item["service"]
        owner["hops"] = item["hops"]
        owners.append(owner)
        if item["hops"] == 0:
            direct_owners.append(owner)
    _call(trace, "nearest_owner", {"services": [item["service"] for item in affected]}, owners)

    incidents_open = []
    incidents_prior = []
    unrelated = []
    if mode == "full":
        for item in affected:
            service_id = f"svc:{item['service']}"
            for incident in reader.open_incidents(service_id):
                incidents_open.append(
                    {
                        "name": incident["name"],
                        "service": item["service"],
                        "tier": item["tier"],
                        "age_days": incident.get("age_days", 0),
                        "title": incident.get("title"),
                    }
                )
        for item in resolved:
            for pair in reader.prior_fixes(item["id"]):
                incidents_prior.append(
                    {
                        "incident": pair["incident"]["name"],
                        "fix": pair["fix"]["name"],
                        "function": item["name"],
                    }
                )
        if change.get("text_mentions_incident"):
            for incident in reader.by_label("Incident"):
                if incident.get("service") not in {item["service"] for item in affected}:
                    unrelated.append(incident["name"])
    _call(
        trace,
        "find_similar_incidents",
        {"functions": [item["name"] for item in resolved]},
        {"open": incidents_open, "prior": incidents_prior, "unrelated": unrelated}
        if mode == "full"
        else {"skipped": True, "reason": "no history join in the 1-hop arm"},
    )

    tests = []
    for item in resolved:
        status = reader.test_status(item["id"])
        tests.append({"function": item["name"], "status": status, "changed": True})
    _call(trace, "get_test_coverage", {"functions": [item["name"] for item in resolved]}, tests)

    critical_consumer_tests = []
    shared = any(item.get("kind") == "library" for item in resolved)
    if shared:
        for item in resolved:
            for callee_service in _critical_consumers(reader, item, affected):
                status, fname = _consumer_test(reader, item["id"], callee_service)
                critical_consumer_tests.append(
                    {"service": callee_service["name"], "function": fname, "status": status}
                )
    if critical_consumer_tests:
        _call(trace, "get_test_coverage", {"critical_consumers": True}, critical_consumer_tests)

    deploys = []
    for item in affected:
        if item["hops"] > hop_limit:
            continue
        node = reader.deploy_of(f"svc:{item['service']}")
        if node is None:
            deploys.append(
                {"service": item["service"], "present": False, "age_hours": None, "health": None, "tier": item["tier"]}
            )
        else:
            deploys.append(
                {
                    "service": item["service"],
                    "present": True,
                    "age_hours": node.get("age_hours"),
                    "health": node.get("health", "ok"),
                    "tier": item["tier"],
                }
            )
    _call(trace, "get_deploy_state", {"services": [item["service"] for item in affected]}, deploys)

    rollback_fix = None
    if mode == "full" and change.get("intent") == "rollback":
        for item in resolved:
            fixes = reader.prior_fixes(item["id"])
            if fixes:
                rollback_fix = {"name": fixes[0]["fix"]["name"], "function": item["name"]}
                break

    routes = []
    for owner in direct_owners:
        if owner.get("team") and not owner["missing"] and not owner["stale"]:
            routes.append({"service": owner["service"], "team": owner["team"]})

    freshness = [{"item": "CI", "detail": _ci_text(change), "ok": _ci_ok(change, policy)}]
    for owner in owners:
        if owner["hops"] != 0:
            continue
        if owner["missing"]:
            freshness.append({"item": f"owner:{owner['service']}", "detail": "missing", "ok": False})
        else:
            freshness.append(
                {
                    "item": f"owner:{owner['service']}",
                    "detail": "stale" if owner["stale"] else f"team {owner['team']}",
                    "ok": not owner["stale"],
                }
            )
    for deploy in deploys:
        if not deploy["present"]:
            freshness.append({"item": f"deploy:{deploy['service']}", "detail": "missing", "ok": False})
        else:
            ok = deploy["age_hours"] is not None and deploy["age_hours"] <= policy["freshness"]["deploy_hours"]
            freshness.append(
                {
                    "item": f"deploy:{deploy['service']}",
                    "detail": f"{deploy['age_hours']}h, health {deploy['health']}",
                    "ok": ok and deploy["health"] == "ok",
                }
            )

    return _bundle(
        change,
        mode,
        functions=resolved,
        affected=affected,
        owners=owners,
        direct_owners=direct_owners,
        incidents_open=incidents_open,
        incidents_prior=incidents_prior,
        tests=tests,
        deploys=deploys,
        critical_consumer_tests=critical_consumer_tests,
        owners_checked=True,
        deploys_checked=True,
        incidents_checked=mode == "full",
        history_checked=mode == "full",
        tests_checked=bool(resolved),
        consumer_tests_checked=shared,
        tiers_known=any(item.get("tier") is not None for item in affected),
        hop_limit_hit=hop_limit_hit,
        shared_name_in_diff=False,
        rollback_fix=rollback_fix,
        routes=routes,
        freshness=freshness,
        unrelated=unrelated,
        outgoing=outgoing,
        mapping=_effective_mapping(change, resolved, True),
    )


def _critical_consumers(reader: GraphReader, function: dict, affected: list[dict]) -> list[dict]:
    consumers = []
    for item in affected:
        if item["hops"] == 0 or item.get("tier") is None or item["tier"] > 1:
            continue
        node = reader.node(f"svc:{item['service']}")
        if node:
            consumers.append(node)
    return consumers


def _consumer_test(reader: GraphReader, function_id: str, service: dict) -> tuple[str, str]:
    for caller in reader.callers(function_id):
        parent = reader.function_service(caller)
        if parent and parent["id"] == service["id"]:
            node = reader.node(caller)
            name = node["name"] if node else caller
            return reader.test_status(caller), name
    functions = [
        reader.node(edge["src"])
        for edge in reader.edges_to(service["id"], "PART_OF")
    ]
    functions = [node for node in functions if node]
    if not functions:
        return "missing", service["name"]
    first = functions[0]
    return reader.test_status(first["id"]), first["name"]


def _bundle(change, mode, **fields) -> dict:
    evidence = {
        "mode": mode,
        "mapping": change.get("mapping", "resolved"),
        "ci": change.get("ci") or {"present": False, "result": None, "age_hours": None},
        "approval": {
            "designated": bool(change.get("designated_approval")),
            "approver": change.get("approver"),
        },
        "dependency": change.get("dependency"),
        "unresolved_refs": list(change.get("unresolved_refs") or []),
        "text_mentions_incident": bool(change.get("text_mentions_incident")),
        "intent": change.get("intent"),
        "shared_lib": any(item.get("kind") == "library" for item in fields["functions"]),
    }
    evidence.update(fields)
    evidence["catalog"] = set()
    return evidence


def _effective_mapping(change: dict, resolved: list[dict], graph_on: bool) -> str:
    mapping = change.get("mapping", "resolved")
    if not graph_on:
        return mapping
    symbols = change.get("symbols") or []
    if mapping == "resolved" and symbols and len(resolved) != len(symbols):
        return "unresolved"
    return mapping


def _services_from_paths(files: list[str]) -> list[str]:
    names = []
    for path in files:
        parts = path.replace("\\", "/").split("/")
        if "services" in parts:
            index = parts.index("services")
            if index + 1 < len(parts):
                names.append(parts[index + 1])
                continue
        if "shared" in parts:
            index = parts.index("shared")
            if index + 1 < len(parts):
                names.append(parts[index + 1])
                continue
        if len(parts) >= 2:
            names.append(parts[-2])
    seen = []
    for name in names:
        if name not in seen:
            seen.append(name)
    return seen


def _ci_text(change: dict) -> str:
    ci = change.get("ci") or {}
    if not ci.get("present"):
        if ci.get("configured"):
            return "workflow file present, latest run result not in the repository"
        return "missing"
    return f"{ci.get('result')} at {ci.get('age_hours')}h"


def _ci_ok(change: dict, policy: dict) -> bool:
    ci = change.get("ci") or {}
    if not ci.get("present"):
        return False
    return ci.get("age_hours", 999) <= policy["freshness"]["ci_hours"] and ci.get("result") == "pass"


def _rationale(decision: dict) -> str:
    if not decision["findings"]:
        return f"{decision['state']}. No hold, review, or guard rule triggered."
    lead = decision["findings"][0]
    # Prefer the finding that matches the winning severity.
    order = {"hold": 0, "insufficient": 1, "review": 2, "guard": 3}
    winning = min(decision["findings"], key=lambda item: order[item["severity"]])
    return f"{decision['state']}. {winning['detail']} ({winning['policy']})."


def _claims(decision: dict, evidence: dict) -> list[dict]:
    claims = []
    if decision["findings"]:
        for finding in decision["findings"]:
            claims.append(
                {
                    "text": finding["detail"],
                    "evidence_id": finding["evidence_id"],
                    "policy": finding["policy"],
                }
            )
    else:
        claims.append(
            {
                "text": "Impact, ownership, tests, and deploy state produced no blocking rule.",
                "evidence_id": "ev-clear",
                "policy": None,
            }
        )
    affected_names = {item["service"] for item in evidence.get("affected") or []}
    for ref in evidence.get("unresolved_refs") or []:
        service = ref.split(".")[0]
        if evidence.get("mode") == "full" and service not in affected_names:
            claims.append(
                {
                    "text": (
                        f"{ref} is referenced by the diff but is not in the blast radius. "
                        "The dependency points outward."
                    ),
                    "evidence_id": "ev-direction",
                    "policy": None,
                }
            )
            break
    if evidence.get("unrelated"):
        names = ", ".join(evidence["unrelated"])
        claims.append(
            {
                "text": f"Incident(s) {names} sit outside the blast radius and were not used.",
                "evidence_id": "ev-unrelated",
                "policy": None,
            }
        )
    if evidence.get("approval", {}).get("designated"):
        claims.append(
            {
                "text": f"Designated approval from {evidence['approval'].get('approver') or 'a reviewer'} is on the change.",
                "evidence_id": "ev-approval",
                "policy": None,
            }
        )
    return claims


def _view(evidence: dict, decision: dict) -> dict:
    nodes = []
    edges = []
    seen = set()

    def add(node_id: str, label: str, **props) -> None:
        if node_id in seen:
            return
        seen.add(node_id)
        nodes.append({"id": node_id, "label": label, **props})

    for item in evidence.get("functions") or []:
        if not item.get("service"):
            continue
        add(item["id"], item["name"], kind="function", role="changed")
        add(f"svc:{item['service']}", item["service"], kind="service", role="direct", tier=item.get("tier"))
        edges.append({"source": item["id"], "target": f"svc:{item['service']}", "label": "PART_OF"})
    for item in evidence.get("affected") or []:
        if item.get("hops", 0) == 0:
            continue
        add(
            f"svc:{item['service']}",
            item["service"],
            kind="service",
            role="consumer",
            tier=item.get("tier"),
            hops=item.get("hops"),
        )
    directs = [item for item in evidence.get("affected") or [] if item.get("hops") == 0]
    consumers = [item for item in evidence.get("affected") or [] if item.get("hops", 0) > 0]
    for consumer in consumers:
        # Attach the consumer to the nearest direct service for a readable path.
        anchor = directs[0]["service"] if directs else consumer["service"]
        edges.append(
            {
                "source": f"svc:{consumer['service']}",
                "target": f"svc:{anchor}",
                "label": f"DEPENDS_ON x{consumer['hops']}",
            }
        )
    for incident in evidence.get("incidents_open") or []:
        add(f"inc:{incident['name']}", incident["name"], kind="incident", role="incident")
        edges.append(
            {"source": f"inc:{incident['name']}", "target": f"svc:{incident['service']}", "label": "AFFECTED"}
        )
    for prior in evidence.get("incidents_prior") or []:
        add(f"fix:{prior['fix']}", prior["fix"], kind="fix", role="fix")
    fix = evidence.get("rollback_fix")
    if fix:
        add(f"fix:{fix['name']}", fix["name"], kind="fix", role="fix")
    for item in evidence.get("outgoing") or []:
        add(f"svc:{item['service']}", item["service"], kind="service", role="dependency", tier=item.get("tier"))
        if directs:
            edges.append(
                {
                    "source": f"svc:{directs[0]['service']}",
                    "target": f"svc:{item['service']}",
                    "label": "DEPENDS_ON",
                }
            )
    for owner in evidence.get("direct_owners") or []:
        if owner.get("team") and not owner.get("stale") and not owner.get("missing"):
            add(f"team:{owner['team']}", owner["team"], kind="team", role="owner")
            edges.append(
                {"source": f"team:{owner['team']}", "target": f"svc:{owner['service']}", "label": "OWNS"}
            )
    return {"nodes": nodes, "edges": edges, "state": decision["state"]}


def _write_back(graph: MemoryGraph, record: dict) -> None:
    graph.add_node(
        {
            "id": record["id"],
            "label": "DecisionRecord",
            "state": record["state"],
            "change_id": record["change_id"],
            "mode": record["mode"],
            "policy_version": record["policy_version"],
            "graph_revision": record.get("graph_revision"),
            "evidence_count": len(record.get("evidence") or []),
            "source": "edl",
            "evidence_type": "observed",
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    change_id = f"change:{record['change_id']}"
    if graph.node(change_id) is None:
        graph.add_node(
            {
                "id": change_id,
                "label": "Change",
                "name": record["change_id"],
                "source": "edl",
                "evidence_type": "observed",
            }
        )
    graph.add_edge(record["id"], "DECIDES", change_id)
