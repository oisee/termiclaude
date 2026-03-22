#!/usr/bin/env python3
"""
termiclaude - Autonomous supervisor for interactive CLI agents.

Wraps any command in a PTY, passes output straight through,
detects when the program is waiting for input (via idle timeout),
and auto-responds with the right answer. No TUI wrapper, no
rendering conflicts — just a transparent pipe with a brain.

Usage:
    termiclaude claude "do the thing"
    termiclaude --idle 5 --provider anthropic npm install
    termiclaude --dry-run claude "refactor everything"
"""

import os
import pty
import sys
import re
import signal
import select
import time
import json
import struct
import fcntl
import termios
import argparse
import tempfile
from datetime import datetime
from dataclasses import dataclass
from typing import Optional, Tuple


# =============================================================================
# ANSI stripping
# =============================================================================

# Matches CSI sequences, OSC sequences, and other escape codes
_ANSI_RE = re.compile(r"""
    \x1b        # ESC
    (?:
        \[          # CSI
        [0-9;?]*    # parameter bytes
        [A-Za-z~]   # final byte
    |
        \]          # OSC
        .*?         # payload
        (?:\x07|\x1b\\)  # ST (BEL or ESC\)
    |
        [()][AB012]  # charset selection
    |
        [=>Nc]       # other short sequences
    )
""", re.VERBOSE | re.DOTALL)

# Control characters (except newline/tab)
_CTRL_RE = re.compile(r'[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]')


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes and control chars from text."""
    # Replace cursor-forward sequences with spaces (Claude Code uses these as spacing)
    text = re.sub(r'\x1b\[(\d+)C', lambda m: ' ' * int(m.group(1)), text)
    text = _ANSI_RE.sub('', text)
    text = _CTRL_RE.sub('', text)
    return text


# =============================================================================
# Fast pattern matching — no LLM needed for these
# =============================================================================

# Each entry: (compiled_regex, response_to_send)
# Checked against ANSI-stripped, lowercased last ~10 lines
FAST_PATTERNS = [
    # ── Claude Code specific ──
    # Claude Code permission prompts: "❯ 1. Yes" with "Enter to confirm · Esc to cancel"
    # The ❯ means option 1 is already highlighted — just press Enter
    (re.compile(r'enter\s+to\s+confirm\s*.*esc\s+to\s+cancel', re.IGNORECASE), ''),
    (re.compile(r'esc\s+to\s+cancel\s*.*enter\s+to\s+confirm', re.IGNORECASE), ''),
    # Claude Code: "Do you want to proceed?" with numbered options
    (re.compile(r'do you want to proceed\?', re.IGNORECASE), ''),
    (re.compile(r'do you want to execute', re.IGNORECASE), ''),
    # Claude Code: trust folder prompt
    (re.compile(r'yes,?\s+i\s+trust\s+this', re.IGNORECASE), ''),
    # Claude Code: tab to amend
    (re.compile(r'tab\s+to\s+amend', re.IGNORECASE), ''),

    # ── Press enter ──
    (re.compile(r'press\s+enter', re.IGNORECASE), ''),

    # ── Standard y/n prompts ──
    (re.compile(r'\[Y/n\]'), 'y'),
    (re.compile(r'\[y/N\]'), 'y'),
    (re.compile(r'\(y/n\)', re.IGNORECASE), 'y'),
    (re.compile(r'\(yes/no\)', re.IGNORECASE), 'yes'),
    (re.compile(r'(?:continue|proceed|confirm)\?\s*$', re.IGNORECASE), 'y'),
    (re.compile(r'are you sure', re.IGNORECASE), 'y'),

    # ── Esc to cancel (generic — current selection is correct) ──
    (re.compile(r'esc\s+to\s+cancel', re.IGNORECASE), ''),

    # ── npm/yarn ──
    (re.compile(r'ok to proceed\?', re.IGNORECASE), 'y'),
    (re.compile(r'is this ok\?', re.IGNORECASE), 'y'),

    # ── Git ──
    (re.compile(r'do you wish to continue', re.IGNORECASE), 'y'),

    # ── pip ──
    (re.compile(r'proceed\s*\(y/n\)', re.IGNORECASE), 'y'),

    # ── Generic numbered menus ──
    (re.compile(r'>\s*1\.\s*yes', re.IGNORECASE), ''),  # already selected, just Enter
    (re.compile(r'(?:choice|select|option)\s*:\s*$', re.IGNORECASE), '1'),
]


def fast_match(text: str) -> Optional[str]:
    """Try to match against known patterns. Returns response or None.
    Only checks the LAST 10 lines to avoid false matches from old output."""
    lines = text.strip().split('\n')
    tail = '\n'.join(lines[-10:])
    for pattern, response in FAST_PATTERNS:
        if pattern.search(tail):
            return response
    return None


# =============================================================================
# Heuristic: does this look like the program is waiting for input?
# =============================================================================

# Patterns that suggest a prompt (even if we don't know the exact answer)
_PROMPT_HINTS = [
    re.compile(r'\?\s*$', re.MULTILINE),
    re.compile(r':\s*$', re.MULTILINE),
    re.compile(r'\[.*\]\s*$', re.MULTILINE),
    re.compile(r'>\s*$', re.MULTILINE),
    re.compile(r'choice', re.IGNORECASE),
    re.compile(r'select', re.IGNORECASE),
    re.compile(r'enter\s', re.IGNORECASE),
    re.compile(r'input', re.IGNORECASE),
    re.compile(r'password', re.IGNORECASE),
    re.compile(r'approve', re.IGNORECASE),
]


def looks_like_prompt(text: str) -> bool:
    """Heuristic: does the tail of output look like it's waiting for input?"""
    # Check last 5 lines
    lines = [l.strip() for l in text.strip().split('\n') if l.strip()]
    if not lines:
        return False
    tail = '\n'.join(lines[-5:])
    return any(p.search(tail) for p in _PROMPT_HINTS)


# =============================================================================
# LLM fallback — only called for ambiguous prompts
# =============================================================================

