#!/usr/bin/env python3
"""Minimal stdio MCP fixture server for local observer evaluations."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from local_observer_harness import (
    FixtureGitHubMCP,
    FixtureMiss,
    FixtureResponseError,
)


SAFE_OUTPUT_TOOLS = (
    "noop",
    "missing_tool",
    "missing_data",
    "report_incomplete",
    "create_pull_request",
    "add_comment",
)


class SafeOutputMCP:
    def __init__(self, capture_path: Path, allowed_tools: set[str] | None = None) -> None:
        self.capture_path = capture_path
        self.allowed_tools = set(SAFE_OUTPUT_TOOLS) if allowed_tools is None else allowed_tools
        unknown = self.allowed_tools - set(SAFE_OUTPUT_TOOLS)
        if unknown:
            raise ValueError(f"unknown safe-output tools: {sorted(unknown)}")

    def list_tools(self) -> list[dict[str, object]]:
        return [
            {
                "name": name,
                "description": (
                    "Capture this safe-output intent locally. It never writes to GitHub."
                ),
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": True,
                },
            }
            for name in SAFE_OUTPUT_TOOLS if name in self.allowed_tools
        ]

    def call(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        if name not in self.allowed_tools:
            raise FixtureMiss(f"safe-output tool is not available: {name}")
        item = {
            "tool": name,
            "arguments": arguments,
            "observed_time": datetime.now(timezone.utc).isoformat(),
        }
        self.capture_path.parent.mkdir(parents=True, exist_ok=True)
        with self.capture_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(item, sort_keys=True) + "\n")
        return {"captured": True, "tool": name}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("github", "safeoutputs"), default="github")
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--calls-out", type=Path)
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--tools", help="comma-separated tool allowlist")
    args = parser.parse_args()
    if args.mode == "github":
        if args.fixture is None:
            parser.error("--fixture is required for github mode")
        server = FixtureGitHubMCP(
            json.loads(args.fixture.read_text(encoding="utf-8")),
            calls_path=args.calls_out,
            allowed_tools=(
                set(filter(None, args.tools.split(",")))
                if args.tools
                else None
            ),
        )
    else:
        if args.capture is None:
            parser.error("--capture is required for safeoutputs mode")
        server = SafeOutputMCP(
            args.capture,
            allowed_tools=set(filter(None, args.tools.split(","))) if args.tools else None,
        )

    for line in sys.stdin:
        request = json.loads(line)
        method = request.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "observer-fixture", "version": "1"},
            }
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            result = {"tools": server.list_tools()}
        elif method == "tools/call":
            params = request.get("params", {})
            try:
                value = server.call(params["name"], params.get("arguments", {}))
                result = {
                    "content": [
                        {"type": "text", "text": json.dumps(value, sort_keys=True)}
                    ],
                }
            except (FixtureMiss, FixtureResponseError, KeyError, ValueError) as error:
                result = {
                    "isError": True,
                    "content": [{"type": "text", "text": str(error)}],
                }
        else:
            result = {"error": {"code": -32601, "message": f"unsupported method: {method}"}}
        if "id" in request:
            print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
