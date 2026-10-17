"""Human-readable CLI reports: Falkor graph, decision, next steps, agent nodes."""

from __future__ import annotations

from typing import Any

from edl.branding import PRODUCT_LONG, VERSION, banner, line


def format_ingest_report(engine, summary: dict) -> str:
    snap = engine.snapshot()
    falkor = snap.get("falkor") or {}
    origin = summary.get("origin") or {}
    lines = [
        banner(),
        line("Repository"),
        f"  target     {origin.get('value')}",
        f"  kind       {origin.get('kind')}",
        f"  nodes      {summary.get('nodes')}    edges {summary.get('edges')}",
        f"  services   {len(summary.get('services') or [])}",
        "",
        line("FalkorDB"),
        f"  ready      {bool(falkor.get('ready') or falkor.get('ok'))}",
        f"  graph      {falkor.get('graph')}",
        f"  detail     {falkor.get('detail') or 'not connected'}",
    ]
    notes = list(getattr(engine, "evidence_notes", []) or [])
    if notes:
        lines.append(f"  evidence   {', '.join(notes)}")
    lines.append("")
    lines.append(line("Services (graph nodes)"))
    for service in summary.get("services") or []:
        owner = service.get("owner") or "no CODEOWNERS"
        tier = service.get("tier")
        tier_text = f"tier={tier}" if tier is not None else "tier=?"
        lines.append(f"  svc:{service['name']:<20} {tier_text:<10} owner={owner}")
    if engine.changes:
        change = engine.changes[0]
        lines.append("")
        lines.append(line("Scoped change"))
        lines.append(f"  id         {change.get('id')}")
        lines.append(f"  mapping    {change.get('mapping')}")
        lines.append(f"  symbols    {', '.join(change.get('symbols') or []) or '(none)'}")
        unresolved = change.get("unresolved_refs") or []
        if unresolved:
            lines.append(f"  unresolved {', '.join(unresolved)}")
        lines.append(f"  note       {change.get('note') or ''}")
    elif summary.get("scope_error"):
        lines.append("")
        lines.append(line("Scope"))
        lines.append(f"  {summary['scope_error']}")
    suggested = summary.get("suggested_files") or snap.get("suggested_files") or []
    if suggested:
        lines.append("")
        lines.append(line("Suggested files to scope"))
        for path in suggested[:10]:
            lines.append(f"  - {path}")
    lines.append("")
    lines.append(line("Next"))
    lines.append(
        "  python -m edl report <repo> --files path/to/changed.py"
    )
    lines.append("  python -m edl agent <repo> --files path/to/changed.py --llm")
    lines.append("  python -m edl serve")
    return "\n".join(lines)


def format_decision_report(
    engine,
    record: dict,
    *,
    action: dict | None = None,
    modes: dict | None = None,
) -> str:
    snap = engine.snapshot()
    falkor = snap.get("falkor") or {}
    origin = snap.get("origin") or {}
    action = action or record.get("action") or {}
    graph_view = record.get("graph") or {}
    nodes = graph_view.get("nodes") or []
    edges = graph_view.get("edges") or []

    lines = [
        banner(),
        line("Decision"),
        f"  state      {record.get('state')}",
        f"  change     {record.get('change_id')}",
        f"  mode       {record.get('mode')}",
        f"  backend    {record.get('graph_backend') or snap.get('backend')}",
        f"  rationale  {record.get('rationale')}",
        f"  triggered  {', '.join(record.get('triggered') or []) or 'none'}",
        "",
        line("Repository + FalkorDB"),
        f"  target     {origin.get('value')}",
        f"  falkor     {falkor.get('detail') or 'not connected'}",
        f"  revision   {record.get('graph_revision')}",
        f"  evidence   {len(record.get('evidence') or [])} rows",
    ]
    if modes:
        lines.append(
            "  modes      "
            + ", ".join(f"{name}={item.get('state')}" for name, item in modes.items())
        )

    lines.append("")
    lines.append(line("Claims (explainable)"))
    claims = record.get("claims") or []
    if not claims:
        lines.append("  (none)")
    for claim in claims:
        lines.append(f"  [{claim.get('evidence_id')}] {claim.get('text')}")

    missing = record.get("missing") or []
    if missing:
        lines.append("")
        lines.append(line("Missing / stale"))
        for item in missing:
            if isinstance(item, dict):
                lines.append(f"  - {item.get('detail') or item}")
            else:
                lines.append(f"  - {item}")

    lines.append("")
    lines.append(line("Action gate"))
    lines.append(
        f"  status     {action.get('status') or 'n/a'} — {action.get('reason') or ''}"
    )

    lines.append("")
    lines.append(line("Graph nodes used in this decision (give these to agents)"))
    if not nodes:
        lines.append("  (none recorded on this run)")
    for node in nodes[:40]:
        node_id = node.get("id") or node.get("name") or "?"
        label = node.get("kind") or node.get("label") or ""
        role = node.get("role") or ""
        lines.append(f"  {node_id:<32} {label:<12} {role}")
    if len(nodes) > 40:
        lines.append(f"  … +{len(nodes) - 40} more")
    if edges:
        lines.append(f"  edges used {len(edges)}")

    pack = agent_node_pack(record, snap)
    lines.append("")
    lines.append(line("Agent context pack (copy into Cursor / MCP chat)"))
    for row in pack:
        lines.append(f"  {row}")

    lines.append("")
    lines.append(line("What you should do"))
    lines.extend(_next_steps(record, action, origin))
    lines.append("")
    lines.append(f"{PRODUCT_LONG} v{VERSION} - policy is the judge; FalkorDB is the witness.")
    return "\n".join(lines)