def ask_llm_prompt(context: str, provider: str = 'anthropic',
                   model: str = None, api_key: str = None,
                   system_instructions: str = None) -> Optional[str]:
    """Ask a cheap LLM what to type at a prompt. Returns the response string or None."""

    extra = ''
    if system_instructions:
        extra = f"\nADDITIONAL INSTRUCTIONS FROM USER:\n{system_instructions}\n"

    prompt = f"""You are an autonomous supervisor for a CLI agent. The program below is waiting for user input. Decide what to type to let it continue productively.

RULES:
- Respond with ONLY the exact text to type (no quotes, no explanation)
- For yes/no: respond "y" or "yes"
- For numbered menus: respond with the number (e.g. "1")
- For press enter: respond "ENTER"
- For text input: respond with a reasonable short answer
- If the program seems stuck/looping (not actually waiting for input), respond "SKIP"
- If the program is asking for something dangerous (delete production data, etc), respond "SKIP"
{extra}
PROGRAM OUTPUT (last 30 lines):
{context}

YOUR INPUT:"""

    try:
        if provider == 'claude-cli':
            return _ask_claude_cli(prompt, model)
        elif provider == 'anthropic':
            return _ask_anthropic(prompt, model or 'claude-haiku-4-5-20251001',
                                  api_key or os.getenv('ANTHROPIC_API_KEY'))
        elif provider == 'ollama':
            return _ask_ollama(prompt, model or 'qwen3:4b')
        elif provider == 'openai':
            return _ask_openai(prompt, model or 'gpt-4o-mini',
                               api_key or os.getenv('OPENAI_API_KEY'))
        elif provider == 'azure':
            return _ask_azure(prompt, model or 'gpt-4o',
                              api_key or os.getenv('AZURE_OPENAI_API_KEY'))
        else:
            return None
    except Exception as e:
        log_event('llm_error', {'error': str(e), 'provider': provider})
        return None


@dataclass
class SupervisorVerdict:
    status: str       # "on_track", "stuck", "off_rails", "error_loop", "idle"
    action: str       # "continue", "interrupt", "message"
    message: str      # what to tell/type to the agent (if action != "continue")
    reasoning: str    # short explanation for the log


def ask_llm_supervise(goal: str, recent_output: str, provider: str = 'anthropic',
                      model: str = None, api_key: str = None) -> Optional[SupervisorVerdict]:
    """Supervisor LLM: assess whether the agent is on track toward the goal."""

    prompt = f"""You are supervising an AI coding agent (Claude Code). The user gave it a task and you need to check if it's on track.

ORIGINAL GOAL:
{goal}

RECENT AGENT OUTPUT (last ~80 lines, ANSI-stripped):
{recent_output}

Assess the agent's status and respond in EXACTLY this JSON format (no other text):
{{"status": "<on_track|stuck|off_rails|error_loop|idle|uncertain>", "action": "<continue|interrupt|message|escalate>", "message": "<text to type if action is message, or question for human if escalate, or empty>", "reasoning": "<1 sentence explanation>"}}

RULES:
- "on_track" + "continue": agent is making progress toward the goal. This is the most common case.
- "stuck" + "interrupt": agent is repeating itself, hitting the same error, or not making progress. You will send Ctrl+C.
- "off_rails" + "message": agent is working on something unrelated to the goal. message should redirect it.
- "error_loop" + "interrupt": agent keeps hitting the same error without fixing it.
- "idle" + "continue": agent finished or is waiting for a new prompt from the user. Do nothing.
- "uncertain" + "escalate": you're not sure if this is right or wrong, or the agent is about to do something risky (destructive operations, major architectural changes, unclear requirements). Ask the human. Put your question in "message".
- Be conservative — only interrupt if clearly stuck/wrong. False positives are worse than being patient.
- Use "escalate" when the situation is ambiguous or risky — let the human decide.
- If you see the agent actively writing code, running tests, reading files — that's "on_track".

JSON:"""

    try:
        if provider == 'claude-cli':
            raw = _ask_claude_cli(prompt, model)
        elif provider == 'anthropic':
            raw = _ask_anthropic(prompt, model or 'claude-haiku-4-5-20251001',
                                 api_key or os.getenv('ANTHROPIC_API_KEY'))
        elif provider == 'ollama':
            raw = _ask_ollama(prompt, model or 'qwen3:4b')
        elif provider == 'openai':
            raw = _ask_openai(prompt, model or 'gpt-4o-mini',
                              api_key or os.getenv('OPENAI_API_KEY'))
        elif provider == 'azure':
            raw = _ask_azure(prompt, model or 'gpt-4o-mini',
                             api_key or os.getenv('AZURE_OPENAI_API_KEY'))
        else:
            return None

        if not raw:
            return None

        # Parse JSON from response (handle markdown code blocks)
        raw = raw.strip()
        if raw.startswith('```'):
            raw = re.sub(r'^```\w*\n?', '', raw)
            raw = re.sub(r'\n?```$', '', raw)
        data = json.loads(raw)
        return SupervisorVerdict(
            status=data.get('status', 'on_track'),
            action=data.get('action', 'continue'),
            message=data.get('message', ''),
            reasoning=data.get('reasoning', '')
        )
    except (json.JSONDecodeError, KeyError) as e:
        log_event('supervisor_parse_error', {'error': str(e), 'raw': raw[:200] if raw else ''})
        return None
    except Exception as e:
        log_event('supervisor_error', {'error': str(e)})
        return None


def _ask_anthropic(prompt: str, model: str, api_key: str) -> Optional[str]:
    if not api_key:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=model,
            max_tokens=200,
            temperature=0.0,
            messages=[{'role': 'user', 'content': prompt}]
        )
        return resp.content[0].text.strip()
    except ImportError:
        # Fallback to raw HTTP
        import urllib.request
        data = json.dumps({
            'model': model,
            'max_tokens': 200,
            'temperature': 0.0,
            'messages': [{'role': 'user', 'content': prompt}]
        }).encode()
        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages',
            data=data,
            headers={
                'Content-Type': 'application/json',
                'X-API-Key': api_key,
                'anthropic-version': '2023-06-01'
            }
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
            return body['content'][0]['text'].strip()


def _ask_claude_cli(prompt: str, model: str = None) -> Optional[str]:
    """Use 'claude -p' (Claude Code CLI) as the LLM. No API key needed — uses Max subscription."""
    import subprocess
    cmd = ['claude', '-p', prompt]
    if model:
        cmd.extend(['--model', model])
    env = os.environ.copy()
    # Allow nested claude invocation
    env.pop('CLAUDECODE', None)
    env.pop('CLAUDE_CODE', None)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, env=env
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
        log_event('claude_cli_error', {'error': str(e)})
        return None


def _normalize_ollama_host(host: str) -> str:
    """Normalize OLLAMA_HOST to a full URL — handles bare IP, host:port, etc."""
    host = host.strip().rstrip('/')
    if not host:
        return 'http://localhost:11434'
    if not host.startswith(('http://', 'https://')):
        host = 'http://' + host
    # Add default port if none specified
    from urllib.parse import urlparse
    parsed = urlparse(host)
    if not parsed.port:
        host = host + ':11434'
    return host


