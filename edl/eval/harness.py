"""Run labeled scenarios and score decision quality.

Primary safety failure: unjustified PROCEED / PROCEED_WITH_GUARDS when the
expected outcome was HOLD, NEEDS_REVIEW, or INSUFFICIENT_EVIDENCE.
"""

from __future__ import annotations

from pathlib import Path

from edl.eval.scenarios import all_scenarios
from edl.graph_reader import MemoryReader
from edl.models import MODES
from edl.policy import load_policy
from edl.review import review_change

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY = ROOT / "policies" / "policies.yaml"

ALLOWING = frozenset({"PROCEED", "PROCEED_WITH_GUARDS"})
BLOCKING = frozenset({"HOLD", "NEEDS_REVIEW", "INSUFFICIENT_EVIDENCE"})


def run_eval(policy_path: Path | None = None, scenario_ids: list[str] | None = None) -> dict:
    policy = load_policy(policy_path or DEFAULT_POLICY)
    scenarios = all_scenarios()
    if scenario_ids:
        wanted = set(scenario_ids)
        scenarios = [item for item in scenarios if item["id"] in wanted]
    rows = []
    for scenario in scenarios:
        rows.append(_run_one(scenario, policy))
    return {
        "policy_version": policy.get("version"),
        "scenarios": rows,
        "summary": summarize(rows),
    }


def _run_one(scenario: dict, policy: dict) -> dict:
    graph = scenario["graph"]
    change = scenario["change"]
    reader = MemoryReader(graph)
    modes: dict[str, dict] = {}
    for mode in MODES:
        record = review_change(graph, change, policy, mode, reader=reader)
        expected = scenario["expected"][mode]
        actual = record["state"]
        modes[mode] = {
            "actual": actual,
            "expected": expected,
            "ok": actual == expected,
            "triggered": list(record.get("triggered") or []),
            "unjustified_proceed": _unjustified_proceed(actual, expected),
            "rationale": record.get("rationale"),
        }

    # Graph lift: full mode blocks using graph facts while graph-off would allow.
    # graph_off may *expect* PROCEED as the documented baseline failure; that still
    # counts as lift when full correctly blocks.
    graph_lift = (
        bool(scenario.get("graph_necessary"))
        and modes["full"]["ok"]
        and modes["full"]["actual"] in BLOCKING
        and modes["graph_off"]["actual"] in ALLOWING
    )

    llm = scenario.get("baseline_llm")
    llm_unjustified = bool(llm) and _unjustified_proceed(llm, scenario["expected"]["full"])

    return {
        "id": scenario["id"],
        "title": scenario["title"],
        "family": scenario["family"],
        "graph_necessary": bool(scenario.get("graph_necessary")),
        "notes": scenario.get("notes"),
        "modes": modes,
        "graph_lift": graph_lift,
        "baseline_llm": llm,
        "baseline_llm_unjustified_proceed": llm_unjustified,
        "unjustified_proceed_any_mode": any(
            modes[mode]["unjustified_proceed"] for mode in MODES
        ),
    }


def _unjustified_proceed(actual: str, expected: str) -> bool:
    """True when the system (or baseline) allowed a change that should not proceed."""
    return actual in ALLOWING and expected in BLOCKING


def summarize(rows: list[dict]) -> dict:
    total = len(rows)
    by_mode = {}
    for mode in MODES:
        correct = sum(1 for row in rows if row["modes"][mode]["ok"])
        unjustified = sum(1 for row in rows if row["modes"][mode]["unjustified_proceed"])
        by_mode[mode] = {
            "accuracy": (correct / total) if total else 0.0,
            "correct": correct,
            "total": total,
            "unjustified_proceed": unjustified,
        }

    graph_necessary = [row for row in rows if row["graph_necessary"]]
    graph_lift = sum(1 for row in graph_necessary if row["graph_lift"])
    llm_bad = sum(1 for row in rows if row.get("baseline_llm_unjustified_proceed"))

    return {
        "scenarios": total,
        "by_mode": by_mode,
        "graph_necessary_scenarios": len(graph_necessary),
        "graph_lift_count": graph_lift,
        "baseline_llm_unjustified_proceed": llm_bad,
        "unjustified_proceed_full": by_mode["full"]["unjustified_proceed"],
        "pass": by_mode["full"]["unjustified_proceed"] == 0
        and by_mode["full"]["correct"] == total,
    }


def format_report(result: dict) -> str:
    lines = [
        f"EDL evaluation  policy={result['policy_version']}  scenarios={result['summary']['scenarios']}",
        "",
    ]
    for row in result["scenarios"]:
        lift = " graph_lift" if row["graph_lift"] else ""
        lines.append(f"## {row['id']} ({row['family']}){lift}")
        lines.append(row["title"])
        for mode in MODES:
            cell = row["modes"][mode]
            mark = "OK" if cell["ok"] else "FAIL"
            danger = "  unjustified_PROCEED" if cell["unjustified_proceed"] else ""
            lines.append(
                f"  {mode:10} {mark:4}  actual={cell['actual']:<22} "
                f"expected={cell['expected']}{danger}"
            )
        if row.get("baseline_llm"):
            bad = "  unjustified_PROCEED" if row["baseline_llm_unjustified_proceed"] else ""
            lines.append(f"  {'llm':10} base  actual={row['baseline_llm']:<22}{bad}")
        lines.append("")

    summary = result["summary"]
    lines.append("## Summary")
    for mode, stats in summary["by_mode"].items():
        lines.append(
            f"  {mode:10} accuracy={stats['correct']}/{stats['total']}  "
            f"unjustified_proceed={stats['unjustified_proceed']}"
        )
    lines.append(
        f"  graph_lift={summary['graph_lift_count']}/{summary['graph_necessary_scenarios']} "
        f"necessary scenarios"
    )
    lines.append(
        f"  baseline_llm unjustified_proceed={summary['baseline_llm_unjustified_proceed']}"
    )
    lines.append(f"  harness_pass={summary['pass']}")
    return "\n".join(lines)
