"""Product identity for the Syzygy CLI and human-facing reports."""

from __future__ import annotations

PRODUCT = "Syzygy"
PRODUCT_LONG = "Syzygy EDL"
TAGLINE = "Engineering Decision Layer - graph-backed change decisions on FalkorDB"
VERSION = "0.2.0"


def banner() -> str:
    return (
        f"{PRODUCT_LONG}  v{VERSION}\n"
        f"{TAGLINE}\n"
        "------------------------------------------------------------"
    )


def line(title: str) -> str:
    return f"\n> {title}"


def usage() -> str:
    return """Commands
  status                 Product + FalkorDB readiness
  ingest <repo>          Scan repo, publish graph to FalkorDB
  report <repo> --files  Publish + decide + explain + agent node pack
  review <repo> --files  Decision report only
  agent  <repo> --files  Graph tools (+ --llm for Gemini)
  notify <repo> --files  Draft/send owner notification from a decision
  pr-comment <repo> --pr N --files   Post decision onto a GitHub PR
  graph status|services|path|explain|evidence
  mcp [--check]          MCP server for Cursor / Claude / Codex
  llm-check              Verify Gemini / LLM settings
  serve                  Open the Syzygy UI

Examples
  python -m edl status
  python -m edl ingest https://github.com/owner/name
  python -m edl report . --files path/to/changed.py
  python -m edl agent . --files path/to/changed.py
  python -m edl notify . --files path/to/changed.py
  python -m edl pr-comment . --pr 42 --files path/to/changed.py --dry-run
  python -m edl serve

Tips
  Prefer .\\.venv\\Scripts\\python.exe -m edl  (Windows py can skip the venv)
  After: pip install -e .   you can run:  syzygy report . --files ...
  Real repos only: no sample fixtures load into the live UI or serve path.
  Long steps print progress on stderr (scan → publish → review → tools).
"""