def _ask_ollama(prompt: str, model: str) -> Optional[str]:
    import urllib.request
    host = _normalize_ollama_host(os.getenv('OLLAMA_HOST', ''))
    # Suppress thinking for qwen3 models — append /no_think
    effective_prompt = prompt
    if 'qwen3' in model.lower():
        effective_prompt = prompt + ' /no_think'
    data = json.dumps({
        'model': model,
        'prompt': effective_prompt,
        'stream': False,
        'options': {'temperature': 0.0, 'num_predict': 300}
    }).encode()
    req = urllib.request.Request(
        f'{host}/api/generate',
        data=data,
        headers={'Content-Type': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read())
        result = body.get('response', '').strip()
        # Strip thinking tags from reasoning models
        if '<think>' in result:
            result = re.sub(r'<think>.*?</think>', '', result, flags=re.DOTALL).strip()
        return result or None


def _ask_openai(prompt: str, model: str, api_key: str) -> Optional[str]:
    if not api_key:
        return None
    import urllib.request
    data = json.dumps({
        'model': model,
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': 0.0,
        'max_tokens': 200
    }).encode()
    req = urllib.request.Request(
        'https://api.openai.com/v1/chat/completions',
        data=data,
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}'
        }
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read())
        return body['choices'][0]['message']['content'].strip()


def _ask_azure(prompt: str, model: str, api_key: str) -> Optional[str]:
    """Azure OpenAI API.

    Env vars:
        AZURE_OPENAI_API_KEY    — API key
        AZURE_OPENAI_ENDPOINT   — e.g. https://myinstance.openai.azure.com
        AZURE_OPENAI_API_VERSION — e.g. 2024-12-01-preview (default)
        AZURE_OPENAI_DEPLOYMENT — deployment name (default: same as model)
    """
    if not api_key:
        return None
    import urllib.request
    endpoint = os.getenv('AZURE_OPENAI_ENDPOINT', '').rstrip('/')
    if not endpoint:
        log_event('azure_error', {'error': 'AZURE_OPENAI_ENDPOINT not set'})
        return None
    api_version = os.getenv('AZURE_OPENAI_API_VERSION', '2024-12-01-preview')
    deployment = os.getenv('AZURE_OPENAI_DEPLOYMENT', model)

    url = f'{endpoint}/openai/deployments/{deployment}/chat/completions?api-version={api_version}'
    # Reasoning models (o-series) don't support temperature, use max_completion_tokens
    is_reasoning = deployment.startswith('o')
    payload = {
        'messages': [{'role': 'user', 'content': prompt}],
        'max_completion_tokens': 300,
    }
    if not is_reasoning:
        payload['temperature'] = 0.0
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            'Content-Type': 'application/json',
            'api-key': api_key
        }
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read())
        return body['choices'][0]['message']['content'].strip()


# =============================================================================
# Logging
# =============================================================================

_log_file = None
_log_ipc = None  # IPC instance for forwarding events to foreman


def init_log(path: str):
    global _log_file
    _log_file = open(path, 'a')


def set_log_ipc(ipc):
    global _log_ipc
    _log_ipc = ipc


def log_event(event: str, data: dict = None):
    entry = {
        'ts': datetime.now().isoformat(),
        'event': event,
        **(data or {})
    }
    if _log_file:
        _log_file.write(json.dumps(entry) + '\n')
        _log_file.flush()
    if _log_ipc:
        try:
            _log_ipc.send_event(event, **(data or {}))
        except Exception:
            pass


# =============================================================================
# IPC — communication between worker (PTY) and foreman (status pane)
# =============================================================================

class IPC:
    """File-based IPC between worker and foreman processes.

    Directory layout:
        {ipc_dir}/events.jsonl  — worker appends, foreman tails
        {ipc_dir}/input.jsonl   — foreman appends, worker polls
        {ipc_dir}/pid           — worker PID (for foreman to check liveness)
    """

    def __init__(self, ipc_dir: str):
        self.ipc_dir = ipc_dir
        self.events_path = os.path.join(ipc_dir, 'events.jsonl')
        self.input_path = os.path.join(ipc_dir, 'input.jsonl')
        self.pid_path = os.path.join(ipc_dir, 'pid')
        self._input_pos = 0  # file position for polling input

    @classmethod
    def create(cls) -> 'IPC':
        """Create a new IPC directory."""
        ipc_dir = tempfile.mkdtemp(prefix='termiclaude_')
        ipc = cls(ipc_dir)
        # Touch files
        open(ipc.events_path, 'w').close()
        open(ipc.input_path, 'w').close()
        with open(ipc.pid_path, 'w') as f:
            f.write(str(os.getpid()))
        return ipc

    @classmethod
    def connect(cls, ipc_dir: str) -> 'IPC':
        """Connect to existing IPC directory."""
        ipc = cls(ipc_dir)
        # Start reading input from end (only new messages)
        if os.path.exists(ipc.input_path):
            ipc._input_pos = os.path.getsize(ipc.input_path)
        return ipc

    def send_event(self, event: str, **data):
        """Worker → Foreman: append event."""
        entry = {'ts': datetime.now().strftime('%H:%M:%S'), 'event': event, **data}
        with open(self.events_path, 'a') as f:
            f.write(json.dumps(entry) + '\n')

    def poll_input(self) -> Optional[dict]:
        """Worker polls for foreman responses. Returns newest message or None."""
        try:
            size = os.path.getsize(self.input_path)
            if size <= self._input_pos:
                return None
            with open(self.input_path) as f:
                f.seek(self._input_pos)
                lines = f.readlines()
                self._input_pos = f.tell()
            # Return last message
            for line in reversed(lines):
                line = line.strip()
                if line:
                    return json.loads(line)
        except Exception:
            pass
        return None

    def send_input(self, message: str, **data):
        """Foreman → Worker: send response."""
        entry = {'ts': datetime.now().strftime('%H:%M:%S'), 'message': message, **data}
        with open(self.input_path, 'a') as f:
            f.write(json.dumps(entry) + '\n')

    def worker_alive(self) -> bool:
        """Check if worker process is still running."""
        try:
            with open(self.pid_path) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            return True
        except (FileNotFoundError, ValueError, ProcessLookupError):
            return False

    def cleanup(self):
        """Remove IPC directory."""
        import shutil
        try:
            shutil.rmtree(self.ipc_dir, ignore_errors=True)
        except Exception:
            pass


# =============================================================================
# Foreman — interactive status pane
# =============================================================================

