#!/usr/bin/env python3
"""Programmatic interface to Claude Code CLI for batch evals.

Write channel: pty fd (keystrokes into the TUI) — fire and forget.
Read channel:  session JSONL file (~/.claude/projects/…/{session_id}.jsonl)

    async with ClaudeREPL() as repl:
        await repl.send_skill("reduck-mcp")
        messages = await repl.query("Take a screenshot")

CC JSONL format insights:
- CC splits a single LLM response into multiple JSONL lines: thinking, text,
  tool_use — each with stop_reason=None except (sometimes) the last.
- stop_reason=end_turn is unreliable: CC sometimes writes None for the final
  assistant message.
- type=progress with hookEvent=Stop is the completion signal.  However,
  agent_progress events appear MID-TURN while a subagent runs — these must
  be excluded from completion detection.
- Closing the pty master fd sends SIGHUP to the child — the process must stay
  alive until the turn completes. close() sends /exit then kills.
- Watermark (line offset into JSONL) scopes each wait to the current turn.
  After completion, watermark is set via _count_lines() (not watermark +
  len(new_lines)) to capture trailing metadata (system, file-history-snapshot).
- The saw_user gate prevents a previous turn's progress from triggering the
  current turn's completion: progress from the skill turn appears before the
  query's user message in the file, so saw_user is still False.
"""

import asyncio
import fcntl
import json
import os
import pty
import select
import struct
import subprocess
import tempfile
import termios
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECTS_DIR = Path.home() / ".claude" / "projects"
FOCUS_IN = b"\x1b[I"
POLL_INTERVAL = 0.15


def _build_mcp_config(server_names: list[str], cwd: Path) -> dict[str, Any]:
    """Pick servers from .mcp.json by name. Single source of truth."""
    if not server_names:
        return {"mcpServers": {}}
    mcp_path = cwd / ".mcp.json"
    if not mcp_path.exists():
        raise FileNotFoundError(f"No .mcp.json found in {cwd}")
    project_mcp = json.loads(mcp_path.read_text())
    all_servers = project_mcp["mcpServers"]
    missing = set(server_names) - set(all_servers)
    if missing:
        raise ValueError(f"MCP servers not found in .mcp.json: {missing}")
    servers = {k: all_servers[k] for k in server_names}
    return {"mcpServers": servers}


KEYS: dict[str, bytes] = {
    "enter": b"\r",
    "escape": b"\x1b",
    "tab": b"\t",
    "shift+tab": b"\x1b[Z",
    "ctrl+c": b"\x03",
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "backspace": b"\x7f",
}


# -- Message types -------------------------------------------------------------


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: str
    type: str = "tool_result"


@dataclass
class AssistantMessage:
    content: list[TextBlock | ToolUseBlock]
    model: str = ""
    stop_reason: str = ""
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResultMessage:
    content: list[ToolResultBlock]


@dataclass
class ResultMessage:
    duration_ms: int = 0
    num_turns: int = 0
    session_id: str = ""
    total_cost_usd: float = 0.0


Message = AssistantMessage | ToolResultMessage | ResultMessage


# -- Parsing -------------------------------------------------------------------


def _parse_assistant(raw: dict[str, Any]) -> AssistantMessage:
    msg = raw["message"]
    blocks: list[TextBlock | ToolUseBlock] = []
    for b in msg.get("content", []):
        if b.get("type") == "text":
            blocks.append(TextBlock(text=b["text"]))
        elif b.get("type") == "tool_use":
            blocks.append(
                ToolUseBlock(id=b["id"], name=b["name"], input=b.get("input", {}))
            )
    return AssistantMessage(
        content=blocks,
        model=msg.get("model", ""),
        stop_reason=msg.get("stop_reason", ""),
        usage=msg.get("usage", {}),
    )


def _parse_tool_result(raw: dict[str, Any]) -> ToolResultMessage:
    msg = raw["message"]
    blocks: list[ToolResultBlock] = []
    for b in msg.get("content", []):
        if b.get("type") == "tool_result":
            content = b.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    p.get("text", "") for p in content if p.get("type") == "text"
                )
            blocks.append(
                ToolResultBlock(tool_use_id=b.get("tool_use_id", ""), content=content)
            )
    return ToolResultMessage(content=blocks)


def _parse_result(raw: dict[str, Any]) -> ResultMessage:
    return ResultMessage(
        duration_ms=raw.get("duration_ms", 0),
        num_turns=raw.get("num_turns", 0),
        session_id=raw.get("session_id", ""),
        total_cost_usd=raw.get("total_cost_usd", 0.0),
    )


