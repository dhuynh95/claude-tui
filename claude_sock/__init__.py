"""claude-sock — sockpuppet for Claude Code."""

import sys

if sys.platform == "win32":
    raise ImportError(
        "claude-sock requires a Unix pty and does not support Windows. "
        "Use WSL or a Linux/macOS environment."
    )

from claude_sock.orchestrator import (
    AssistantMessage,
    ClaudeREPL,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolResultMessage,
    ToolUseBlock,
)

__all__ = [
    "ClaudeREPL",
    "AssistantMessage",
    "ResultMessage",
    "TextBlock",
    "ToolResultBlock",
    "ToolResultMessage",
    "ToolUseBlock",
]