def run_foreman(ipc_dir: str):
    """Foreman process: shows events, handles escalations."""
    ipc = IPC.connect(ipc_dir)
    events_pos = 0

    # Colors
    C_RESET = '\033[0m'
    C_GRAY = '\033[90m'
    C_CYAN = '\033[36m'
    C_GREEN = '\033[32m'
    C_YELLOW = '\033[1;33m'
    C_RED = '\033[1;31m'
    C_BOLD = '\033[1m'

    print(f"{C_BOLD}─── termiclaude foreman ───{C_RESET}")
    print(f"{C_GRAY}watching agent | ipc: {ipc_dir}{C_RESET}")
    print()

    pending_escalation = False

    try:
        while True:
            # Check worker liveness
            if not ipc.worker_alive():
                print(f"\n{C_GRAY}Agent process exited.{C_RESET}")
                break

            # Read new events
            try:
                size = os.path.getsize(ipc.events_path)
                if size > events_pos:
                    with open(ipc.events_path) as f:
                        f.seek(events_pos)
                        new_lines = f.readlines()
                        events_pos = f.tell()

                    for line in new_lines:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            ev = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        ts = ev.get('ts', '')
                        event = ev.get('event', '')

                        if event == 'hook_approve':
                            tool = ev.get('tool', '?')
                            print(f"  {C_GRAY}{ts}{C_RESET} {C_GREEN}✓{C_RESET} {tool}")

                        elif event == 'respond':
                            src = ev.get('source', '?')
                            resp = ev.get('response', '')
                            display = repr(resp) if resp else '↵'
                            n = ev.get('count', '?')
                            print(f"  {C_GRAY}{ts}{C_RESET} {C_CYAN}#{n}{C_RESET} sent {display} ({src})")

                        elif event == 'supervise':
                            status = ev.get('status', '?')
                            reasoning = ev.get('reasoning', '')
                            if status == 'on_track':
                                print(f"  {C_GRAY}{ts}{C_RESET} {C_GREEN}● on track{C_RESET} {C_GRAY}{reasoning}{C_RESET}")
                            else:
                                print(f"  {C_GRAY}{ts}{C_RESET} {C_YELLOW}● {status}{C_RESET} {reasoning}")

                        elif event == 'escalate':
                            question = ev.get('question', ev.get('reasoning', '?'))
                            print(f"\n  {C_YELLOW}{'─' * 50}")
                            print(f"  ⚠  NEEDS YOUR INPUT")
                            print(f"  {question}")
                            print(f"  {'─' * 50}{C_RESET}\n")
                            print('\a', end='', flush=True)  # BEL
                            pending_escalation = True

                        elif event == 'intervene':
                            msg = ev.get('message', '')
                            print(f"  {C_GRAY}{ts}{C_RESET} {C_RED}▶ intervened{C_RESET} {msg[:60]}")

                        elif event in ('start', 'exit', 'hooks_installed', 'hooks_uninstalled'):
                            print(f"  {C_GRAY}{ts} [{event}]{C_RESET}")

                        else:
                            print(f"  {C_GRAY}{ts} {event}{C_RESET}")

            except Exception:
                pass

            # If escalation pending, prompt for input (non-blocking check)
            if pending_escalation:
                try:
                    import select as sel
                    r, _, _ = sel.select([sys.stdin], [], [], 0.1)
                    if r:
                        response = sys.stdin.readline().strip()
                        if response:
                            ipc.send_input(response)
                            print(f"  {C_CYAN}→ sent: {response}{C_RESET}\n")
                            pending_escalation = False
                except Exception:
                    pass
            else:
                time.sleep(0.3)

    except KeyboardInterrupt:
        print(f"\n{C_GRAY}Foreman stopped.{C_RESET}")


# =============================================================================
# tmux launcher — auto-split terminal
# =============================================================================

def launch_tmux(args_list: list[str], ipc_dir: str):
    """Launch termiclaude in a tmux session with worker (top) + foreman (bottom)."""
    import subprocess
    import shutil

    tmux = shutil.which('tmux')
    if not tmux:
        print("[termiclaude] tmux not found — run 'termiclaude --foreman' in another tab")
        print(f"  IPC dir: {ipc_dir}")
        return None

    session_name = f'termiclaude-{os.getpid()}'
    termiclaude_bin = os.path.abspath(__file__)
    python = sys.executable

    # Build worker command (pass through all original args + --ipc-dir)
    worker_args = [python, termiclaude_bin, '--ipc-dir', ipc_dir] + args_list
    worker_cmd = ' '.join(_shell_quote(a) for a in worker_args)

    # Foreman command
    foreman_cmd = f'{_shell_quote(python)} {_shell_quote(termiclaude_bin)} --foreman {_shell_quote(ipc_dir)}'

    # Create tmux session: top pane = worker, then split bottom = foreman
    subprocess.run([
        tmux, 'new-session', '-d', '-s', session_name,
        '-x', '200', '-y', '50',
        worker_cmd,
    ])
    subprocess.run([
        tmux, 'split-window', '-v', '-t', session_name,
        '-l', '30%',
        foreman_cmd,
    ])
    # Focus on top pane (worker/claude)
    subprocess.run([tmux, 'select-pane', '-t', f'{session_name}:.0'])

    # Attach
    os.execvp(tmux, [tmux, 'attach-session', '-t', session_name])


def _shell_quote(s: str) -> str:
    """Shell-quote a string."""
    if not s:
        return "''"
    import shlex
    return shlex.quote(s)


# =============================================================================
# Rail detection — is the agent going off track?
# =============================================================================

class RailDetector:
    """Detect if the agent is stuck in a loop or going off-rails."""

    def __init__(self, max_responses_per_minute: int = 10,
                 repeat_threshold: int = 3):
        self.response_times = []      # timestamps of auto-responses
        self.recent_contexts = []     # last N context hashes
        self.max_rpm = max_responses_per_minute
        self.repeat_threshold = repeat_threshold

    def record_response(self, context_snippet: str):
        now = time.time()
        self.response_times.append(now)
        # Keep last 60s
        self.response_times = [t for t in self.response_times if now - t < 60]

        # Track context similarity (simple: first 200 chars)
        sig = context_snippet[:200].strip()
        self.recent_contexts.append(sig)
        if len(self.recent_contexts) > 20:
            self.recent_contexts = self.recent_contexts[-20:]

    def is_looping(self) -> bool:
        """Too many responses too fast?"""
        return len(self.response_times) > self.max_rpm

    def is_repeating(self) -> bool:
        """Same prompt appearing over and over?"""
        if len(self.recent_contexts) < self.repeat_threshold:
            return False
        last = self.recent_contexts[-1]
        recent_same = sum(1 for c in self.recent_contexts[-5:] if c == last)
        return recent_same >= self.repeat_threshold


# =============================================================================
# Window size propagation
# =============================================================================

def get_winsize() -> Tuple[int, int]:
    """Get terminal window size (rows, cols)."""
    try:
        packed = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ,
                             b'\x00' * 8)
        rows, cols = struct.unpack('HHHH', packed)[:2]
        return rows, cols
    except Exception:
        return 24, 80


def set_winsize(fd: int, rows: int, cols: int):
    """Set window size on a PTY fd."""
    try:
        packed = struct.pack('HHHH', rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, packed)
    except Exception:
        pass


# =============================================================================
# Main supervisor loop
# =============================================================================

