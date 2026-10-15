"""Load .env and read FalkorDB settings. The password never leaves this process."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"


def load_env(path: Path | None = None) -> None:
    file_path = path or ENV_PATH
    if not file_path.exists():
        return
    for raw in file_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_env()


@dataclass(frozen=True)
class FalkorSettings:
    host: str
    port: int
    username: str
    password: str
    graph: str
    browser: str

    @property
    def configured(self) -> bool:
        return bool(self.host or self.browser)


def falkor_settings() -> FalkorSettings:
    host = os.environ.get("FALKORDB_HOST", "").strip()
    port_text = os.environ.get("FALKORDB_PORT", "6379").strip() or "6379"
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError(f"FALKORDB_PORT must be a number, got {port_text!r}") from exc
    graph = os.environ.get("FALKORDB_GRAPH", "edl").strip() or "edl"
    browser = os.environ.get("FALKORDB_BROWSER", "").strip().rstrip("/")
    if not browser and host and host not in {"localhost", "127.0.0.1"}:
        browser = f"http://{host}:3000"
    return FalkorSettings(
        host=host,
        port=port,
        username=os.environ.get("FALKORDB_USERNAME", "").strip(),
        password=os.environ.get("FALKORDB_PASSWORD", ""),
        graph=graph,
        browser=browser,
    )
