"""Turn a repository the user chooses into a decision graph.

Only facts found in that tree are stored: services, compose dependencies,
CODEOWNERS, test files, workflow files, and function names from source.
CI run results, deployment health, incidents, and tiers are not invented.
Nothing here pins a demo repository.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from edl.memory import MemoryGraph

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "coverage",
    "vendor",
    "__pycache__",
    "docs",
    "doc",
    "examples",
}
IGNORE_AS_SERVICE = SKIP_DIRS | {".github", "tests", "test", "scratch", "scripts", "tools"}
CODE_MARKERS = ("Dockerfile", "dockerfile", "go.mod", "package.json", "pyproject.toml", "pom.xml", "Cargo.toml")
COMPOSE_NAMES = {"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"}
SERVICE_ROOTS = {"services", "apps", "packages", "cmd"}
CODEOWNERS_PATHS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
SOURCE_SUFFIXES = (".py", ".go", ".js", ".ts", ".tsx", ".jsx", ".proto")
OBSERVED_AT = "2026-10-15T12:00:00Z"
MAX_REMOTE_SOURCES = 80
ROOT_SERVICE = "__repo__"
ROOT_SOURCE_DIRS = {"src", "lib", "app", "internal"}


class IngestError(Exception):
    pass


def parse_target(value: str) -> tuple[str, str]:
    text = value.strip().strip('"').strip("'")
    path = Path(text)
    if path.exists():
        return "path", str(path.resolve())
    repo = _parse_github(text)
    if repo:
        return "github", f"{repo[0]}/{repo[1]}"
    raise IngestError(
        "Give a GitHub URL (https://github.com/owner/name) or a folder on this machine."
    )


def ingest(value: str) -> tuple[MemoryGraph, dict]:
    """Build the structural graph for a repository. Does not invent a change."""
    from edl.progress import step as progress

    kind, target = parse_target(value)
    if kind == "path":
        progress("Detecting services / owners / tests in local tree")
        detected = detect_local(Path(target))
        detected["origin"] = {"kind": "path", "value": target}
    else:
        progress(f"Fetching GitHub tree for {target}")
        owner, name = target.split("/", 1)
        detected = detect_github(owner, name)
        detected["origin"] = {"kind": "github", "value": target}
    if not detected["services"]:
        raise IngestError(
            "No services showed up in that tree. Looked for a compose file, a Dockerfile, "
            "a project manifest (package.json, pyproject.toml, go.mod, Cargo.toml, pom.xml), "
            "and folders under services/, apps/, packages/, or cmd/."
        )
    progress(f"Found {len(detected['services'])} service(s); building graph")
    graph = build_graph(detected)
    overlay_notes: list[str] = []
    overlay = dict(detected.get("evidence_overlay") or {})
    if overlay:
        from edl.evidence_overlay import apply_to_graph

        overlay_notes = apply_to_graph(graph, overlay)
    origin = detected["origin"]
    if origin.get("kind") == "path":
        from edl.calls import add_python_calls

        progress("Indexing Python call edges (CALLS)")
        call_count = add_python_calls(graph, Path(origin["value"]))
        if call_count:
            overlay_notes.append(f"calls:{call_count}")
    from edl.ci_actions import enrich_overlay_with_actions

    progress("Checking GitHub Actions / evidence overlay (no invented CI)")
    overlay, action_notes = enrich_overlay_with_actions(overlay, origin)
    overlay_notes.extend(action_notes)
    if action_notes and overlay.get("ci"):
        # Re-apply CI onto the change later via overlay; graph deploys/incidents
        # already applied above from the file before Actions merge.
        pass
    summary = {
        "origin": origin,
        "service_count": len(detected["services"]),
        "dependency_count": len(detected["depends"]),
        "owner_rules": len(detected.get("codeowners") or []),
        "ci_configured": bool(detected.get("ci_configured") or (overlay.get("ci") or {}).get("configured")),
        "note": _note(detected),
        "evidence_overlay": overlay,
        "evidence_notes": overlay_notes,
    }
    return graph, summary


def detect_local(root: Path) -> dict:
    paths: list[str] = []
    compose_parts: list[dict] = []
    functions: dict[str, list[str]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name for name in dirnames if name not in SKIP_DIRS and (not name.startswith(".") or name == ".github")
        ]
        folder = Path(dirpath)
        for filename in filenames:
            rel = (folder / filename).relative_to(root).as_posix()
            paths.append(rel)
            file_path = folder / filename
            if filename in COMPOSE_NAMES:
                compose_parts.append(_compose_from_text(file_path.read_text(encoding="utf-8", errors="ignore")))
            if filename.endswith(SOURCE_SUFFIXES) and not _looks_like_test(rel):
                text = _read_limited(file_path)
                found = _scan_functions(filename, text)
                if found:
                    functions[rel] = found
    detected = _assemble(paths, _merge_compose(compose_parts), functions)
    _bind_repo_service(detected, root.name)
    detected["codeowners"] = _read_local_codeowners(root)
    detected["ci_configured"] = _ci_configured(paths)
    detected["test_paths"] = [path for path in paths if _looks_like_test(path)]
    from edl.evidence_overlay import load_overlay

    detected["evidence_overlay"] = load_overlay(root)
    return detected


def detect_github(owner: str, name: str) -> dict:
    meta = _github_json(f"https://api.github.com/repos/{owner}/{name}")
    branch = meta.get("default_branch") or "main"
    tree = _github_json(
        f"https://api.github.com/repos/{owner}/{name}/git/trees/{branch}?recursive=1"
    )
    paths = [item["path"] for item in tree.get("tree", []) if item.get("type") == "blob"]
    compose_parts = []
    for path in _compose_paths(paths):
        raw = _github_text_optional(f"https://raw.githubusercontent.com/{owner}/{name}/{branch}/{path}")
        if raw:
            compose_parts.append(_compose_from_text(raw))
    detected = _assemble(paths, _merge_compose(compose_parts), {})
    _bind_repo_service(detected, name)
    functions = _github_functions(owner, name, branch, paths, detected["services"])
    _merge_functions(detected["services"], functions)
    detected["truncated"] = bool(tree.get("truncated"))
    detected["license"] = (meta.get("license") or {}).get("spdx_id")
    detected["branch"] = branch
    detected["codeowners"] = _github_codeowners(owner, name, branch, paths)
    detected["ci_configured"] = _ci_configured(paths)
    detected["test_paths"] = [path for path in paths if _looks_like_test(path)]
    detected["evidence_overlay"] = {}
    return detected


def detect_from_paths(
    paths: list[str],
    compose: dict | None = None,
    functions: dict[str, list[str]] | None = None,
    codeowners: list[dict] | None = None,
    test_paths: list[str] | None = None,
    ci_configured: bool = False,
) -> dict:
    detected = _assemble(paths, compose, functions or {})
    detected["codeowners"] = codeowners or []
    detected["test_paths"] = test_paths or []
    detected["ci_configured"] = ci_configured
    return detected


def _assemble(paths: list[str], compose: dict | None, functions: dict[str, list[str]]) -> dict:
    services: dict[str, dict] = {}

    def ensure(name: str) -> dict:
        key = ROOT_SERVICE if name == ROOT_SERVICE else _slug(name)
        if not key or key in IGNORE_AS_SERVICE:
            return services.setdefault("_skip", {"name": "_skip", "paths": [], "functions": []})
        services.setdefault(key, {"name": key, "paths": [], "functions": []})
        return services[key]

    if compose:
        for name in compose:
            ensure(name)
        for name, deps in compose.items():
            ensure(name)
            for dep in deps:
                ensure(dep)

    for path in paths:
        parts = path.split("/")
        if any(part in IGNORE_AS_SERVICE for part in parts):
            continue
        base = parts[-1]
        if base in CODE_MARKERS or base == "Dockerfile":
            if len(parts) == 1:
                bucket = ensure(ROOT_SERVICE)
                bucket["root"] = True
            else:
                bucket = ensure(parts[-2])
            bucket["paths"].append(path)
        if len(parts) >= 2 and parts[-2] in SERVICE_ROOTS and "." not in parts[-1]:
            ensure(parts[-1])
        for index, part in enumerate(parts[:-1]):
            if part in SERVICE_ROOTS:
                child = parts[index + 1]
                if "." not in child:
                    ensure(child)["paths"].append(path)

    services.pop("_skip", None)
    # Always discover top-level code folders. A root pyproject.toml / package.json
    # must not hide packages such as edl/ or web/.
    top: dict[str, int] = {}
    for path in paths:
        parts = [part for part in path.split("/") if part and part not in IGNORE_AS_SERVICE]
        if len(parts) < 2:
            continue
        if parts[-1].endswith(SOURCE_SUFFIXES) and not _looks_like_test(path):
            top[parts[0]] = top.get(parts[0], 0) + 1
    for name, count in top.items():
        if count:
            ensure(name)
    root_files = [
        path
        for path in paths
        if "/" not in path and path.endswith(SOURCE_SUFFIXES) and not _looks_like_test(path)
    ]
    if root_files:
        bucket = ensure(ROOT_SERVICE)
        bucket["root"] = True
        for path in root_files:
            if path not in bucket["paths"]:
                bucket["paths"].append(path)

    ranked = sorted(
        (service for service in services.values() if service["name"] != "_skip"),
        key=lambda service: (0 if service["paths"] or service["functions"] else 1, service["name"]),
    )
    ordered = ranked[:60]
    for path, names in functions.items():
        service = _service_for_path(ordered, path)
        if service is None:
            continue
        if path not in service["paths"]:
            service["paths"].append(path)
        units = service.setdefault("function_units", [])
        seen = {(row["path"], row["name"]) for row in units}
        for name in names:
            key = (path, name)
            if key in seen:
                continue
            units.append({"name": name, "path": path})
            seen.add(key)
            if name not in service["functions"]:
                service["functions"].append(name)
    for service in ordered:
        # Keep path-bound units for every source file. Flat name list is a short index.
        units = service.get("function_units") or []
        service["function_units"] = units[:120]
        service["functions"] = [row["name"] for row in service["function_units"]]

    depends = []
    if compose:
        known = {service["name"] for service in ordered}
        for name, deps in compose.items():
            consumer = _slug(name)
            for dep in deps:
                dependency = _slug(dep)
                if consumer in known and dependency in known and consumer != dependency:
                    depends.append((consumer, dependency))
    return {"services": ordered, "depends": depends}


def build_graph(detected: dict) -> MemoryGraph:
    """Structural graph only. A Change is created later by edl.change scope."""
    graph = MemoryGraph()
    origin = detected.get("origin", {})
    repo_name = origin.get("value", "selected-repo")
    graph.add_node(
        {
            "id": "repo:selected",
            "label": "Repository",
            "name": repo_name,
            "source": "repository",
            "evidence_type": "observed",
            "method": "tree-scan",
            "observed_at": OBSERVED_AT,
            "valid_until": None,
            "confidence": 1,
        }
    )

    rules = detected.get("codeowners") or []
    test_paths = detected.get("test_paths") or []
    for service in detected["services"]:
        name = service["name"]
        sample_paths = [
            path
            for path in (service.get("paths") or [])
            if path.endswith(SOURCE_SUFFIXES) and not _looks_like_test(path)
        ][:8]
        graph.add_node(
            {
                "id": f"svc:{name}",
                "label": "Service",
                "name": name,
                "tier": None,
                "kind": "service",
                "source": "repository",
                "evidence_type": "observed",
                "method": "tree-scan",
                "observed_at": OBSERVED_AT,
                "confidence": 1,
                "root": bool(service.get("root")),
                "sample_paths": sample_paths,
            }
        )
        graph.add_edge(f"svc:{name}", "IN_REPO", "repo:selected", source="repository", evidence_type="observed")
        _attach_owners(graph, f"svc:{name}", _owners_for_service(service, rules))

        function_units = service.get("function_units") or [
            {"name": fname, "path": None} for fname in (service.get("functions") or [])
        ]
        if not function_units:
            function_units = [{"name": name, "path": None, "granularity": "service"}]
        service_tests = [path for path in test_paths if _service_for_path([service], path)]
        for unit in function_units:
            fname = unit["name"]
            granularity = unit.get("granularity") or "function"
            source_path = unit.get("path")
            symbol = name if granularity == "service" else f"{name}.{fname}"
            fid = f"fn:{symbol}"
            # Same symbol can appear in one file; keep the first path-bound node.
            if graph.node(fid) is not None:
                existing = graph.node(fid)
                if source_path and not existing.get("path"):
                    existing["path"] = source_path
                continue
            graph.add_node(
                {
                    "id": fid,
                    "label": "Function",
                    "name": symbol,
                    "service": name,
                    "fname": fname,
                    "path": source_path,
                    "granularity": granularity,
                    "source": "repository",
                    "evidence_type": "observed",
                    "method": "tree-scan" if granularity == "function" else "service-folder",
                    "observed_at": OBSERVED_AT,
                    "confidence": 1,
                }
            )
            graph.add_edge(fid, "PART_OF", f"svc:{name}", source="repository", evidence_type="observed")
            covering = [
                path
                for path in service_tests
                if source_path is None
                or Path(path).stem.replace("test_", "").replace("_test", "")
                == Path(source_path).stem
                or Path(source_path).stem in path
            ] or service_tests[:1]
            if covering:
                graph.add_node(
                    {
                        "id": f"test:{symbol}",
                        "label": "Test",
                        "name": covering[0],
                        "status": "not-run",
                        "source": "repository",
                        "evidence_type": "observed",
                        "method": "test-file",
                        "observed_at": OBSERVED_AT,
                        "confidence": 1,
                    }
                )
                graph.add_edge(f"test:{symbol}", "COVERS", fid, source="repository", evidence_type="observed")

    for consumer, dependency in detected["depends"]:
        graph.add_edge(
            f"svc:{consumer}",
            "DEPENDS_ON",
            f"svc:{dependency}",
            evidence_type="observed",
            source="repository",
        )
    return graph


def parse_codeowners(text: str) -> list[dict]:
    rules = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        owners = [part for part in parts[1:] if part.startswith("@")]
        if owners:
            rules.append({"pattern": parts[0], "owners": owners})
    return rules


def _attach_owners(graph: MemoryGraph, service_id: str, owners: list[str]) -> None:
    for handle in owners:
        raw = handle[1:] if handle.startswith("@") else handle
        if "/" in raw:
            node_id = "team:" + raw.replace("/", "-")
            label = "Team"
        else:
            node_id = "eng:" + raw
            label = "Engineer"
        if graph.node(node_id) is None:
            graph.add_node(
                {
                    "id": node_id,
                    "label": label,
                    "name": raw,
                    "source": "repository",
                    "evidence_type": "observed",
                    "method": "codeowners",
                    "observed_at": OBSERVED_AT,
                    "confidence": 1,
                }
            )
        graph.add_edge(
            node_id,
            "OWNS",
            service_id,
            observed_at=OBSERVED_AT,
            valid_until=None,
            evidence_type="observed",
            source="repository",
        )


def _owners_for_service(service: dict, rules: list[dict]) -> list[str]:
    paths = service.get("paths") or [service["name"]]
    winner: list[str] = []
    for rule in rules:
        if any(_rule_matches(rule["pattern"], path) for path in paths):
            winner = rule["owners"]
    return winner


def _rule_matches(pattern: str, path: str) -> bool:
    path = path.lstrip("/")
    if pattern == "*":
        return True
    token = pattern.lstrip("/")
    if token.endswith("/"):
        prefix = token
        return path.startswith(prefix) or path == prefix.rstrip("/")
    if "/" not in token:
        return token in path.split("/")
    return path == token or path.startswith(token.rstrip("/") + "/")


def _bind_repo_service(detected: dict, repo_name: str) -> None:
    slug = _slug(Path(str(repo_name)).name)
    services = detected["services"]
    root = next((service for service in services if service["name"] == ROOT_SERVICE), None)
    if root is None:
        return
    existing = next((service for service in services if service["name"] == slug), None)
    if existing is not None and existing is not root:
        for path in root["paths"]:
            if path not in existing["paths"]:
                existing["paths"].append(path)
        units = existing.setdefault("function_units", [])
        seen = {(row.get("path"), row["name"]) for row in units}
        for unit in root.get("function_units") or [
            {"name": fname, "path": None} for fname in root.get("functions") or []
        ]:
            key = (unit.get("path"), unit["name"])
            if key in seen:
                continue
            units.append(unit)
            seen.add(key)
            if unit["name"] not in existing["functions"]:
                existing["functions"].append(unit["name"])
        existing["root"] = True
        services.remove(root)
        return
    root["name"] = slug


def _note(detected: dict) -> str:
    named = sum(len(service["functions"]) for service in detected["services"])
    folders = sum(1 for service in detected["services"] if not service["functions"])
    text = (
        f"Found {len(detected['services'])} services and {named} function names. "
        f"{folders} services had no function name in the files that were read. "
        "A review needs a scoped change (git base/head, working tree, or explicit files). "
        "A test file is recorded as not-run. CI run results, deployment health, incidents, and tiers "
        "are not in the git tree, so they are left missing."
    )
    if detected.get("truncated"):
        text += " GitHub did not return the full file tree."
    return text


def _service_for_path(services: list[dict], path: str) -> dict | None:
    parts = path.split("/")
    partset = {_slug(part) for part in parts}
    matches = [
        service for service in services if service["name"] in partset and service["name"] != ROOT_SERVICE
    ]
    if matches:
        return sorted(matches, key=lambda service: len(service["name"]), reverse=True)[0]
    if len(services) == 1:
        return services[0]
    roots = [service for service in services if service.get("root")]
    if len(roots) == 1 and (len(parts) == 1 or parts[0] in ROOT_SOURCE_DIRS):
        return roots[0]
    return None


def _looks_like_test(path: str) -> bool:
    name = path.replace("\\", "/").split("/")[-1]
    parts = set(path.replace("\\", "/").split("/"))
    if parts & {"test", "tests", "__tests__"}:
        return True
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name.endswith("_test.go")
        or name.endswith(".test.js")
        or name.endswith(".test.ts")
        or name.endswith(".spec.js")
        or name.endswith(".spec.ts")
    )


def _ci_configured(paths: list[str]) -> bool:
    for path in paths:
        normalized = path.replace("\\", "/")
        if "/workflows/" in f"/{normalized}" and normalized.endswith((".yml", ".yaml")):
            return True
    return False


def _scan_functions(filename: str, text: str) -> list[str]:
    found: list[str] = []
    if filename.endswith(".py"):
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("async def "):
                stripped = "def " + stripped[len("async def ") :]
            if not stripped.startswith("def "):
                continue
            name = stripped[4:].split("(")[0].strip()
            if name and not name.startswith("_") and name not in found:
                found.append(name)
    elif filename.endswith(".go"):
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("func "):
                continue
            head = stripped[5:]
            if head.startswith("("):
                close = head.find(")")
                head = head[close + 1 :] if close != -1 else head
            name = head.split("(")[0].strip()
            if name and name[0].isupper() and name not in found:
                found.append(name)
    elif filename.endswith((".js", ".ts", ".tsx", ".jsx")):
        for line in text.splitlines():
            indented = line.startswith((" ", "\t"))
            stripped = line.strip()
            if stripped.startswith("export default "):
                stripped = stripped[len("export default ") :]
            elif stripped.startswith("export "):
                stripped = stripped[len("export ") :]
            _remember_js(found, stripped, indented)
    elif filename.endswith(".rs"):
        for line in text.splitlines():
            stripped = line.strip()
            for prefix in ("pub async fn ", "async fn ", "pub fn ", "fn "):
                if stripped.startswith(prefix):
                    name = stripped[len(prefix) :].split("(")[0].strip()
                    if name and not name.startswith("_") and name not in found:
                        found.append(name)
                    break
    elif filename.endswith(".proto"):
        for name in _proto_rpcs(text):
            if name not in found:
                found.append(name)
    return found[:8]


def _compose_from_text(text: str) -> dict[str, list[str]]:
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return {}
    block = data.get("services") if isinstance(data, dict) else None
    if not isinstance(block, dict):
        return {}
    parsed = {}
    for name, body in block.items():
        deps = []
        if isinstance(body, dict):
            raw = body.get("depends_on") or []
            if isinstance(raw, dict):
                deps = list(raw.keys())
            elif isinstance(raw, list):
                deps = [str(item) for item in raw]
        parsed[str(name)] = deps
    return parsed


def _proto_rpcs(text: str) -> list[str]:
    found = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("rpc "):
            name = stripped.split()[1].split("(")[0]
            if name and name not in found:
                found.append(name)
    return found


def _slug(name: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in name.strip())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-") or "service"


def _read_limited(path: Path, limit: int = 100_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:limit]
    except OSError:
        return ""


def _read_local_codeowners(root: Path) -> list[dict]:
    for rel in CODEOWNERS_PATHS:
        file_path = root / rel
        if file_path.is_file():
            return parse_codeowners(file_path.read_text(encoding="utf-8", errors="ignore"))
    return []


def _github_codeowners(owner: str, name: str, branch: str, paths: list[str]) -> list[dict]:
    for rel in CODEOWNERS_PATHS:
        if rel not in paths:
            continue
        text = _github_text_optional(f"https://raw.githubusercontent.com/{owner}/{name}/{branch}/{rel}")
        if text:
            return parse_codeowners(text)
    return []


def _merge_functions(services: list[dict], functions: dict[str, list[str]]) -> None:
    for path, names in functions.items():
        service = _service_for_path(services, path)
        if service is None:
            continue
        if path not in service["paths"]:
            service["paths"].append(path)
        units = service.setdefault("function_units", [])
        seen = {(row["path"], row["name"]) for row in units}
        for fname in names:
            key = (path, fname)
            if key in seen:
                continue
            units.append({"name": fname, "path": path})
            seen.add(key)
            if fname not in service["functions"]:
                service["functions"].append(fname)
        service["function_units"] = units[:120]
        service["functions"] = [row["name"] for row in service["function_units"]]


_JS_CLASS = re.compile(r"^class\s+([A-Za-z_$][\w$]*)")
_JS_FUNC = re.compile(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)")
_JS_ARROW = re.compile(
    r"^(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\b|\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"
)
_JS_METHOD = re.compile(r"^(?:async\s+)?([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{")
_JS_SKIP = {"if", "for", "while", "switch", "catch", "function", "return", "do", "else"}
_BORING_FILES = {"__init__.py", "setup.py", "setup_dev_env.py", "conftest.py", "wsgi.py"}
_INTERESTING_DIRS = {"src", "lib", "app", "views", "controllers", "command", "commands", "handlers", "api"}


def _remember_js(found: list[str], stripped: str, indented: bool) -> None:
    class_match = _JS_CLASS.match(stripped)
    if class_match:
        _append_symbol(found, class_match.group(1))
        return
    func_match = _JS_FUNC.match(stripped)
    if func_match:
        _append_symbol(found, func_match.group(1))
        return
    arrow_match = _JS_ARROW.match(stripped)
    if arrow_match:
        _append_symbol(found, arrow_match.group(1))
        return
    if not indented:
        return
    method_match = _JS_METHOD.match(stripped)
    if method_match and method_match.group(1) not in _JS_SKIP:
        _append_symbol(found, method_match.group(1))


def _append_symbol(found: list[str], name: str) -> None:
    if name and name not in found and not name.startswith("_"):
        found.append(name)


def _merge_compose(parts: list[dict]) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {}
    for part in parts:
        for name, deps in part.items():
            bucket = merged.setdefault(str(name), [])
            for dep in deps:
                if dep not in bucket:
                    bucket.append(dep)
    return merged


def _compose_paths(paths: list[str]) -> list[str]:
    found = [path for path in paths if path.rsplit("/", 1)[-1] in COMPOSE_NAMES]
    found.sort(key=lambda path: (path.count("/"), path))
    return found


def _source_rank(path: str) -> tuple:
    parts = path.split("/")
    name = parts[-1]
    boring = name in _BORING_FILES or name.startswith("__")
    interesting = any(part in _INTERESTING_DIRS for part in parts)
    kind = 0 if path.endswith(".py") else 1 if path.endswith((".js", ".jsx", ".ts", ".tsx")) else 2
    return (boring, not interesting, kind, path.count("/"), path)


def _skipped_tree_path(path: str) -> bool:
    if set(path.split("/")) & SKIP_DIRS:
        return True
    name = path.rsplit("/", 1)[-1]
    return name.endswith(".min.js") or name.endswith(".min.ts")


def _github_functions(
    owner: str, name: str, branch: str, paths: list[str], services: list[dict]
) -> dict[str, list[str]]:
    candidates = [
        path
        for path in paths
        if path.endswith(SOURCE_SUFFIXES)
        and not _looks_like_test(path)
        and not _skipped_tree_path(path)
        and _service_for_path(services, path) is not None
    ]
    by_service: dict[str, list[str]] = {}
    for path in candidates:
        service = _service_for_path(services, path)
        if service is None:
            continue
        by_service.setdefault(service["name"], []).append(path)
    chosen: list[str] = []
    for paths_for_service in by_service.values():
        paths_for_service.sort(key=_source_rank)
        chosen.extend(paths_for_service[:3])
    chosen = chosen[:MAX_REMOTE_SOURCES]

    def read_one(path: str) -> tuple[str, list[str]]:
        text = _github_text_optional(f"https://raw.githubusercontent.com/{owner}/{name}/{branch}/{path}")
        if not text:
            return path, []
        return path, _scan_functions(path.rsplit("/", 1)[-1], text)

    found: dict[str, list[str]] = {}
    if not chosen:
        return found
    workers = min(8, len(chosen))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for path, names in pool.map(read_one, chosen):
            if names:
                found[path] = names
    return found


def _parse_github(value: str) -> tuple[str, str] | None:
    text = value.strip().rstrip("/")
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    if text.startswith("github.com/"):
        text = text[len("github.com/") :]
    parts = [part for part in text.split("/") if part]
    if len(parts) >= 2 and "." not in parts[0] and "\\" not in value:
        return parts[0], parts[1].removesuffix(".git")
    return None


def _github_json(url: str) -> dict:
    raw = _github_text(url)
    return json.loads(raw)


def _github_text(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "syzygy-edl", "Accept": "application/vnd.github+json"},
    )
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise IngestError("GitHub could not find that repository. Check the owner and name.") from exc
        if exc.code == 403:
            raise IngestError(
                "GitHub refused the lookup (rate limit or a private repo). "
                "Set GITHUB_TOKEN, or choose a local folder instead."
            ) from exc
        raise IngestError(f"GitHub returned {exc.code} for that repository.") from exc
    except urllib.error.URLError as exc:
        raise IngestError(f"Could not reach GitHub ({exc.reason}). A local folder works offline.") from exc


def _github_text_optional(url: str) -> str | None:
    try:
        return _github_text(url)
    except IngestError:
        return None