def agent_node_pack(record: dict, snap: dict) -> list[str]:
    """Compact lines an agent host can ingest as context."""
    graph_view = record.get("graph") or {}
    node_ids = [n.get("id") for n in (graph_view.get("nodes") or []) if n.get("id")]
    change = (snap.get("changes") or [{}])[0]
    rows = [
        f"state={record.get('state')}",
        f"change_id={record.get('change_id')}",
        f"symbols={','.join(change.get('symbols') or [])}",
        f"triggered={','.join(record.get('triggered') or [])}",
        f"backend={record.get('graph_backend')}",
    ]
    if node_ids:
        rows.append("nodes=" + ",".join(node_ids[:24]))
    evidence_ids = [
        item.get("id")
        for item in (record.get("evidence") or [])
        if isinstance(item, dict) and item.get("id")
    ]
    if evidence_ids:
        rows.append("evidence=" + ",".join(evidence_ids[:16]))
    return rows


def _next_steps(record: dict, action: dict, origin: dict) -> list[str]:
    state = str(record.get("state") or "")
    target = origin.get("value") or "<repo>"
    steps = []
    if state == "HOLD":
        steps.append("  1. Do not merge. Resolve the open incident / failing health cited above.")
        steps.append("  2. Re-run: python -m edl report \"{0}\" --files <same files>".format(target))
    elif state == "NEEDS_REVIEW":
        steps.append("  1. Route to the owners listed in claims / CODEOWNERS.")
        steps.append("  2. After approval, re-run report and confirm state upgrades.")
    elif state == "INSUFFICIENT_EVIDENCE":
        steps.append("  1. Add CI runs, deploy, or incident data (Actions / syzygy.evidence.yaml).")
        steps.append("  2. Fix unresolved file→symbol mapping if P6 fired.")
        steps.append("  3. Re-run report once evidence exists — do not invent a PROCEED.")
    elif state == "PROCEED_WITH_GUARDS":
        steps.append("  1. Keep the listed guards (tests / hop limits) before merge.")
        steps.append("  2. Optional: python -m edl agent \"{0}\" --files <files> --act".format(target))
    elif state == "PROCEED":
        steps.append("  1. Safe to proceed under current policy + graph evidence.")
        if action.get("status") == "ready":
            steps.append("  2. Optional gated issue: add --act on the agent command.")
    else:
        steps.append("  1. Re-run with --files scoped to the paths you changed.")
    steps.append(
        "  Agent: python -m edl agent \"{0}\" --files <files> --llm".format(target)
    )
    steps.append("  UI:    python -m edl serve   → http://127.0.0.1:8000")
    return steps


def format_status(engine) -> str:
    snap = engine.snapshot()
    falkor = snap.get("falkor") or {}
    origin = snap.get("origin") or {}
    return "\n".join(
        [
            banner(),
            line("Status"),
            f"  product    {PRODUCT_LONG} v{VERSION}",
            f"  dataset    {snap.get('dataset')}",
            f"  target     {origin.get('value') or '(empty - ingest a repo)'}",
            f"  nodes      {snap.get('nodes')}  edges={snap.get('edges')}",
            f"  falkor     {falkor.get('detail')}",
            f"  ready      {snap.get('graph_ready')}",
            f"  changes    {len(snap.get('changes') or [])}",
        ]
    )
