#!/usr/bin/env python3
"""Drop-in replacement for `claude -p` that drives the Claude Code TUI.

Accepts the same CLI interface as `claude -p` so OpenClaw (and anything
else expecting pipe-mode Claude) can call it directly. Reads prompt from
stdin, streams JSONL to stdout in `claude -p` format.
"""

import argparse
import asyncio
import json
import sys
import time
from typing import Any

from claude_tui.orchestrator import ClaudeREPL


def emit(obj: dict) -> None:
    print(json.dumps(obj), flush=True)


def _extract_text(raw: dict[str, Any]) -> str:
    """Pull plain text from an assistant message's content blocks."""
    msg = raw.get("message", {})
    parts = []
    for block in msg.get("content", []):
        if block.get("type") == "text":
            parts.append(block["text"])
    return "".join(parts)


async def _run() -> None:
    parser = argparse.ArgumentParser(description="claude -p compatible wrapper over Claude Code TUI.")
    parser.add_argument("query", nargs="?", default=None, help="Message to send (or read from stdin)")
    parser.add_argument("--timeout", type=float, default=120)

    # claude -p compatible flags (accepted, mostly ignored)
    parser.add_argument("-p", action="store_true")
    parser.add_argument("--output-format", default=None)
    parser.add_argument("--include-partial-messages", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--permission-mode", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--append-system-prompt", default=None)
    parser.add_argument("--resume", default=None)
    parser.add_argument("--mcp-config", default=None)
    parser.add_argument("--strict-mcp-config", action="store_true")

    args = parser.parse_args()

    prompt = args.query
    if not prompt and not sys.stdin.isatty():
        prompt = sys.stdin.read().strip()
    if not prompt:
        print("Error: no prompt provided (pass as argument or via stdin)", file=sys.stderr)
        sys.exit(1)

    t0 = time.monotonic()

    async with ClaudeREPL(timeout=args.timeout, server_names=[], resume=args.resume) as repl:
        # 1. Init line
        emit({
            "type": "system",
            "subtype": "init",
            "session_id": repl.session_id,
            "model": args.model or "",
            "tools": [],
            "mcp_servers": [],
            "permissionMode": args.permission_mode or "default",
            "claude_code_version": "claude-tui",
            "uuid": repl.session_id,
        })

        # 2. Stream — forward assistant messages, collect final text
        final_text = ""
        async for raw in repl.query_stream_raw(prompt):
            t = raw.get("type")
            if t == "assistant":
                emit({
                    "type": "assistant",
                    "message": raw.get("message", {}),
                    "session_id": repl.session_id,
                    "uuid": raw.get("uuid", ""),
                })
                final_text = _extract_text(raw) or final_text

        # 3. Result line
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        emit({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": elapsed_ms,
            "num_turns": 1,
            "result": final_text,
            "stop_reason": "end_turn",
            "session_id": repl.session_id,
            "total_cost_usd": 0,
            "usage": {},
            "terminal_reason": "completed",
            "uuid": repl.session_id,
        })


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
