"""Turn a repository the user chooses into a decision graph.

Only facts found in that tree are stored: services, compose dependencies,
CODEOWNERS, test files, workflow files, and function names from source.
CI run results, deployment health, incidents, and tiers are not invented.
Nothing here pins a demo repository.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
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
IGNORE_AS_SERVICE = SKIP_DIRS | {".github"}
CODE_MARKERS = ("Dockerfile", "dockerfile", "go.mod", "package.json", "pyproject.toml", "pom.xml", "Cargo.toml")
SERVICE_ROOTS = {"services", "apps", "packages", "cmd"}
CODEOWNERS_PATHS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
SOURCE_SUFFIXES = (".py", ".go", ".js", ".ts", ".tsx", ".jsx", ".proto")
OBSERVED_AT = "2026-10-15T12:00:00Z"
MAX_REMOTE_SOURCES = 12


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


def ingest(value: str) -> tuple[MemoryGraph, list[dict], dict]:
    kind, target = parse_target(value)
    if kind == "path":
        detected = detect_local(Path(target))
        detected["origin"] = {"kind": "path", "value": target}
    else:
        owner, name = target.split("/", 1)
        detected = detect_github(owner, name)
        detected["origin"] = {"kind": "github", "value": target}
    if not detected["services"]:
        raise IngestError(
            "No services showed up in that tree. Looked for a compose file, Dockerfiles, "
            "and folders under services/, apps/, packages/, or cmd/."
        )
    graph, changes = build_graph(detected)
    summary = {
        "origin": detected["origin"],
        "service_count": len(detected["services"]),
        "dependency_count": len(detected["depends"]),
        "owner_rules": len(detected.get("codeowners") or []),
        "ci_configured": bool(detected.get("ci_configured")),
        "note": (
            "Services, dependencies, CODEOWNERS, test files, and function names come from the repository. "
            "A test file is recorded as not-run. CI run results, deployment health, incidents, and tiers "
            "are not in the git tree, so they are left missing."
        ),
    }
    return graph, changes, summary


def detect_local(root: Path) -> dict:
    paths: list[str] = []
    compose = None
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
            if filename in {"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"} and compose is None:
                compose = _compose_from_text(file_path.read_text(encoding="utf-8", errors="ignore"))
            if filename.endswith(SOURCE_SUFFIXES) and not _looks_like_test(rel):
                text = _read_limited(file_path)
                found = _scan_functions(filename, text)
                if found:
                    functions[rel] = found
    detected = _assemble(paths, compose, functions)
    detected["codeowners"] = _read_local_codeowners(root)
    detected["ci_configured"] = _ci_configured(paths)
    detected["test_paths"] = [path for path in paths if _looks_like_test(path)]
    return detected


def detect_github(owner: str, name: str) -> dict:
    meta = _github_json(f"https://api.github.com/repos/{owner}/{name}")
    branch = meta.get("default_branch") or "main"
    tree = _github_json(
        f"https://api.github.com/repos/{owner}/{name}/git/trees/{branch}?recursive=1"
    )
    paths = [item["path"] for item in tree.get("tree", []) if item.get("type") == "blob"]
    compose = None
    for path in paths:
        base = path.rsplit("/", 1)[-1]
        if base in {"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"}:
            raw = _github_text(f"https://raw.githubusercontent.com/{owner}/{name}/{branch}/{path}")
            compose = _compose_from_text(raw)
            break
    detected = _assemble(paths, compose, {})
    functions = _github_functions(owner, name, branch, paths, detected["services"])
    _merge_functions(detected["services"], functions)
    detected["license"] = (meta.get("license") or {}).get("spdx_id")
    detected["branch"] = branch
    detected["codeowners"] = _github_codeowners(owner, name, branch, paths)
    detected["ci_configured"] = _ci_configured(paths)
    detected["test_paths"] = [path for path in paths if _looks_like_test(path)]
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
        key = _slug(name)
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
            parent = parts[-2] if len(parts) > 1 else _slug(base)
            ensure(parent)["paths"].append(path)
        if len(parts) >= 2 and parts[-2] in SERVICE_ROOTS and "." not in parts[-1]:
            ensure(parts[-1])
        for index, part in enumerate(parts[:-1]):
            if part in SERVICE_ROOTS:
                child = parts[index + 1]
                if "." not in child:
                    ensure(child)["paths"].append(path)

    services.pop("_skip", None)
    if not services:
        top: dict[str, int] = {}
        for path in paths:
            parts = [part for part in path.split("/") if part and part not in IGNORE_AS_SERVICE]
            if len(parts) < 2:
                continue
            if parts[-1].endswith(SOURCE_SUFFIXES):
                top[parts[0]] = top.get(parts[0], 0) + 1
        for name, count in top.items():
            if count:
                ensure(name)

    ordered = [service for service in list(services.values())[:40] if service["name"] != "_skip"]
    for path, names in functions.items():
        service = _service_for_path(ordered, path)
        if service is None:
            continue
        if path not in service["paths"]:
            service["paths"].append(path)
        for name in names:
            if name not in service["functions"]:
                service["functions"].append(name)
    for service in ordered:
        service["functions"] = service["functions"][:8]

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


def build_graph(detected: dict) -> tuple[MemoryGraph, list[dict]]:
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
    ci_configured = bool(detected.get("ci_configured"))
    changes = []
    for service in detected["services"]:
        name = service["name"]
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
            }
        )
        graph.add_edge(f"svc:{name}", "IN_REPO", "repo:selected", source="repository", evidence_type="observed")
        _attach_owners(graph, f"svc:{name}", _owners_for_service(service, rules))

        units = service["functions"] or [name]
        granularities = ["function"] * len(service["functions"]) or ["service"]
        service_tests = [path for path in test_paths if _service_for_path([service], path)]
        for fname, granularity in zip(units, granularities):
            symbol = name if granularity == "service" else f"{name}.{fname}"
            fid = f"fn:{symbol}"
            graph.add_node(
                {
                    "id": fid,
                    "label": "Function",
                    "name": symbol,
                    "service": name,
                    "fname": fname,
                    "granularity": granularity,
                    "source": "repository",
                    "evidence_type": "observed",
                    "method": "tree-scan" if granularity == "function" else "service-folder",
                    "observed_at": OBSERVED_AT,
                    "confidence": 1,
                }
            )
            graph.add_edge(fid, "PART_OF", f"svc:{name}", source="repository", evidence_type="observed")
            if service_tests:
                graph.add_node(
                    {
                        "id": f"test:{symbol}",
                        "label": "Test",
                        "name": service_tests[0],
                        "status": "not-run",
                        "source": "repository",
                        "evidence_type": "observed",
                        "method": "test-file",
                        "observed_at": OBSERVED_AT,
                        "confidence": 1,
                    }
                )
                graph.add_edge(f"test:{symbol}", "COVERS", fid, source="repository", evidence_type="observed")
            note = None
            if granularity == "service":
                note = "No function names were found. This review is of the service folder."
            changes.append(
                {
                    "id": f"repo:{symbol}",
                    "title": symbol,
                    "symbols": [symbol],
                    "files": service["paths"][:5] or [f"{name}/"],
                    "description": f"Review {symbol} from the repository you selected.",
                    "ci": {"present": False, "result": None, "age_hours": None, "configured": ci_configured},
                    "designated_approval": False,
                    "approver": None,
                    "mapping": "resolved",
                    "dependency": None,
                    "unresolved_refs": [],
                    "text_mentions_incident": False,
                    "intent": None,
                    "authorized": True,
                    "ticketing": "ok",
                    "note": note,
                }
            )

    for consumer, dependency in detected["depends"]:
        graph.add_edge(
            f"svc:{consumer}",
            "DEPENDS_ON",
            f"svc:{dependency}",
            evidence_type="observed",
            source="repository",
        )
    return graph, changes


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


def _service_for_path(services: list[dict], path: str) -> dict | None:
    parts = set(path.split("/"))
    matches = [service for service in services if service["name"] in parts]
    if not matches:
        return None
    return sorted(matches, key=lambda service: len(service["name"]), reverse=True)[0]


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
            stripped = line.strip()
            if not stripped.startswith("function "):
                continue
            name = stripped[len("function ") :].split("(")[0].strip()
            if name and name not in found:
                found.append(name)
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
        for fname in names:
            if fname not in service["functions"]:
                service["functions"].append(fname)
        service["functions"] = service["functions"][:8]


def _github_functions(
    owner: str, name: str, branch: str, paths: list[str], services: list[dict]
) -> dict[str, list[str]]:
    candidates = [
        path
        for path in paths
        if path.endswith(SOURCE_SUFFIXES)
        and not _looks_like_test(path)
        and ".github/" not in path
        and _service_for_path(services, path) is not None
    ]
    found: dict[str, list[str]] = {}
    for path in candidates[:MAX_REMOTE_SOURCES]:
        text = _github_text_optional(f"https://raw.githubusercontent.com/{owner}/{name}/{branch}/{path}")
        if not text:
            continue
        names = _scan_functions(path.rsplit("/", 1)[-1], text)
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