def _parse_line(raw: dict[str, Any]) -> Message | None:
    t = raw.get("type")
    if t == "assistant":
        return _parse_assistant(raw)
    if t == "user" and isinstance(raw.get("message", {}).get("content"), list):
        has_tool_result = any(
            b.get("type") == "tool_result" for b in raw["message"]["content"]
        )
        if has_tool_result:
            return _parse_tool_result(raw)
    if t == "result":
        return _parse_result(raw)
    return None


def _is_done(raw: dict[str, Any]) -> bool:
    """True if this JSONL line signals turn completion.

    Primary signal: type=progress with hookEvent=Stop (written after every
    completed turn).  agent_progress is an intermediate signal emitted while
    a subagent is still running and must be excluded.
    Fallback: stop_reason=end_turn on a non-tool-use assistant message,
    because CC sometimes omits the progress line after skill turns.
    """
    if raw.get("type") == "result":
        return True
    if raw.get("type") == "progress":
        data = raw.get("data", {})
        # agent_progress is mid-turn — subagent still running
        if data.get("type") == "agent_progress":
            return False
        return True
    if raw.get("type") == "assistant":
        msg = raw.get("message", {})
        if msg.get("stop_reason") == "end_turn":
            has_tool_use = any(
                b.get("type") == "tool_use" for b in msg.get("content", [])
            )
            if not has_tool_use:
                return True
    return False


def _is_user_message(raw: dict[str, Any]) -> bool:
    return raw.get("type") == "user"


# -- Path encoding -------------------------------------------------------------


def encode_project_path(path: Path) -> str:
    return str(path.resolve()).replace("/", "-").replace(".", "-")


# -- REPL ----------------------------------------------------------------------