class Supervisor:
    def __init__(self, command: list[str], idle_seconds: float = 4.0,
                 provider: str = 'none', model: str = None,
                 api_key: str = None, dry_run: bool = False,
                 log_path: str = None, max_responses: int = 0,
                 goal: str = None, supervise_interval: float = 0,
                 no_hooks: bool = False, ipc_dir: str = None,
                 llm_only: bool = False,
                 system_instructions: str = None):
        self.command = command
        self.idle_seconds = idle_seconds
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self.dry_run = dry_run
        self.max_responses = max_responses  # 0 = unlimited
        self.goal = goal                    # what the agent should be doing
        self.supervise_interval = supervise_interval  # seconds between health checks (0=off)
        self.log_path = log_path
        self.ipc = IPC.connect(ipc_dir) if ipc_dir else None
        self.llm_only = llm_only  # skip patterns, let LLM decide everything
        self.system_instructions = system_instructions  # extra instructions for LLM

        self.master_fd = None
        self.child_pid = None
        self.buffer = []          # rolling buffer of recent output lines
        self.full_buffer = []     # full buffer for supervisor (not cleared on response)
        self.max_buffer = 200     # keep last N lines
        self.max_full_buffer = 500
        self.last_output_time = time.time()
        self.idle_handled = False  # already responded to this idle period?
        self.total_responses = 0
        self.total_interventions = 0
        self.rail_detector = RailDetector()
        self.running = True
        self.last_supervise_time = 0.0  # when we last ran a supervisor check

        # Hooks state
        self._use_hooks = (not no_hooks
                           and command and command[0] in ('claude', 'claude-code'))
        self._settings_backup = None
        self._settings_path = None
        self._state_file = None  # temp file for PostToolUse state

        if log_path:
            init_log(log_path)
        if self.ipc:
            set_log_ipc(self.ipc)

    def _install_hooks(self):
        """Install Claude Code hooks for auto-approval and supervisor."""
        settings_dir = os.path.join(os.getcwd(), '.claude')
        self._settings_path = os.path.join(settings_dir, 'settings.local.json')

        # Backup existing settings
        if os.path.exists(self._settings_path):
            with open(self._settings_path) as f:
                self._settings_backup = f.read()

        settings = {}
        if self._settings_backup:
            try:
                settings = json.loads(self._settings_backup)
            except json.JSONDecodeError:
                settings = {}

        # Build hook command pointing to this script
        hook_bin = os.path.abspath(__file__)
        python = sys.executable

        hooks = {
            'PreToolUse': [{
                'matcher': '',
                'hooks': [{'type': 'command',
                           'command': f'{python} {hook_bin} --hook-pre-tool'}]
            }],
        }

        # PostToolUse: feed supervisor state
        if self.goal and self.provider != 'none':
            self._state_file = tempfile.NamedTemporaryFile(
                mode='w', prefix='termiclaude_state_', suffix='.jsonl',
                delete=False)
            self._state_file.close()
            hooks['PostToolUse'] = [{
                'matcher': '',
                'hooks': [{'type': 'command',
                           'command': f'{python} {hook_bin} --hook-post-tool'}]
            }]
            # Stop hook: supervisor check when Claude is idle
            hooks['Stop'] = [{
                'matcher': '',
                'hooks': [{'type': 'command',
                           'command': f'{python} {hook_bin} --hook-stop'}]
            }]

        # Preserve existing hooks, add ours
        existing_hooks = settings.get('hooks', {})
        for event, hook_list in hooks.items():
            existing = existing_hooks.get(event, [])
            existing_hooks[event] = existing + hook_list
        settings['hooks'] = existing_hooks

        os.makedirs(settings_dir, exist_ok=True)
        with open(self._settings_path, 'w') as f:
            json.dump(settings, f, indent=2)

        log_event('hooks_installed', {
            'path': self._settings_path,
            'events': list(hooks.keys()),
        })

    def _uninstall_hooks(self):
        """Restore original settings after claude exits."""
        if not self._settings_path:
            return
        try:
            if self._settings_backup:
                with open(self._settings_path, 'w') as f:
                    f.write(self._settings_backup)
            elif os.path.exists(self._settings_path):
                os.remove(self._settings_path)
        except Exception as e:
            log_event('hooks_uninstall_error', {'error': str(e)})

        # Clean up state file
        if self._state_file:
            try:
                os.unlink(self._state_file.name)
            except Exception:
                pass

        log_event('hooks_uninstalled')

    def _get_hook_env(self) -> dict:
        """Extra env vars for child process so hooks can find log/state/IPC."""
        env = {}
        if self.log_path:
            env['TERMICLAUDE_LOG'] = os.path.abspath(self.log_path)
        if self._state_file:
            env['TERMICLAUDE_STATE'] = self._state_file.name
        if self.goal:
            env['TERMICLAUDE_GOAL'] = self.goal
        if self.provider and self.provider != 'none':
            env['TERMICLAUDE_PROVIDER'] = self.provider
        if self.model:
            env['TERMICLAUDE_MODEL'] = self.model
        if self.api_key:
            env['TERMICLAUDE_API_KEY'] = self.api_key
        if self.ipc:
            env['TERMICLAUDE_IPC'] = self.ipc.ipc_dir
        return env

    def start(self) -> int:
        """Spawn child and run the supervisor loop. Returns exit code."""
        # Install hooks before spawning claude
        if self._use_hooks:
            self._install_hooks()

        # Save original terminal settings
        old_attrs = None
        try:
            old_attrs = termios.tcgetattr(sys.stdin.fileno())
        except Exception:
            pass

        # Fork PTY
        self.child_pid, self.master_fd = pty.fork()

        if self.child_pid == 0:
            # Child: clean env so nested CLI agents (claude, etc.) don't refuse to start
            for var in ['CLAUDECODE', 'CLAUDE_CODE']:
                os.environ.pop(var, None)
            # Pass hook env vars
            if self._use_hooks:
                os.environ.update(self._get_hook_env())
            os.execvp(self.command[0], self.command)
            sys.exit(127)  # unreachable unless exec fails

        # Parent: set up
        rows, cols = get_winsize()
        set_winsize(self.master_fd, rows, cols)

        # Handle SIGWINCH — propagate terminal resizes
        def on_winch(signum, frame):
            r, c = get_winsize()
            set_winsize(self.master_fd, r, c)
        signal.signal(signal.SIGWINCH, on_winch)

        # Put stdin in raw mode so keystrokes pass through immediately
        try:
            import tty
            tty.setraw(sys.stdin.fileno())
        except Exception:
            pass

        log_event('start', {'command': self.command, 'pid': self.child_pid,
                            'provider': self.provider, 'idle_seconds': self.idle_seconds})

        exit_code = 0
        try:
            exit_code = self._loop()
        except KeyboardInterrupt:
            # Forward Ctrl+C to child
            try:
                os.kill(self.child_pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            exit_code = 130
        finally:
            # Restore terminal
            if old_attrs:
                try:
                    termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN,
                                      old_attrs)
                except Exception:
                    pass
            # Clean up child
            try:
                os.close(self.master_fd)
            except Exception:
                pass
            try:
                os.waitpid(self.child_pid, 0)
            except Exception:
                pass
            # Uninstall hooks
            if self._use_hooks:
                self._uninstall_hooks()

            log_event('exit', {'code': exit_code,
                               'total_responses': self.total_responses})

        return exit_code

    def _loop(self) -> int:
        """Main select loop."""
        while self.running:
            # Build fd list
            fds = [self.master_fd]
            try:
                fds.append(sys.stdin.fileno())
            except Exception:
                pass

            try:
                readable, _, _ = select.select(fds, [], [], 0.5)
            except (OSError, ValueError):
                break

            for fd in readable:
                if fd == self.master_fd:
                    # Output from child
                    try:
                        data = os.read(self.master_fd, 16384)
                    except OSError:
                        self.running = False
                        break
                    if not data:
                        self.running = False
                        break

                    # Pass through to real stdout
                    os.write(sys.stdout.fileno(), data)

                    # Buffer for analysis
                    text = data.decode('utf-8', errors='replace')
                    self._buffer_output(text)
                    self.last_output_time = time.time()
                    self.idle_handled = False

                elif fd == sys.stdin.fileno():
                    # Input from user — pass through to child
                    try:
                        data = os.read(sys.stdin.fileno(), 4096)
                    except OSError:
                        break
                    if data:
                        try:
                            os.write(self.master_fd, data)
                        except OSError:
                            self.running = False
                            break
                        # User typed something — reset idle
                        self.last_output_time = time.time()
                        self.idle_handled = True  # don't auto-respond after user input

            # Check for idle (prompt auto-response)
            if not self.idle_handled and self.buffer:
                elapsed = time.time() - self.last_output_time
                if elapsed >= self.idle_seconds:
                    self._handle_idle()

            # Periodic supervisor health check
            if (self.supervise_interval > 0 and self.goal
                    and self.provider != 'none'):
                now = time.time()
                if now - self.last_supervise_time >= self.supervise_interval:
                    self._supervise()
                    self.last_supervise_time = now

            # Poll foreman input (escalation responses)
            if self.ipc:
                msg = self.ipc.poll_input()
                if msg and msg.get('message'):
                    response_text = msg['message']
                    try:
                        os.write(self.master_fd, (response_text + '\r').encode())
                    except OSError:
                        pass
                    self.idle_handled = False  # resume auto-approvals
                    self._notify(f"[termiclaude] foreman response: {response_text[:60]}", 'info')
                    log_event('foreman_response', {'message': response_text})

            # Check if child exited
            try:
                pid, status = os.waitpid(self.child_pid, os.WNOHANG)
                if pid != 0:
                    # Drain remaining output
                    try:
                        while True:
                            r, _, _ = select.select([self.master_fd], [], [], 0.1)
                            if not r:
                                break
                            data = os.read(self.master_fd, 16384)
                            if not data:
                                break
                            os.write(sys.stdout.fileno(), data)
                    except Exception:
                        pass
                    return os.waitstatus_to_exitcode(status)
            except ChildProcessError:
                return 0

        return 0

    def _buffer_output(self, text: str):
        """Add text to rolling line buffers."""
        lines = text.split('\n')
        for buf in (self.buffer, self.full_buffer):
            if lines and buf:
                buf[-1] += lines[0]
                buf.extend(lines[1:])
            else:
                buf.extend(lines)
        if len(self.buffer) > self.max_buffer:
            self.buffer = self.buffer[-self.max_buffer:]
        if len(self.full_buffer) > self.max_full_buffer:
            self.full_buffer = self.full_buffer[-self.max_full_buffer:]

    def _handle_idle(self):
        """Program has been quiet — check if it's waiting for input."""
        self.idle_handled = True

        # Check response limit
        if self.max_responses and self.total_responses >= self.max_responses:
            log_event('limit_reached', {'max': self.max_responses})
            return

        # Get clean text for analysis
        raw_tail = '\n'.join(self.buffer[-30:])
        clean_tail = strip_ansi(raw_tail)
        clean_tail_stripped = clean_tail.strip()

        if not clean_tail_stripped:
            return

        # Check for rail issues
        if self.rail_detector.is_looping():
            log_event('rail_looping', {'context': clean_tail_stripped[-200:]})
            self._notify("[termiclaude] Too many auto-responses/min — pausing automation", 'alert')
            return

        if self.rail_detector.is_repeating():
            log_event('rail_repeating', {'context': clean_tail_stripped[-200:]})
            self._notify("[termiclaude] Repeated prompt detected — pausing automation", 'alert')
            return

        # Try fast pattern match first (unless --llm-only)
        response = None if self.llm_only else fast_match(clean_tail_stripped)
        source = 'pattern'

        if response is None:
            # Check if it even looks like a prompt
            if not looks_like_prompt(clean_tail_stripped):
                return  # Probably just slow output, not a prompt

            # LLM fallback
            if self.provider == 'none':
                # No LLM configured, but it looks like a prompt — default to yes
                response = 'y'
                source = 'default'
            else:
                context = '\n'.join(self.buffer[-30:])
                clean_context = strip_ansi(context)
                response = ask_llm_prompt(clean_context, provider=self.provider,
                                   model=self.model, api_key=self.api_key,
                                   system_instructions=self.system_instructions)
                source = 'llm'

                if response and response.upper() == 'SKIP':
                    log_event('llm_skip', {'context': clean_tail_stripped[-200:]})
                    return

                if response and response.upper() == 'ENTER':
                    response = ''

                if response is None:
                    # LLM failed or returned nothing — default to "y"
                    response = 'y'
                    source = 'default'

        # Dry run?
        if self.dry_run:
            display = repr(response) if response else "'\\n'"
            self._notify(f"[termiclaude] DRY RUN: would send {display} (source: {source})")
            log_event('dry_run', {'response': response, 'source': source,
                                  'context': clean_tail_stripped[-200:]})
            return

        # Send the response
        # Use \r (carriage return) — in raw terminal mode, Enter = \r not \n
        try:
            to_send = response + '\r' if response else '\r'
            os.write(self.master_fd, to_send.encode())
        except OSError:
            return

        self.total_responses += 1
        self.rail_detector.record_response(clean_tail_stripped)

        # Clear buffer so old prompts don't cause false matches next time
        self.buffer.clear()

        display = repr(response) if response else '↵'
        log_event('respond', {
            'response': response,
            'source': source,
            'context': clean_tail_stripped[-100:],
            'count': self.total_responses
        })

        # Brief visual indicator (sent to stderr so it doesn't mix with PTY)
        self._notify(f"[termiclaude] #{self.total_responses} sent {display} ({source})", 'ok')

    def _supervise(self):
        """Periodic health check — ask supervisor LLM if agent is on track."""
        if not self.full_buffer:
            return

        raw_output = '\n'.join(self.full_buffer[-80:])
        clean_output = strip_ansi(raw_output).strip()
        if not clean_output:
            return

        verdict = ask_llm_supervise(
            goal=self.goal,
            recent_output=clean_output,
            provider=self.provider,
            model=self.model,
            api_key=self.api_key
        )

        if not verdict:
            return

        log_event('supervise', {
            'status': verdict.status,
            'action': verdict.action,
            'message': verdict.message,
            'reasoning': verdict.reasoning,
        })

        if verdict.action == 'continue':
            # All good, just log
            self._notify(f"[termiclaude] on track — {verdict.reasoning}", 'ok')
            return

        if verdict.action == 'escalate':
            self.total_interventions += 1
            question = verdict.message or verdict.reasoning
            self._notify(
                f"[termiclaude] NEEDS YOUR INPUT: {question}",
                'escalate')
            # Pause auto-approvals until user types something
            self.idle_handled = True
            log_event('escalate', {
                'question': question,
                'reasoning': verdict.reasoning,
                'intervention_count': self.total_interventions,
            })
            return

        if verdict.action == 'interrupt':
            self.total_interventions += 1
            self._notify(
                f"[termiclaude] INTERRUPTING: {verdict.status} — {verdict.reasoning}",
                'alert')
            # Send Ctrl+C to interrupt the agent
            try:
                os.write(self.master_fd, b'\x03')
            except OSError:
                pass
            # If there's a redirect message, type it after a short pause
            if verdict.message:
                time.sleep(1)
                try:
                    os.write(self.master_fd, (verdict.message + '\r').encode())
                except OSError:
                    pass
                self._notify(
                    f"[termiclaude] sent redirect: {verdict.message[:80]}", 'info')
            log_event('intervene', {
                'type': 'interrupt',
                'message': verdict.message,
                'reasoning': verdict.reasoning,
                'intervention_count': self.total_interventions,
            })

        elif verdict.action == 'message':
            self.total_interventions += 1
            self._notify(
                f"[termiclaude] REDIRECTING: {verdict.reasoning}", 'alert')
            # Type a message to the agent (it should be at a prompt)
            if verdict.message:
                try:
                    os.write(self.master_fd, (verdict.message + '\r').encode())
                except OSError:
                    pass
                self._notify(
                    f"[termiclaude] sent: {verdict.message[:80]}", 'info')
            log_event('intervene', {
                'type': 'message',
                'message': verdict.message,
                'reasoning': verdict.reasoning,
                'intervention_count': self.total_interventions,
            })

    # ANSI color codes for notification levels
    _NOTIFY_STYLES = {
        'ok':       '\x1b[90m',        # gray — all good, low noise
        'info':     '\x1b[36m',        # cyan — informational
        'alert':    '\x1b[1;31m',      # bold red — intervention
        'escalate': '\x1b[1;33m',      # bold yellow — needs human
    }

    def _notify(self, msg: str, level: str = 'info'):
        """Print a supervisor message with appropriate urgency."""
        try:
            style = self._NOTIFY_STYLES.get(level, '\x1b[90m')
            bel = '\x07' if level in ('alert', 'escalate') else ''
            line = f"\r\n{style}{msg}\x1b[0m{bel}\r\n"
            sys.stderr.buffer.write(line.encode())
            sys.stderr.buffer.flush()
        except Exception:
            pass
        # Forward to foreman via IPC
        if self.ipc:
            self.ipc.send_event('notify', msg=msg, level=level)


