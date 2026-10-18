"""LLM tool loop for one engineering change.

The model may choose named tools and write the narration. decide_change is the
only source of the state. A missing API key uses the local planner, which
follows the same rule.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from edl.agent import LOG, POLICY, MAX_SYMBOL_PROBES, _explain, load_scenario
from edl.config import load_env
from edl.engine import Engine
from edl.progress import step as progress
from edl.tools import ToolContext, call_tool, list_tools

STATES = (
    "PROCEED_WITH_GUARDS",
    "INSUFFICIENT_EVIDENCE",
    "NEEDS_REVIEW",
    "PROCEED",
    "HOLD",
)

SYSTEM = (
    "You investigate one engineering change by calling Syzygy tools. "
    "Do not write Cypher. Do not invent CI results, incidents, owners, or a decision state. "
    "Read the graph first (impact, owner, incidents, deploy, callers, tests), "
    "then call decide_change, then propose_action. "
    "The state inside decide_change is final. Repeat that state in your last message. "
    "Do not upgrade HOLD, NEEDS_REVIEW, or INSUFFICIENT_EVIDENCE."
)


GEMINI_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"


@dataclass
class LlmSettings:
    base_url: str
    api_key: str
    model: str
    provider: str = ""

    @property
    def remote(self) -> bool:
        return bool(self.base_url and self.api_key)


def llm_settings() -> LlmSettings:
    """Resolve OpenAI-compatible chat settings.

    Google AI Studio keys work via Gemini's OpenAI-compatible endpoint when
    SYZYGY_LLM_PROVIDER=gemini, or when GEMINI_API_KEY / GOOGLE_API_KEY is set,
    or when an API key is present without a base URL (defaults to Gemini).
    """
    load_env()
    provider = os.environ.get("SYZYGY_LLM_PROVIDER", "").strip().lower()
    api_key = (
        os.environ.get("SYZYGY_LLM_API_KEY", "").strip()
        or os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
    )
    base_url = os.environ.get("SYZYGY_LLM_BASE_URL", "").strip().rstrip("/")
    model = os.environ.get("SYZYGY_LLM_MODEL", "").strip()

    gemini_hint = provider in {"gemini", "google", "ai-studio"} or bool(
        os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    )
    if api_key and (gemini_hint or not base_url):
        provider = provider or "gemini"
        base_url = base_url or GEMINI_OPENAI_BASE
        model = model or DEFAULT_GEMINI_MODEL
    elif not model:
        model = "gpt-4o-mini"

    return LlmSettings(
        base_url=base_url,
        api_key=api_key,
        model=model,
        provider=provider or ("openai" if base_url else ""),
    )


def llm_public_status() -> dict:
    settings = llm_settings()
    ready = bool(settings.remote)
    return {
        "llm_ready": ready,
        "llm_model": settings.model if ready else None,
        "llm_provider": settings.provider if ready else None,
    }


def _llm_timeout() -> int:
    raw = os.environ.get("SYZYGY_LLM_TIMEOUT", "120").strip()
    try:
        return max(30, min(int(raw), 180))
    except ValueError:
        return 120


def _is_timeout(exc: object) -> bool:
    text = str(exc).lower()
    return "timed out" in text or "timeout" in text or isinstance(exc, TimeoutError)


def openai_tools() -> list[dict]:
    tools = []
    for spec in list_tools():
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": spec["name"],
                    "description": spec["description"],
                    "parameters": spec["inputSchema"],
                },
            }
        )
    return tools


class ToolCall:
    def __init__(self, name: str, arguments: dict, call_id: str) -> None:
        self.name = name
        self.arguments = arguments
        self.call_id = call_id


class PlannerStep:
    def __init__(self, calls: list[ToolCall] | None = None, final: str | None = None) -> None:
        self.calls = calls or []
        self.final = final


class Planner(Protocol):
    name: str

    def next(self, trace: list[dict]) -> PlannerStep: ...

    def observe(self, call: ToolCall, payload: dict) -> None: ...


class LocalPlanner:
    """Chooses the same tools an investigator should, with no network call."""

    name = "local"

    def __init__(self, task: dict) -> None:
        self.task = task
        self.started = False
        self.planned = False
        self.queue: list[ToolCall] = []

    def next(self, trace: list[dict]) -> PlannerStep:
        if not self.started:
            self.started = True
            if self.task.get("scenario"):
                return PlannerStep(calls=[ToolCall("list_changes", {}, "list_changes")])
            if self.task.get("target"):
                return PlannerStep(
                    calls=[
                        ToolCall(
                            "ingest_repository",
                            {
                                "target": self.task["target"],
                                "files": self.task.get("files"),
                                "base": self.task.get("base"),
                                "head": self.task.get("head"),
                            },
                            "ingest_repository",
                        )
                    ]
                )
            if self.task.get("files") or self.task.get("base") or self.task.get("head"):
                return PlannerStep(
                    calls=[
                        ToolCall(
                            "scope_change",
                            {
                                "files": self.task.get("files"),
                                "base": self.task.get("base"),
                                "head": self.task.get("head"),
                            },
                            "scope_change",
                        )
                    ]
                )
            return PlannerStep(calls=[ToolCall("list_changes", {}, "list_changes")])
        if not any(item["tool"] == "list_changes" for item in trace):
            return PlannerStep(calls=[ToolCall("list_changes", {}, "list_changes")])
        if not self.planned:
            self.queue = _investigation_calls(trace, self.task)
            self.planned = True
        if self.queue:
            return PlannerStep(calls=[self.queue.pop(0)])
        return PlannerStep(final=_local_narration(trace))

    def observe(self, call: ToolCall, payload: dict) -> None:
        return None


class RemotePlanner:
    """OpenAI-compatible chat completions with tool calls."""

    name = "remote"

    def __init__(self, settings: LlmSettings, task_text: str) -> None:
        self.settings = settings
        self.messages: list[dict] = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": task_text},
        ]
        self._pending_message: dict | None = None

    def next(self, trace: list[dict]) -> PlannerStep:
        del trace
        message = chat_completion(self.messages, self.settings)
        self._pending_message = message
        calls = _parse_tool_calls(message)
        if calls:
            self.messages.append(message)
            return PlannerStep(calls=calls)
        return PlannerStep(final=str(message.get("content") or "").strip())

    def observe(self, call: ToolCall, payload: dict) -> None:
        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": call.call_id,
                "content": json.dumps(payload, default=str)[:12000],
            }
        )


def chat_completion(
    messages: list[dict],
    settings: LlmSettings,
    *,
    with_tools: bool = True,
) -> dict:
    if not settings.base_url:
        raise RuntimeError("SYZYGY_LLM_BASE_URL is empty.")
    if not settings.api_key:
        raise RuntimeError("SYZYGY_LLM_API_KEY is empty.")
    url = settings.base_url + "/chat/completions"
    body: dict[str, Any] = {
        "model": settings.model,
        "temperature": 0,
        "messages": messages,
    }
    if with_tools:
        body["tools"] = openai_tools()
    encoded = json.dumps(body).encode("utf-8")
    timeout = _llm_timeout()
    last_error: Exception | None = None
    for attempt in range(2):
        request = urllib.request.Request(url, data=encoded, method="POST")
        request.add_header("Content-Type", "application/json")
        if settings.api_key:
            request.add_header("Authorization", f"Bearer {settings.api_key}")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"LLM HTTP {exc.code}: {detail}") from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            last_error = exc
            reason = getattr(exc, "reason", exc)
            if attempt == 0 and _is_timeout(reason):
                continue
            raise RuntimeError(f"LLM request failed: {reason}") from exc
    else:
        raise RuntimeError(f"LLM request failed: {last_error}") from last_error
    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError("LLM response had no choices.")
    message = choices[0].get("message") or {}
    message.setdefault("role", "assistant")
    return message


def run_llm_agent(
    target: str | None = None,
    *,
    files: list[str] | None = None,
    base: str | None = None,
    head: str | None = None,
    change_id: str | None = None,
    mode: str = "full",
    execute_action: bool = False,
    engine: Engine | None = None,
    hops: int = 3,
    scenario: str | None = None,
    planner: Planner | None = None,
    max_steps: int = 40,
) -> dict[str, Any]:
    """Let a planner pick tools. The policy state is copied from decide_change."""
    if scenario:
        active = load_scenario(scenario, engine)
    else:
        active = engine or Engine(POLICY, LOG)
    ctx = ToolContext(active)
    if not change_id and active.changes:
        change_id = active.changes[0]["id"]
    task = {
        "target": target,
        "files": files,
        "base": base,
        "head": head,
        "mode": mode,
        "execute": execute_action,
        "hops": hops,
        "scenario": scenario,
        "change_id": change_id,
    }
    active_planner = planner or _default_planner(task)
    trace: list[dict] = []
    narration = ""
    error = ""

    def step(call: ToolCall) -> dict:
        arguments = _lock_arguments(call, task, trace, change_id)
        progress(f"Agent tool: {call.name}")
        payload = call_tool(ctx, call.name, arguments)
        trace.append({"tool": call.name, "arguments": arguments, "response": payload})
        active_planner.observe(call, payload)
        return payload

    try:
        for _ in range(max_steps):
            action = active_planner.next(trace)
            if action.final is not None and not action.calls:
                narration = action.final
                break
            if not action.calls:
                narration = action.final or "Planner stopped without a tool call."
                break
            for item in action.calls:
                step(item)
        else:
            narration = narration or "Stopped at the tool-call limit."
    except Exception as exc:
        error = str(exc)

    _ensure_decision(step, trace, task, change_id)
    decision = _last_result(trace, "decide_change")
    action_payload = _last_result(trace, "propose_action")
    action = (action_payload or {}).get("action") or {}
    if not decision:
        return {
            "ok": False,
            "error": error or "No decision. Pass --files, a git base/head, or --scenario.",
            "planner": active_planner.name,
            "tool_trace": trace,
            "tool_selection": [item["tool"] for item in trace],
            "narration": narration,
        }
    explanation = _explain(trace, decision, action)
    conflict = narration_conflicts(narration, str(decision.get("state") or ""))
    report = {
        "ok": bool(decision) and not error,
        "change_id": _change_id_from(trace, change_id),
        "mode": mode,
        "state": decision.get("state"),
        "rationale": decision.get("rationale"),
        "action": action,
        "graph_backend": _backend(trace),
        "planner": active_planner.name,
        "model": llm_settings().model if active_planner.name == "remote" else "",
        "tool_selection": [item["tool"] for item in trace],
        "tool_trace": trace,
        "explanation": explanation,
        "narration": narration,
        "narration_conflict": conflict,
    }
    if error:
        report["ok"] = False
        report["error"] = error
    return report


def narration_conflicts(text: str, state: str) -> bool:
    if not text or not state:
        return False
    return any(name != state for name in _mentioned_states(text))


def _mentioned_states(text: str) -> set[str]:
    found: set[str] = set()
    remaining = text
    for name in STATES:
        if re.search(rf"\b{name}\b", remaining):
            found.add(name)
            remaining = remaining.replace(name, " ")
    return found


def _default_planner(task: dict) -> Planner:
    settings = llm_settings()
    force_remote = os.environ.get("SYZYGY_LLM_FORCE_REMOTE", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    # Offline eval scenarios stay deterministic unless explicitly forced remote.
    if task.get("scenario") and not force_remote:
        return LocalPlanner(task)
    if settings.remote:
        return RemotePlanner(settings, _task_text(task))
    return LocalPlanner(task)


def _task_text(task: dict) -> str:
    parts = [
        "Investigate this change and finish by calling decide_change, then propose_action.",
        f"mode={task.get('mode')}",
    ]
    if task.get("scenario"):
        parts.append(f"The change is already loaded from scenario {task['scenario']}. Start with list_changes.")
    elif task.get("target"):
        parts.append(f"Repository target: {task['target']}")
        if task.get("files"):
            parts.append("Changed files: " + ", ".join(task["files"]))
    return "\n".join(parts)


def _investigation_calls(trace: list[dict], task: dict) -> list[ToolCall]:
    change_id = _change_id_from(trace, task.get("change_id"))
    symbols = _symbols_from(trace)
    services: list[str] = []
    for symbol in symbols:
        service_id = f"svc:{symbol.split('.', 1)[0]}"
        if service_id not in services:
            services.append(service_id)
    calls: list[ToolCall] = []
    hops = int(task.get("hops") or 3)
    for service_id in services:
        calls.append(ToolCall("impact_analysis", {"service_id": service_id, "hops": hops}, f"impact:{service_id}"))
        calls.append(ToolCall("nearest_owner", {"service_id": service_id}, f"owner:{service_id}"))
        calls.append(ToolCall("find_similar_incidents", {"service_id": service_id}, f"inc:{service_id}"))
        calls.append(ToolCall("get_deploy_state", {"service_id": service_id}, f"deploy:{service_id}"))
    # Cap per-function probes so large files cannot exhaust the step budget
    # before decide_change. Policy still sees every scoped symbol via decide.
    for symbol in symbols[:MAX_SYMBOL_PROBES]:
        function_id = f"fn:{symbol}"
        calls.append(ToolCall("centrality_rank", {"function_id": function_id}, f"rank:{function_id}"))
        calls.append(ToolCall("get_test_coverage", {"function_id": function_id}, f"tests:{function_id}"))
    if change_id:
        calls.append(
            ToolCall(
                "decide_change",
                {"change_id": change_id, "mode": task.get("mode") or "full"},
                "decide_change",
            )
        )
        calls.append(
            ToolCall(
                "propose_action",
                {
                    "change_id": change_id,
                    "mode": task.get("mode") or "full",
                    "execute": bool(task.get("execute")),
                },
                "propose_action",
            )
        )
    return calls


def _ensure_decision(step, trace: list[dict], task: dict, change_id: str | None) -> None:
    selected = _change_id_from(trace, change_id or task.get("change_id"))
    if not selected:
        return
    if not any(item["tool"] == "decide_change" for item in trace):
        step(ToolCall("decide_change", {"change_id": selected, "mode": task.get("mode") or "full"}, "decide_change"))
    if not any(item["tool"] == "propose_action" for item in trace):
        step(
            ToolCall(
                "propose_action",
                {
                    "change_id": selected,
                    "mode": task.get("mode") or "full",
                    "execute": bool(task.get("execute")),
                },
                "propose_action",
            )
        )


def _lock_arguments(call: ToolCall, task: dict, trace: list[dict], change_id: str | None) -> dict:
    arguments = dict(call.arguments or {})
    selected = change_id or task.get("change_id") or _change_id_from(trace, None)
    if call.name in {"decide_change", "propose_action"}:
        arguments["mode"] = task.get("mode") or "full"
        if selected:
            arguments["change_id"] = selected
    if call.name == "propose_action":
        arguments["execute"] = bool(task.get("execute"))
    return arguments


def _local_narration(trace: list[dict]) -> str:
    decision = _last_result(trace, "decide_change") or {}
    action = (_last_result(trace, "propose_action") or {}).get("action") or {}
    state = decision.get("state") or "unknown"
    rationale = decision.get("rationale") or f"The policy state is {state}."
    status = action.get("status") or "not-run"
    reason = action.get("reason") or ""
    return (
        f"I called the named graph tools, then decide_change. {rationale} "
        f"The gate says {status}: {reason} That state stands."
    )


def _parse_tool_calls(message: dict) -> list[ToolCall]:
    calls = []
    for item in message.get("tool_calls") or []:
        function = item.get("function") or {}
        name = str(function.get("name") or "")
        raw = function.get("arguments") if "arguments" in function else item.get("arguments")
        if isinstance(raw, str):
            try:
                arguments = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                arguments = {}
        elif isinstance(raw, dict):
            arguments = raw
        else:
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        calls.append(ToolCall(name, arguments, str(item.get("id") or name)))
    return calls


def _change_id_from(trace: list[dict], explicit: str | None) -> str | None:
    if explicit:
        return explicit
    for item in reversed(trace):
        if item["tool"] not in {"list_changes", "ingest_repository", "scope_change"}:
            continue
        result = (item.get("response") or {}).get("result") or {}
        for change in _change_rows(result):
            if change.get("id"):
                return str(change["id"])
        if result.get("id"):
            return str(result["id"])
    return None


def _symbols_from(trace: list[dict]) -> list[str]:
    for item in reversed(trace):
        if item["tool"] not in {"list_changes", "ingest_repository", "scope_change"}:
            continue
        result = (item.get("response") or {}).get("result") or {}
        for change in _change_rows(result):
            symbols = list(change.get("symbols") or [])
            if symbols:
                return symbols
    return []


def _change_rows(result: dict) -> list[dict]:
    rows = result.get("changes")
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, dict)]
    change = result.get("change")
    if isinstance(change, dict):
        return [change]
    return []


def _last_result(trace: list[dict], name: str) -> dict | None:
    for item in reversed(trace):
        if item["tool"] == name:
            return (item.get("response") or {}).get("result") or {}
    return None


def _backend(trace: list[dict]) -> str:
    for item in reversed(trace):
        if item["tool"] == "decide_change":
            return str((item.get("response") or {}).get("backend") or "")
    return ""
