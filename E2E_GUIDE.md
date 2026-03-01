# End-to-End Guide: Autonomous Claude Code with termiclaude

## Setup (one time)

```bash
# 1. Install termiclaude
cd ~/dev/termiclaude
pip install -e .

# 2. Choose your supervisor provider:

# Option A: Ollama (recommended — free, fast, private)
# Make sure ollama is running on your server/machine
export OLLAMA_HOST=192.168.8.107  # your ollama host
ollama pull qwen3:4b              # run on the ollama machine

# Option B: Anthropic API
export ANTHROPIC_API_KEY=sk-ant-...

# Option C: Another Claude Code instance (from regular terminal only)
# No setup needed — uses your Max subscription

# 3. Verify it works
termiclaude --idle 2 --no-log python3 -c "
r = input('Continue? [Y/n]: ')
print(f'got: {r}')
"
# Should auto-answer "y" after 2 seconds
```

## Use Case 1: Simple task, just auto-approve

No supervisor needed. Just let Claude work and approve everything.

```bash
cd ~/my-project
termiclaude claude "add a health check endpoint to the API at /api/health that returns {status: ok}"
```

What happens:
- Claude starts, reads your code, writes files
- Each file write/command shows an approval prompt
- termiclaude presses Enter (Yes) on each one
- Claude finishes, you review the changes with `git diff`

## Use Case 2: Bigger task with supervisor

For longer tasks where Claude might get distracted.

```bash
cd ~/my-project
termiclaude --provider ollama claude "add JWT authentication to the login endpoint. Use jsonwebtoken package. Add middleware for protected routes. Add tests."
```

What happens:
- Same as above, but every 60 seconds the supervisor checks progress
- If Claude starts doing something unrelated (reformatting, refactoring
  other modules, yak-shaving), the supervisor sends a redirect
- Everything logged to `termiclaude.jsonl`

## Use Case 3: Overnight autonomous session

Leave Claude working on a big task while you sleep.

```bash
cd ~/my-project

# Create a focused CLAUDE.md for the task
cat > CLAUDE.md << 'EOF'
# Task: Comprehensive Test Suite
Add unit tests for all modules in src/. Target 80% coverage.
Focus on: auth, api, database modules.
Do NOT refactor existing code. Only add tests.
Do NOT modify package.json beyond adding test dependencies.
EOF

# Launch with supervisor, safety limits, and detailed logging
termiclaude \
  --provider ollama \
  --supervise 30 \
  --max-responses 100 \
  --idle 6 \
  --log overnight-$(date +%Y%m%d).jsonl \
  claude "Follow the instructions in CLAUDE.md. Add comprehensive tests for all modules."

# Next morning: review
cat overnight-*.jsonl | python3 -c "
import sys, json
for line in sys.stdin:
    d = json.loads(line)
    ev = d['event']
    if ev == 'respond':
        print(f\"{d['ts'][11:19]} AUTO: sent {repr(d['response'])} ({d['source']})\")
    elif ev == 'supervise' and d['status'] != 'on_track':
        print(f\"{d['ts'][11:19]} WARN: {d['status']} — {d['reasoning']}\")
    elif ev == 'intervene':
        print(f\"{d['ts'][11:19]} INTERVENE: {d['message'][:80]}\")
    elif ev in ('start', 'exit'):
        print(f\"{d['ts'][11:19]} {ev.upper()}\")
"

# Review the actual code changes
git diff
git diff --stat
```

## Use Case 4: Multiple agents in parallel

Run several termiclaude instances in different tmux panes.

```bash
# Terminal 1: auth module
cd ~/my-project
termiclaude --provider ollama --log agent1.jsonl \
  claude "add JWT auth to src/auth/"

# Terminal 2: tests (in a separate worktree to avoid conflicts)
cd ~/my-project
git worktree add /tmp/proj-tests main
cd /tmp/proj-tests
termiclaude --provider ollama --log agent2.jsonl \
  claude "add tests for src/api/"

# Terminal 3: docs
cd ~/my-project
termiclaude --provider ollama --log agent3.jsonl \
  claude "add JSDoc comments to all exported functions in src/"
```

## Use Case 5: Dry run first, then go

See what would be auto-approved before committing to it.

```bash
cd ~/my-project

# First: dry run — see what prompts Claude triggers
termiclaude --dry-run --idle 6 \
  claude "delete all unused dependencies and clean up imports"
# Watch for [DRY RUN] messages
# Ctrl+C when you've seen enough

# If it looks safe:
termiclaude --provider ollama \
  claude "delete all unused dependencies and clean up imports"
```

## Prepared use case: ready to run

Copy-paste this into a regular terminal (not inside Claude Code):

```bash
# Create a test project
mkdir -p /tmp/termiclaude-demo && cd /tmp/termiclaude-demo
git init
cat > app.py << 'PYEOF'
from flask import Flask, jsonify, request

app = Flask(__name__)

users = {}

@app.route('/users', methods=['GET'])
def list_users():
    return jsonify(list(users.values()))

@app.route('/users', methods=['POST'])
def create_user():
    data = request.json
    user_id = str(len(users) + 1)
    users[user_id] = {'id': user_id, 'name': data['name'], 'email': data['email']}
    return jsonify(users[user_id]), 201

@app.route('/users/<user_id>', methods=['GET'])
def get_user(user_id):
    if user_id in users:
        return jsonify(users[user_id])
    return jsonify({'error': 'not found'}), 404

if __name__ == '__main__':
    app.run(debug=True)
PYEOF

cat > requirements.txt << 'EOF'
flask
EOF

git add -A && git commit -m "initial: basic flask CRUD app"

# Now let termiclaude + Claude autonomously add features
termiclaude --provider ollama --supervise 30 \
  claude "This is a Flask API. Please: 1) Add input validation to POST /users (name and email required, email must be valid format). 2) Add PUT /users/<id> and DELETE /users/<id> endpoints. 3) Add proper error handling with consistent error response format. 4) Add tests using pytest. Create a test_app.py file. Do not modify requirements.txt beyond adding pytest."

# When it's done:
git diff --stat
python3 -m pytest test_app.py -v  # run the tests Claude wrote
cat termiclaude.jsonl | grep -c '"event": "respond"'  # how many auto-approvals
```

## Troubleshooting

### termiclaude doesn't respond to prompts
- Increase `--idle` (maybe Claude is still outputting)
- Check `termiclaude.jsonl` for what's being detected
- Try `--dry-run` to see pattern matches without sending

### Supervisor keeps intervening unnecessarily
- Increase `--supervise` interval (e.g., 120 seconds)
- Make `--goal` more specific
- Supervisor is conservative by design; false positives are rare

### Claude Code refuses to start (nested session error)
- termiclaude clears CLAUDECODE env var automatically
- If still failing, run from a regular terminal, not inside Claude Code

### Ollama connection issues
- Check: `curl http://$OLLAMA_HOST:11434/api/tags`
- OLLAMA_HOST can be bare IP (`192.168.1.100`), host:port, or full URL
- Make sure qwen3:4b is pulled: `ollama pull qwen3:4b`
