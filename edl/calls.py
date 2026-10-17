"""Build CALLS edges from Python sources onto Function nodes already in the graph.

This is a bounded, same-service name match — not a full language call graph.
It makes centrality_rank and hub rules meaningful on real Python packages.
"""

from __future__ import annotations

import ast
from pathlib import Path

from edl.memory import MemoryGraph

SKIP_NAMES = {
    "print",
    "len",
    "str",
    "int",
    "float",
    "list",
    "dict",
    "set",
    "range",
    "open",
    "isinstance",
    "getattr",
    "setattr",
    "super",
    "enumerate",
    "zip",
    "min",
    "max",
    "sum",
    "sorted",
    "any",
    "all",
    "bool",
    "type",
    "Exception",
    "ValueError",
    "KeyError",
    "RuntimeError",
    "TypeError",
}


def add_python_calls(graph: MemoryGraph, root: Path, *, limit: int = 400) -> int:
    """Add CALLS edges. Returns how many edges were created."""
    by_fname: dict[str, list[str]] = {}
    path_of: dict[str, str] = {}
    for node in graph.by_label("Function"):
        fname = str(node.get("fname") or "")
        if not fname or node.get("granularity") == "service":
            continue
        by_fname.setdefault(fname, []).append(node["id"])
        if node.get("path"):
            path_of[node["id"]] = str(node["path"])

    if not by_fname:
        return 0

    created = 0
    existing = {(edge["src"], edge["rel"], edge["dst"]) for edge in graph.edges}
    for node in list(graph.by_label("Function")):
        if created >= limit:
            break
        src = node["id"]
        rel = path_of.get(src) or node.get("path")
        if not rel:
            continue
        file_path = root / str(rel)
        if not file_path.is_file() or file_path.suffix != ".py":
            continue
        try:
            tree = ast.parse(file_path.read_text(encoding="utf-8", errors="ignore"))
        except (SyntaxError, OSError):
            continue
        callees = _callee_names(tree)
        for name in callees:
            if name in SKIP_NAMES or name.startswith("_"):
                continue
            for dst in by_fname.get(name) or []:
                if dst == src:
                    continue
                key = (src, "CALLS", dst)
                if key in existing:
                    continue
                graph.add_edge(
                    src,
                    "CALLS",
                    dst,
                    source="repository",
                    evidence_type="observed",
                    method="python-ast",
                )
                existing.add(key)
                created += 1
                if created >= limit:
                    return created
    return created


def _callee_names(tree: ast.AST) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = None
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        if name and name not in found:
            found.append(name)
    return found
