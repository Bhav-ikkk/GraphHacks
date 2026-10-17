"""Scope one engineering change from a file set (git diff or explicit paths).

Structural ingest still builds the repository graph. This module turns a list of
modified paths into a single Change: resolved symbols, unresolved paths, and a
mapping status that policy can abstain on.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from edl.memory import MemoryGraph

ROOT_SOURCE_DIRS = {"src", "lib", "app", "internal"}


class ChangeError(Exception):
    pass


def list_changed_files(
    root: Path,
    *,
    base: str | None = None,
    head: str | None = None,
) -> tuple[list[str], str]:
    """Return (paths, how) from a local git repository.

    how is a short label such as 'git:HEAD~1..HEAD' or 'git:working-tree'.
    """
    root = root.resolve()
    if not (root / ".git").exists() and not _git_ok(root):
        raise ChangeError(f"{root} is not a git repository. Pass explicit files instead.")

    if base and head:
        paths = _git_names(root, f"{base}...{head}")
        if not paths:
            paths = _git_names(root, f"{base}..{head}")
        return paths, f"git:{base}...{head}"

    if base and not head:
        paths = _git_names(root, f"{base}...HEAD")
        if not paths:
            paths = _git_names(root, base)
        return paths, f"git:{base}...HEAD"

    if head and not base:
        paths = _git_names(root, f"HEAD...{head}")
        return paths, f"git:HEAD...{head}"

    working = _git_names(root, "HEAD")
    staged = _git_names(root, cached=True)
    merged = _unique(working + staged)
    if merged:
        return merged, "git:working-tree"

    paths = _git_names(root, "HEAD~1...HEAD")
    if paths:
        return paths, "git:HEAD~1...HEAD"
    raise ChangeError(
        "No changed files found (clean working tree and no HEAD~1..HEAD diff). "
        "Pass --files or a base/head range."
    )


def suggest_scope_files(
    graph: MemoryGraph,
    *,
    origin: dict | None = None,
    limit: int = 12,
) -> list[str]:
    """Paths the UI/CLI can offer when auto-git scope finds nothing.

    Prefers real git diffs on a local folder, then sample source paths stored
    on Service nodes during ingest. Never invents paths that were not observed.
    """
    if origin and origin.get("kind") == "path":
        root = Path(origin["value"])
        try:
            paths, _how = list_changed_files(root)
            if paths:
                return paths[:limit]
        except ChangeError:
            pass

    samples: list[str] = []
    for service in graph.by_label("Service"):
        if service.get("name") == "__repo__":
            continue
        for path in service.get("sample_paths") or []:
            if path not in samples:
                samples.append(path)
            if len(samples) >= limit:
                return samples
    return samples[:limit]


def scope_help_message(graph: MemoryGraph, origin: dict | None = None, *, reason: str) -> str:
    """Human-readable scope failure with concrete file suggestions when available."""
    hints = suggest_scope_files(graph, origin=origin)
    if hints:
        shown = ", ".join(hints[:6])
        extra = f" (+{len(hints) - 6} more)" if len(hints) > 6 else ""
        return f"{reason} Suggested files from this tree: {shown}{extra}."
    return (
        f"{reason} Pass explicit files (comma-separated paths), or for a local git "
        "folder use base/head when a diff exists."
    )


def build_scoped_change(
    graph: MemoryGraph,
    files: list[str],
    *,
    base: str | None = None,
    head: str | None = None,
    source: str = "files",
    ci_configured: bool = False,
    title: str | None = None,
    change_id: str | None = None,
) -> dict:
    """Map modified files onto graph symbols. Unresolved paths stay unresolved."""
    normalized = [_norm(path) for path in files if path and path.strip()]
    normalized = _unique(normalized)
    if not normalized:
        raise ChangeError("Change scope is empty. Provide at least one modified file.")

    services = graph.by_label("Service")
    functions = graph.by_label("Function")
    resolved: list[str] = []
    unresolved: list[str] = []
    matched_files: list[str] = []
    touches: list[dict] = []

    for path in normalized:
        service = service_for_path(services, path)
        if service is None:
            unresolved.append(path)
            touches.append({"path": path, "service": None, "symbols": [], "status": "unresolved"})
            continue
        symbols = symbols_for_path(functions, service["name"], path)
        if not symbols:
            unresolved.append(path)
            touches.append(
                {
                    "path": path,
                    "service": service["name"],
                    "symbols": [],
                    "status": "unresolved",
                }
            )
            continue
        matched_files.append(path)
        for symbol in symbols:
            if symbol not in resolved:
                resolved.append(symbol)
        touches.append(
            {
                "path": path,
                "service": service["name"],
                "symbols": symbols,
                "status": "resolved",
            }
        )

    if resolved and not unresolved:
        mapping = "resolved"
    elif not resolved:
        mapping = "unresolved"
    else:
        mapping = "partial"

    range_label = _range_label(base, head, source)
    auto_id = change_id or f"change:{_slug_id(range_label)}"
    auto_title = title or _default_title(resolved, unresolved, range_label)
    note = _note(mapping, resolved, unresolved, range_label)

    return {
        "id": auto_id,
        "title": auto_title,
        "symbols": resolved,
        "files": matched_files + unresolved,
        "description": f"Scoped change from {range_label}.",
        "ci": {
            "present": False,
            "result": None,
            "age_hours": None,
            "configured": ci_configured,
        },
        "designated_approval": False,
        "approver": None,
        "mapping": mapping,
        "dependency": None,
        "unresolved_refs": list(unresolved),
        "text_mentions_incident": False,
        "intent": None,
        "authorized": True,
        "ticketing": "ok",
        "note": note,
        "scope": {
            "source": source,
            "base": base,
            "head": head,
            "files": normalized,
            "touches": touches,
        },
        "commit": _commit_ref(base, head, source),
    }


def attach_change_node(graph: MemoryGraph, change: dict) -> None:
    """Write the Change node and TOUCHES / UNRESOLVED edges into the working graph."""
    graph_id = change["id"] if str(change["id"]).startswith("change:") else f"change:{change['id']}"
    scope = change.get("scope") or {}
    graph.add_node(
        {
            "id": graph_id,
            "label": "Change",
            "name": change["id"],
            "title": change.get("title"),
            "mapping": change.get("mapping"),
            "base": scope.get("base"),
            "head": scope.get("head"),
            "source": "change-scope",
            "evidence_type": "observed",
            "method": scope.get("source") or "files",
        }
    )
    for symbol in change.get("symbols") or []:
        fid = f"fn:{symbol}"
        if graph.node(fid) is None:
            continue
        graph.add_edge(
            graph_id,
            "TOUCHES",
            fid,
            source="change-scope",
            evidence_type="observed",
        )
    for path in change.get("unresolved_refs") or []:
        missing_id = f"path:{_slug_id(path)}"
        if graph.node(missing_id) is None:
            graph.add_node(
                {
                    "id": missing_id,
                    "label": "UnresolvedPath",
                    "name": path,
                    "source": "change-scope",
                    "evidence_type": "observed",
                    "method": "unresolved",
                }
            )
        graph.add_edge(
            graph_id,
            "UNRESOLVED",
            missing_id,
            source="change-scope",
            evidence_type="observed",
        )


def service_for_path(services: list[dict], path: str) -> dict | None:
    parts = _norm(path).split("/")
    partset = {_slug(part) for part in parts}
    matches = [
        service
        for service in services
        if service["name"] in partset and service["name"] != "__repo__"
    ]
    if matches:
        return sorted(matches, key=lambda service: len(service["name"]), reverse=True)[0]
    if len(services) == 1:
        return services[0]
    roots = [service for service in services if service.get("root")]
    if len(roots) == 1 and (len(parts) == 1 or parts[0] in ROOT_SOURCE_DIRS):
        return roots[0]
    return None


def symbols_for_path(functions: list[dict], service_name: str, path: str) -> list[str]:
    """Return symbols defined in this file — never every function in the service."""
    path = _norm(path)
    stem = Path(path).stem
    stem_key = _slug(stem)
    svc_fns = [node for node in functions if node.get("service") == service_name]
    if not svc_fns:
        return []

    by_path = [
        node["name"]
        for node in svc_fns
        if _norm(str(node.get("path") or "")) == path and node.get("granularity") != "service"
    ]
    if by_path:
        return _unique(by_path)

    # Legacy nodes without path: only accept exact file-stem == function name.
    exact = []
    for node in svc_fns:
        if node.get("path"):
            continue
        fname_key = _slug(str(node.get("fname") or ""))
        if fname_key and fname_key == stem_key:
            exact.append(node["name"])
    if exact:
        return _unique(exact)

    folder = [
        node["name"]
        for node in svc_fns
        if node.get("granularity") == "service"
        and (not node.get("path") or _norm(str(node.get("path") or "")) == path)
    ]
    return _unique(folder)


def _git_ok(root: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and "true" in (result.stdout or "").lower()


def _git_names(root: Path, rev_range: str | None = None, *, cached: bool = False) -> list[str]:
    if cached:
        command = [
            "git",
            "-C",
            str(root),
            "diff",
            "--name-only",
            "--diff-filter=ACMR",
            "--cached",
            "HEAD",
        ]
    elif rev_range:
        command = [
            "git",
            "-C",
            str(root),
            "diff",
            "--name-only",
            "--diff-filter=ACMR",
            rev_range,
        ]
    else:
        command = ["git", "-C", str(root), "diff", "--name-only", "--diff-filter=ACMR", "HEAD"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ChangeError(f"git failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "git diff failed").strip()
        raise ChangeError(detail.splitlines()[0][:240])
    return [_norm(line) for line in (result.stdout or "").splitlines() if line.strip()]


def _norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _unique(items: list[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def _slug(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-")


def _slug_id(value: str) -> str:
    text = _slug(value) or "change"
    return text[:48]


def _range_label(base: str | None, head: str | None, source: str) -> str:
    if base and head:
        return f"{base}...{head}"
    if base:
        return f"{base}...HEAD"
    if head:
        return f"HEAD...{head}"
    return source


def _commit_ref(base: str | None, head: str | None, source: str) -> str:
    if head:
        return head
    if base:
        return base
    return f"scope-{source}"


def _default_title(resolved: list[str], unresolved: list[str], range_label: str) -> str:
    if resolved:
        shown = ", ".join(resolved[:3])
        extra = f" +{len(resolved) - 3}" if len(resolved) > 3 else ""
        return f"{shown}{extra} ({range_label})"
    return f"Unresolved paths ({range_label})"


def _note(mapping: str, resolved: list[str], unresolved: list[str], range_label: str) -> str:
    parts = [f"Scoped from {range_label}: {len(resolved)} symbol(s), {len(unresolved)} unresolved path(s)."]
    if mapping == "resolved":
        parts.append("Every modified path mapped onto the graph.")
    elif mapping == "partial":
        parts.append("Some paths could not be mapped; mapping is partial.")
    else:
        parts.append("No modified path mapped onto a known service or function.")
    return " ".join(parts)
