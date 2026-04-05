# claude-sock: Use your Claude subscription with your Claw

CLI that wraps the Claude Code CLI to use Claude models just with a subscription, thus without paying through API or extra tokens.

## Why

Anthropic recently limited support for third-party harnesses through subscription. It used to be possible to use Claude subscription to use a third-party harness such as Open Claw.

## Getting started

1. Install claude-sock

`pip install claude-sock`

2. Have it be used as your main Agent.

Example with Open Claw you need to:

## How it works



```
Your code
   │
   ▼ keystrokes
┌─────────┐        ┌──────────────┐
│   pty   │───────▶│  Claude Code  │
│ (write) │        │    TUI        │
└─────────┘        └──────┬───────┘
                          │ writes
                          ▼
                   ┌──────────────┐
                   │ session.jsonl │
                   └──────┬───────┘
                          │ reads
                          ▼
                   ┌──────────────┐
                   │  Your code   │
                   │  (parsed)    │
                   └──────────────┘
```

## Contributing

MIT