# =============================================================================
# CLI
# =============================================================================

def _hook_write_event(event: str, **data):
    """Write event to log and IPC from hook subprocess."""
    entry = {'ts': datetime.now().isoformat(), 'event': event, **data}
    log_path = os.environ.get('TERMICLAUDE_LOG')
    if log_path:
        with open(log_path, 'a') as f:
            f.write(json.dumps(entry) + '\n')
    ipc_dir = os.environ.get('TERMICLAUDE_IPC')
    if ipc_dir:
        events_path = os.path.join(ipc_dir, 'events.jsonl')
        ipc_entry = {'ts': datetime.now().strftime('%H:%M:%S'), 'event': event, **data}
        with open(events_path, 'a') as f:
            f.write(json.dumps(ipc_entry) + '\n')


def _hook_pre_tool_use():
    """Hook entry point for Claude Code PreToolUse — auto-approves all tools."""
    try:
        input_data = json.loads(sys.stdin.read())
        tool_name = input_data.get('tool_name', 'unknown')
        _hook_write_event('hook_approve', tool=tool_name)
        json.dump({'decision': 'approve'}, sys.stdout)
    except Exception:
        json.dump({'decision': 'approve'}, sys.stdout)
    sys.exit(0)


def _hook_post_tool_use():
    """Hook entry point for Claude Code PostToolUse — feeds supervisor."""
    state_path = os.environ.get('TERMICLAUDE_STATE')
    if not state_path:
        sys.exit(0)

    try:
        input_data = json.loads(sys.stdin.read())
        tool_name = input_data.get('tool_name', 'unknown')
        tool_input = input_data.get('tool_input', {})
        summary = _summarize_tool_input(tool_name, tool_input)

        # Append to shared state file for supervisor to read
        with open(state_path, 'a') as f:
            f.write(json.dumps({
                'ts': datetime.now().isoformat(),
                'tool': tool_name,
                'input_summary': summary,
            }) + '\n')

        _hook_write_event('hook_post_tool', tool=tool_name, summary=summary)
    except Exception:
        pass
    sys.exit(0)


