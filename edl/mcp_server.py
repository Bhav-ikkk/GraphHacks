"""MCP stdio server exposing Syzygy graph tools.

Speak JSON-RPC 2.0 with Content-Length framing (MCP). Tools call FalkorDB-backed
named reads — never free-text Cypher from the client.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, TextIO

from edl.engine import Engine
from edl.tools import ToolContext, call_tool, list_tools

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "policies" / "policies.yaml"
LOG = ROOT / "logs" / "mcp.jsonl"
PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "syzygy-edl", "version": "0.2.0"}


class McpServer:
    def __init__(self, engine: Engine | None = None) -> None:
        self.engine = engine or Engine(POLICY, LOG)
        self.ctx = ToolContext(self.engine)
        self._initialized = False

    def handle(self, message: dict) -> dict | None:
        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params") or {}

        if method == "initialize":
            self._initialized = True
            return _result(
                msg_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                },
            )
        if method == "notifications/initialized":
            return None
        if method == "ping":
            return _result(msg_id, {})
        if method == "tools/list":
            tools = []
            for spec in list_tools():
                tools.append(
                    {
                        "name": spec["name"],
                        "description": spec["description"],
                        "inputSchema": spec["inputSchema"],
                    }
                )
            return _result(msg_id, {"tools": tools})
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if not name:
                return _error(msg_id, -32602, "tools/call requires name")
            payload = call_tool(self.ctx, str(name), arguments if isinstance(arguments, dict) else {})
            text = json.dumps(payload, indent=2, default=str)
            is_error = not payload.get("ok", True)
            return _result(
                msg_id,
                {
                    "content": [{"type": "text", "text": text}],
                    "structuredContent": payload,
                    "isError": is_error,
                },
            )
        if msg_id is None:
            return None
        return _error(msg_id, -32601, f"Method not found: {method}")


def serve_stdio(stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
    """Block on stdin and answer MCP messages until EOF."""
    server = McpServer()
    inp = stdin or sys.stdin
    out = stdout or sys.stdout
    while True:
        message = _read_message(inp)
        if message is None:
            break
        response = server.handle(message)
        if response is not None:
            _write_message(out, response)


def _read_message(inp: TextIO) -> dict | None:
    """Read one MCP message (Content-Length framed or a single JSON line)."""
    # Peek-style: if first line looks like Content-Length, use framing.
    line = inp.readline()
    if line == "":
        return None
    if line.lower().startswith("content-length:"):
        try:
            length = int(line.split(":", 1)[1].strip())
        except ValueError:
            return None
        # consume headers until blank line
        while True:
            header = inp.readline()
            if header in ("", "\n", "\r\n"):
                break
        body = inp.read(length)
        if not body:
            return None
        return json.loads(body)
    # newline-delimited JSON (handy for manual tests)
    text = line.strip()
    if not text:
        return _read_message(inp)
    return json.loads(text)


def _write_message(out: TextIO, message: dict) -> None:
    body = json.dumps(message, ensure_ascii=False, default=str)
    raw = body.encode("utf-8")
    out.write(f"Content-Length: {len(raw)}\r\n\r\n")
    out.flush()
    # write bytes through buffer if possible
    buffer = getattr(out, "buffer", None)
    if buffer is not None:
        buffer.write(raw)
        buffer.flush()
    else:
        out.write(body)
        out.flush()


def _result(msg_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def main() -> None:
    serve_stdio()


if __name__ == "__main__":
    main()
