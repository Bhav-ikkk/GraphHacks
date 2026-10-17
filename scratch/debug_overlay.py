from pathlib import Path
import tempfile

from edl.engine import Engine
from edl.tools import ToolContext, call_tool

tmp = Path(tempfile.mkdtemp())
(tmp / "edl").mkdir()
(tmp / "edl" / "charge.py").write_text("def charge():\n    return 1\n", encoding="utf-8")
(tmp / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
(tmp / "syzygy.evidence.yaml").write_text(
    """
ci:
  present: true
  result: pass
  age_hours: 1
  configured: true
tiers:
  edl: 0
deploys:
  - service: edl
    health: ok
    age_hours: 1
approvals:
  designated: true
  approver: lead
tests:
  - function: edl.charge
    status: pass
incidents:
  - id: edl-outage
    service: edl
    status: open
    age_days: 1
""".strip(),
    encoding="utf-8",
)
engine = Engine(Path("policies/policies.yaml"))
ctx = ToolContext(engine)
payload = call_tool(
    ctx, "ingest_repository", {"target": str(tmp), "files": ["edl/charge.py"]}
)
print("services", payload["result"]["services"])
print("notes", payload["result"].get("evidence_notes"))
print("inc", [n for n in engine.graph.nodes if str(n).startswith("inc:")])
print("svc", engine.graph.node("svc:edl"))
change_id = payload["result"]["changes"][0]["id"]
print("change ci", engine.change(change_id).get("ci"))
decide = call_tool(ctx, "decide_change", {"change_id": change_id, "mode": "full"})
print("state", decide["result"]["state"])
print("rationale", decide["result"]["rationale"])
print("triggered", decide["result"].get("triggered"))
print("missing", decide["result"].get("missing"))
print("backend", decide.get("backend"))
