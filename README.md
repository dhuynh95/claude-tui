# claude-sock: Use your Claude subscription with your Claw

CLI that wraps the Claude Code CLI to use Claude models just with a subscription, thus without paying through API or extra tokens.

## Why

Anthropic recently limited support for third-party harnesses through subscription. It used to be possible to use Claude subscription to use a third-party harness such as Open Claw.

claude-sock bridges the gap: it speaks the `claude -p` protocol so tools like OpenClaw can use it as a drop-in backend.

## Getting started

### Prerequisites

- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and logged in (`claude --version`)
- Python 3.10+
- [OpenClaw](https://github.com/openclaw/openclaw) (or any tool that calls `claude -p`)

### 1. Install claude-sock

```bash
pipx install claude-sock 
```

### 2. Install OpenClaw

```bash
npm install -g openclaw@latest
openclaw onboard --install-daemon
```

During onboard, pick **"Anthropic Claude CLI"** when asked for the provider.

### 3. Point OpenClaw to claude-sock

Add to `~/.openclaw/openclaw.json`:

```json5
{
  agents: {
    defaults: {
      model: {
        primary: "claude-cli/claude-sonnet-4-6"
      },
      cliBackends: {
        "claude-cli": {
          command: "claude-sock"
        }
      }
    }
  }
}
```

Note: it will use the model you use in your Claude Code (which means you can access the latest best models such as Opus 4.6 1M Context).

### 4. Restart and chat

```bash
openclaw daemon restart
openclaw dashboard
```

### Standalone usage

claude-sock also works on its own:

```bash
# Positional argument
claude-sock "What is 2+2?"

# Pipe mode (same as claude -p)
echo "What is 2+2?" | claude-sock -p --output-format stream-json
```

## How it works

claude-sock spawns the Claude Code TUI in a pty, types your prompt as keystrokes, then reads the response from Claude Code's session JSONL file. Output is translated to `claude -p` format (stdin/stdout JSONL) so any tool expecting that protocol works seamlessly.

```
Your code
   │
   ▼ stdin (prompt)
┌─────────────┐
│  claude-sock │
└──────┬──────┘
       │ keystrokes
       ▼
┌─────────┐        ┌──────────────┐
│   pty   │───────>│  Claude Code  │
│ (write) │        │    TUI        │
└─────────┘        └──────┬───────┘
                          │ writes
                          ▼
                   ┌──────────────┐
                   │ session.jsonl │
                   └──────┬───────┘
                          │ reads + translates
                          ▼
                   ┌──────────────┐
                   │  claude -p   │
                   │  format out  │──> stdout (JSONL)
                   └──────────────┘
```

## Known limitations

- Each call spawns a fresh TUI process (~5s startup overhead)

## Contributing

Feel free to open PRs to make it more stable / more feature prone.

## Liability

This is a toy project provided as-is, with no warranty of any kind. Use it at your own risk. The author is not responsible for any consequences arising from its use, including but not limited to account restrictions, unexpected charges, or violations of Anthropic's terms of service. You are solely responsible for how you use this tool and for ensuring compliance with all applicable terms and policies.