class ClaudeREPL:
    """Programmatic handle to a Claude Code TUI session.

    All synchronization goes through the JSONL session file.
    The pty is a write-only channel — fire keystrokes and forget.
    """

    def __init__(
        self,
        timeout: float = 120,
        session_id: str | None = None,
        workdir: Path | None = None,
        env: dict[str, str] | None = None,
        server_names: list[str] | None = None,
        resume: str | None = None,
    ):
        self.timeout = timeout
        self.session_id = resume or session_id or str(uuid.uuid4())
        self._resume = resume
        self._workdir = (workdir or Path.cwd()).resolve()
        self._byte_offset = 0

        # Write MCP config to temp file (auto-cleaned on process exit)
        mcp_cfg = _build_mcp_config(server_names or [], self._workdir)
        self._mcp_tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", prefix="mcp_", delete=False
        )
        json.dump(mcp_cfg, self._mcp_tmp)
        self._mcp_tmp.close()

        # Spawn pty
        child_env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("CLAUDE_REPL", "CLAUDECODE")
        }
        child_env["TERM"] = "xterm-256color"
        if env:
            child_env.update(env)

        self._master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))

        cmd = ["claude"]
        if self._resume:
            cmd += ["--resume", self._resume]
        else:
            cmd += ["--session-id", self.session_id]
        cmd += [
            "--dangerously-skip-permissions",
            "--mcp-config",
            self._mcp_tmp.name,
            "--strict-mcp-config",
        ]

        self._proc = subprocess.Popen(
            cmd,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
            cwd=self._workdir,
            env=child_env,
        )
        os.close(slave)

    @property
    def session_path(self) -> Path:
        project_dir = encode_project_path(self._workdir)
        return PROJECTS_DIR / project_dir / f"{self.session_id}.jsonl"

    # -- Pty helpers (write-only) ----------------------------------------------

    def _type_text(self, text: str) -> None:
        """Inject characters into the pty synchronously."""
        for c in text:
            os.write(self._master, c.encode())

    def _drain_pty(self) -> None:
        """Consume pty output so the buffer doesn't block."""
        while True:
            ready, _, _ = select.select([self._master], [], [], 0)
            if not ready:
                break
            try:
                if not os.read(self._master, 4096):
                    break
            except OSError:
                break

    # -- JSONL tail (read-only, source of truth) --------------------------------

    async def _tail_jsonl(self, timeout: float) -> AsyncIterator[dict[str, Any]]:
        """Yield new JSONL objects as they're appended to the session file.

        Seeks to byte offset and reads forward — O(new bytes) per poll,
        not O(total file). Deadline resets on every new line (activity-based).
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout

        while not self.session_path.exists():
            if loop.time() > deadline:
                raise TimeoutError("Session file never appeared")
            self._drain_pty()
            await asyncio.sleep(0.2)

        with open(self.session_path) as f:
            f.seek(self._byte_offset)
            buf = ""
            while loop.time() < deadline:
                chunk = f.read()
                if chunk:
                    buf += chunk
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        if line.strip():
                            self._byte_offset = f.tell() - len(buf.encode())
                            deadline = loop.time() + timeout
                            yield json.loads(line)
                else:
                    self._drain_pty()
                    await asyncio.sleep(POLL_INTERVAL)

        raise TimeoutError(f"No activity for {timeout:.0f}s")

    # -- Public API ------------------------------------------------------------

    async def _collect_turn(self, timeout: float) -> list[Message]:
        """Collect all messages until the turn completes."""
        saw_user = False
        msgs: list[Message] = []
        async for raw in self._tail_jsonl(timeout):
            if _is_user_message(raw):
                saw_user = True
            if not saw_user:
                continue
            msg = _parse_line(raw)
            if msg:
                msgs.append(msg)
            if _is_done(raw):
                return msgs
        return msgs

    async def send_skill(
        self, name: str, timeout: float | None = None
    ) -> list[Message]:
        """Load a slash command. Returns messages from the skill acknowledgment turn."""
        timeout = timeout or self.timeout
        text = name if name.startswith("/") else f"/{name}"

        self._type_text(text)
        await asyncio.sleep(0.05)
        os.write(self._master, KEYS["tab"])
        await asyncio.sleep(0.3)
        os.write(self._master, KEYS["enter"])

        return await self._collect_turn(timeout)

    async def query_stream_raw(
        self, text: str, timeout: float | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Send a message and yield raw JSONL dicts as they arrive."""
        timeout = timeout or self.timeout

        self._type_text(text)
        await asyncio.sleep(0.05)
        os.write(self._master, KEYS["enter"])

        saw_user = False
        async for raw in self._tail_jsonl(timeout):
            if _is_user_message(raw):
                saw_user = True
            if saw_user:
                yield raw
            if saw_user and _is_done(raw):
                return

    async def query_stream(
        self, text: str, timeout: float | None = None
    ) -> AsyncIterator[Message]:
        """Send a message and yield parsed messages as they arrive."""
        async for raw in self.query_stream_raw(text, timeout):
            msg = _parse_line(raw)
            if msg:
                yield msg

    async def query(self, text: str, timeout: float | None = None) -> list[Message]:
        """Send a message and wait for completion. Returns all messages from the turn."""
        return [msg async for msg in self.query_stream(text, timeout)]

    # -- Lifecycle -------------------------------------------------------------

    def _read_pty_output(self) -> str:
        """Read all available PTY output as a string."""
        chunks: list[bytes] = []
        while True:
            ready, _, _ = select.select([self._master], [], [], 0)
            if not ready:
                break
            try:
                data = os.read(self._master, 4096)
                if not data:
                    break
                chunks.append(data)
            except OSError:
                break
        return b"".join(chunks).decode("utf-8", errors="replace")

    def _accept_dialog(self, output: str) -> bool:
        """Detect and accept a TUI confirmation dialog. Returns True if handled."""
        # Bypass permissions dialog: "Yes, I accept" is option 2 → down + enter
        if "Yes" in output and ("Bypass" in output or "dangerous" in output.lower()):
            os.write(self._master, KEYS["down"])
            os.write(self._master, KEYS["enter"])
            return True
        # Workspace trust dialog: "Yes, I trust this folder" is option 1 → enter
        if "trust" in output.lower() and "Yes" in output:
            os.write(self._master, KEYS["enter"])
            return True
        return False

    async def start(self) -> None:
        """Wait for TUI startup, accept any startup dialogs, and activate input."""
        await asyncio.sleep(2)

        # Handle up to 3 sequential dialogs (permissions, workspace trust, etc.)
        for _ in range(3):
            output = self._read_pty_output()
            if not self._accept_dialog(output):
                break
            await asyncio.sleep(3)

        # Wait for TUI to be fully ready
        await asyncio.sleep(2)
        self._drain_pty()
        os.write(self._master, FOCUS_IN)
        await asyncio.sleep(0.2)
        self._drain_pty()
        if self.session_path.exists():
            self._byte_offset = self.session_path.stat().st_size

    async def close(self) -> None:
        """Shut down the Claude process and close the pty."""
        try:
            os.write(self._master, b"/exit\r")
            await asyncio.sleep(2)
        except OSError:
            pass
        self._proc.kill()
        try:
            os.close(self._master)
        except OSError:
            pass
        try:
            os.unlink(self._mcp_tmp.name)
        except OSError:
            pass

    async def __aenter__(self) -> "ClaudeREPL":
        await self.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