def _hook_stop():
    """Hook entry point for Claude Code Stop — runs supervisor check."""
    state_path = os.environ.get('TERMICLAUDE_STATE')
    log_path = os.environ.get('TERMICLAUDE_LOG')
    goal = os.environ.get('TERMICLAUDE_GOAL')
    provider = os.environ.get('TERMICLAUDE_PROVIDER')
    model = os.environ.get('TERMICLAUDE_MODEL')
    api_key = os.environ.get('TERMICLAUDE_API_KEY')

    if not (state_path and goal and provider):
        sys.exit(0)

    try:
        # Read accumulated tool actions from state file
        if not os.path.exists(state_path):
            sys.exit(0)
        with open(state_path) as f:
            lines = f.readlines()
        if not lines:
            sys.exit(0)

        # Build context from recent tool actions
        recent = lines[-30:]  # last 30 tool actions
        context = '\n'.join(line.strip() for line in recent)

        # Also read stdin for stop event data
        try:
            stop_data = json.loads(sys.stdin.read())
        except Exception:
            stop_data = {}

        verdict = ask_llm_supervise(
            goal=goal,
            recent_output=f"Recent tool actions:\n{context}",
            provider=provider,
            model=model,
            api_key=api_key,
        )

        if verdict and log_path:
            with open(log_path, 'a') as f:
                f.write(json.dumps({
                    'ts': datetime.now().isoformat(),
                    'event': 'hook_supervise',
                    'status': verdict.status,
                    'action': verdict.action,
                    'reasoning': verdict.reasoning,
                }) + '\n')

        # Clear state file after check
        with open(state_path, 'w') as f:
            pass

        # Notify user based on verdict
        if verdict and verdict.action == 'escalate':
            msg = verdict.message or verdict.reasoning
            sys.stderr.write(
                f"\r\n\x1b[1;33m[termiclaude] NEEDS YOUR INPUT: {msg}\x1b[0m\x07\r\n")
            sys.stderr.flush()
        elif verdict and verdict.action in ('interrupt', 'message'):
            sys.stderr.write(
                f"\r\n\x1b[1;31m[termiclaude] SUPERVISOR: {verdict.reasoning}\x1b[0m\x07\r\n")
            sys.stderr.flush()

    except Exception:
        pass
    sys.exit(0)


