# termiclaude

Autonomous supervisor for interactive CLI agents. Wraps any command in a PTY,
auto-approves prompts, and optionally watches for agents going off-rails.

## Install

```bash
cd ~/dev/termiclaude
pip install -e .
```

## Quick start

```bash
cd ~/your-project
termiclaude claude "add tests for the auth module"
```

That's it. termiclaude wraps Claude Code, auto-approves tool permissions,
and logs every decision to `termiclaude.jsonl`.

## How it works

```
termiclaude (PTY wrapper)
  |
  +-- spawns: claude "your task"
  |     |
  |     +-- Claude asks "Write file? [1. Yes / 2. No]"
  |     +-- termiclaude detects idle (output stopped for N seconds)
  |     +-- pattern match: "Esc to cancel" -> press Enter
  |     +-- Claude continues working
  |
  +-- optional supervisor (--provider):
        +-- every N seconds, sends recent output to cheap LLM
        +-- LLM checks: is agent on track toward the goal?
        +-- if off-rails: sends Ctrl+C + redirect message
```

**Two layers:**

| Layer | What it does | LLM needed? |
|-------|-------------|-------------|
| Doorman | Pattern-matches prompts, presses Enter/y | No |
| Supervisor | Periodically checks if agent is on track | Yes (cheap model) |

## Usage

### Basic: auto-approve only (no API key needed)

```bash
termiclaude claude "refactor auth to use JWT"
termiclaude claude                              # interactive mode
termiclaude npm init                            # works with any CLI
```

### With supervisor (watches for derailing)

```bash
# Using local Ollama (free, fast)
termiclaude --provider ollama claude "add JWT auth"

# Using Anthropic API
termiclaude --provider anthropic claude "fix login bug"

# Using another Claude Code instance (Max subscription, no API cost)
# Must be run from a regular terminal, not inside Claude Code
termiclaude --provider claude-cli claude "refactor everything"
```

### Options

```
--idle SECS         Seconds of silence before auto-responding (default: 4)
--provider          LLM for supervisor: none|ollama|anthropic|openai|claude-cli
--model MODEL       Specific model (default: qwen3:4b for ollama, haiku for anthropic)
--goal GOAL         What the agent should accomplish (auto-extracted from claude command)
--supervise SECS    Supervisor check interval (default: 60s when provider is set)
--dry-run           Detect prompts but don't send responses
--log FILE          Log file path (default: termiclaude.jsonl)
--no-log            Disable logging
--max-responses N   Stop auto-approving after N responses (0=unlimited)
```

### Environment variables

```bash
# Ollama (recommended for supervisor)
export OLLAMA_HOST=192.168.1.100     # bare IP, or http://host:port

# Anthropic API
export ANTHROPIC_API_KEY=sk-ant-...

# OpenAI API
export OPENAI_API_KEY=sk-...
```

## Provider comparison

| Provider | Speed | Cost | Setup |
|----------|-------|------|-------|
| `none` | instant | free | nothing |
| `ollama` (qwen3:4b) | ~0.5s | free | `ollama pull qwen3:4b` |
| `anthropic` (haiku) | ~1-2s | ~$0.001/check | API key |
| `claude-cli` | ~3-5s | free (Max sub) | `claude` on PATH |
| `openai` (gpt-4o-mini) | ~1-2s | ~$0.001/check | API key |

## Log format

Every decision is logged to `termiclaude.jsonl` (one JSON object per line):

```json
{"ts": "2026-03-01T15:01:54", "event": "respond", "response": "", "source": "pattern", "context": "Esc to cancel", "count": 1}
{"ts": "2026-03-01T15:02:00", "event": "supervise", "status": "off_rails", "action": "message", "reasoning": "Agent is formatting code instead of fixing the bug"}
{"ts": "2026-03-01T15:02:00", "event": "intervene", "type": "message", "message": "Focus on the login bug", "intervention_count": 1}
```

## Safety

- Pattern matching only sends Enter/y — never destructive input
- Supervisor is conservative: only interrupts when clearly stuck/wrong
- `--max-responses N` caps total auto-approvals
- `--dry-run` shows what would happen without doing it
- Rail detection: pauses if too many responses/minute or repeated prompts
- You can always type into the terminal yourself — termiclaude backs off