def _summarize_tool_input(tool_name: str, tool_input: dict) -> str:
    """Short summary of tool input for supervisor context."""
    if tool_name in ('Read', 'Glob', 'Grep'):
        return tool_input.get('file_path', tool_input.get('pattern', str(tool_input)[:100]))
    if tool_name == 'Write':
        path = tool_input.get('file_path', '?')
        size = len(tool_input.get('content', ''))
        return f'{path} ({size} chars)'
    if tool_name == 'Edit':
        return tool_input.get('file_path', '?')
    if tool_name == 'Bash':
        return tool_input.get('command', '')[:120]
    return str(tool_input)[:100]


def main():
    # Handle hook subcommands before argparse (they read stdin, must be fast)
    if len(sys.argv) >= 2 and sys.argv[1] == '--hook-pre-tool':
        _hook_pre_tool_use()
    if len(sys.argv) >= 2 and sys.argv[1] == '--hook-post-tool':
        _hook_post_tool_use()
    if len(sys.argv) >= 2 and sys.argv[1] == '--hook-stop':
        _hook_stop()
    # Foreman mode: termiclaude --foreman <ipc_dir>
    if len(sys.argv) >= 3 and sys.argv[1] == '--foreman':
        run_foreman(sys.argv[2])
        sys.exit(0)

    parser = argparse.ArgumentParser(
        prog='termiclaude',
        description='Autonomous supervisor for interactive CLI agents',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
usage:
  cd ~/your-project
  termiclaude claude "refactor the auth module"

  That's it. termiclaude wraps claude (or any CLI), auto-approves
  prompts, and logs every decision to termiclaude.jsonl.

  With tmux installed, termiclaude auto-splits into two panes:
    top  = Claude Code (full passthrough, you see everything)
    bottom = Foreman (status, logs, answers your questions)

more examples:
  termiclaude claude "add tests for auth"     # auto-approve, goal auto-extracted
  termiclaude --idle 8 claude "big refactor"  # more patience before responding
  termiclaude --dry-run claude "delete stuff" # see what it would approve
  termiclaude --no-log npm init               # wrap any interactive CLI
  termiclaude --no-tmux claude "quick fix"    # single-pane mode (no split)

with supervisor (watches output, intervenes if agent goes off-rails):
  termiclaude --provider ollama claude "add JWT auth"
  termiclaude --provider ollama --supervise 30 claude "big refactor"
  termiclaude --provider anthropic --goal "fix login bug" claude
        """
    )

    parser.add_argument('command', nargs=argparse.REMAINDER,
                        help='command to run and supervise (use -- before commands with flags)')
    parser.add_argument('--idle', type=float, default=4.0,
                        help='seconds of silence before checking for prompt (default: 4)')
    parser.add_argument('--provider',
                        choices=['none', 'claude-cli', 'anthropic', 'ollama', 'openai', 'azure'],
                        default='none',
                        help='LLM provider for supervisor & ambiguous prompts '
                             '(default: none, pattern-only)')
    parser.add_argument('--model', help='specific model to use with LLM provider')
    parser.add_argument('--api-key', help='API key (or use env var)')
    parser.add_argument('--dry-run', action='store_true',
                        help='detect prompts but don\'t send responses')
    parser.add_argument('--log', default='termiclaude.jsonl',
                        help='log file path (default: termiclaude.jsonl)')
    parser.add_argument('--no-log', action='store_true',
                        help='disable logging')
    parser.add_argument('--max-responses', type=int, default=0,
                        help='max auto-responses before stopping (0=unlimited)')
    parser.add_argument('--goal',
                        help='describe what the agent should accomplish '
                             '(enables supervisor health checks)')
    parser.add_argument('--supervise', type=float, default=0, metavar='SECS',
                        help='supervisor check interval in seconds '
                             '(default: 0=off, try 30-120)')
    parser.add_argument('--no-hooks', action='store_true',
                        help='disable Claude Code hooks (use PTY-only mode)')
    parser.add_argument('--no-tmux', action='store_true',
                        help='single-pane mode, no tmux split')
    parser.add_argument('--llm-only', action='store_true',
                        help='skip pattern matching, let LLM decide every response')
    parser.add_argument('--system', metavar='TEXT',
                        help='extra instructions for the supervisor LLM '
                             '(e.g. "always answer no", "choose option 2")')
    parser.add_argument('--ipc-dir',
                        help=argparse.SUPPRESS)  # internal: set by tmux launcher

    args = parser.parse_args()

    log_path = None if args.no_log else args.log

    # Strip leading '--' from REMAINDER
    command = args.command
    if command and command[0] == '--':
        command = command[1:]
    if not command:
        parser.error('no command specified')

    # Auto-extract goal from command if wrapping claude and no explicit --goal
    goal = args.goal
    if not goal and len(command) >= 2 and command[0] in ('claude', 'claude-code'):
        non_flag_args = [a for a in command[1:] if not a.startswith('-')]
        if non_flag_args:
            goal = ' '.join(non_flag_args)

    # If goal provided but no supervise interval, default to 60s
    supervise_interval = args.supervise
    if goal and supervise_interval == 0 and args.provider != 'none':
        supervise_interval = 60.0

    # tmux auto-split: launch in tmux if not already there and not disabled
    if not args.no_tmux and not args.ipc_dir and not os.environ.get('TMUX'):
        ipc = IPC.create()
        # Rebuild args for the worker (add --ipc-dir, pass everything else)
        worker_args = sys.argv[1:]  # original args as-is
        launch_tmux(worker_args, ipc.ipc_dir)
        # launch_tmux does execvp, so we only get here if tmux not found
        # Fall through to single-pane mode
        args.ipc_dir = None

    sup = Supervisor(
        command=command,
        idle_seconds=args.idle,
        provider=args.provider,
        model=args.model,
        api_key=args.api_key,
        dry_run=args.dry_run,
        log_path=log_path,
        max_responses=args.max_responses,
        goal=goal,
        supervise_interval=supervise_interval,
        no_hooks=args.no_hooks,
        ipc_dir=args.ipc_dir,
        llm_only=args.llm_only,
        system_instructions=args.system,
    )

    exit_code = sup.start()
    # Clean up IPC if we created it
    if args.ipc_dir:
        try:
            IPC(args.ipc_dir).cleanup()
        except Exception:
            pass
    sys.exit(exit_code)


if __name__ == '__main__':
    main()
