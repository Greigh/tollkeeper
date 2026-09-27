# How to Use Tollkeeper

A comprehensive guide to using the tollkeeper tool for cost-aware AI coding assistance.

## Table of Contents

- [Installation](#installation)
- [Quick Start](#quick-start)
- [Core Concepts](#core-concepts)
- [Commands](#commands)
- [Configuration](#configuration)
- [Adapters](#adapters)
- [Capsules](#capsules)
- [Dashboard](#dashboard)
- [Troubleshooting](#troubleshooting)

## Installation

### Prerequisites

- Python 3.11+
- At least one AI coding tool CLI installed (Claude, Cursor, Gemini, Devin, Codex, etc.)
- Optional: OpenRouter API key for metered fallback

### Setup

```bash
# Clone or navigate to the tollkeeper directory
cd ~/tollkeeper

# Create an isolated Python environment and install the application
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'

# Build the control-center frontend
cd frontend
npm install
npm run build
cd ..
```

### Configuration

Create a config file at `~/.config/router/config.toml`:

```toml
# Daily spending cap for metered APIs (default: $5)
[policy]
daily_cap_usd = 5.0

# Routing table for task-specific routing
[routing]
routes = [
  { types = ["trivial"], adapter_order = ["cursor", "gemini"] },
  { types = ["standard"], adapter_order = ["claude", "cursor"] },
  { types = ["hard"], adapter_order = ["claude", "openrouter:anthropic/claude-3.5-sonnet"] },
]
```

## Quick Start

### 1. Start the Application

```bash
.venv/bin/python -m router app
```

The browser opens the local control center at `http://127.0.0.1:8080`. It provides task execution with live output, route previews, provider status, vendor-reported quota, spend history, capsules, and settings.

### 2. Check Adapter Status from the CLI

```bash
.venv/bin/python -m router status
```

This shows which AI coding tools are available and their quota status.

### 2. Run a Task (Dry Run)

```bash
python3 -m router run --dry-run --task "fix a typo in the README"
```

This shows the routing decision without executing anything.

### 3. Run a Task (Real)

```bash
python3 -m router run --task "refactor the auth module"
```

This executes the task using the best available adapter.

### 4. Check Spending

```bash
python3 -m router spend
```

Shows your spending summary.

### 5. Start Dashboard

```bash
python3 -m router dashboard
```

Opens a local-only web dashboard at `http://127.0.0.1:8080` for visual monitoring.

## Core Concepts

### Sunk Cost First

The router prioritizes subscription adapters (Claude, Cursor, etc.) over metered APIs (OpenRouter) because subscriptions are already paid for.

### Task Classification

Tasks are automatically classified as:
- **trivial**: Simple changes, typos, formatting
- **standard**: Normal coding tasks, bug fixes
- **hard**: Complex refactoring, architecture, performance

### Quota Detection

The router detects when subscription quotas are depleted by:
1. **Explicit probing**: Checking CLI availability
2. **Sentinel parsing**: Regex patterns on CLI output (rate limit, quota, 429, etc.)
3. **Ledger backstop**: Caching quota observations locally

### Session Capsules

When switching adapters (due to quota depletion), the router creates a "capsule" containing:
- Task summary and type
- Execution plan and current step
- Code state (branch, files touched, diff summary)
- Conversation digest
- Handoff notes for the next adapter

## Commands

### `router status`

Shows the health and quota status of all adapters.

```bash
python3 -m router status
```

Output:
```
ADAPTER       KIND         REACHABLE  DETAIL
claude        subscription True       claude is installed and ready; quota state will appear after the first run
cursor        subscription True       cursor is installed and ready; quota state will appear after the first run
gemini        subscription True       gemini is installed and ready; quota state will appear after the first run
devin         subscription True       devin is installed and ready; quota state will appear after the first run
codex         subscription True       codex is installed and ready; quota state will appear after the first run
perplexity    subscription False      perplexity CLI not installed (tried ['perplexity', 'pplx'])
openrouter    metered      True       458 models listed
```

### `router run`

Executes a task with intelligent routing.

```bash
# Basic usage
python3 -m router run --task "add tests for the login function"

# Dry run (preview without executing)
python3 -m router run --dry-run --task "refactor the database layer"

# Force a specific adapter
python3 -m router run --adapter claude --task "fix this bug"

# Force a specific model (for metered adapters)
python3 -m router run --adapter openrouter --model "anthropic/claude-3.5-sonnet" --task "complex task"

# Resume from a capsule
python3 -m router run --resume ~/.local/share/router/capsules/capsule-20260926-175116.json --task "continue work"
```

### `router spend`

Shows spending summary.

```bash
python3 -m router spend
python3 -m router spend --days 7
```

Output:
```
ADAPTER       MODEL                                    RUNS  ACTUAL $  EST $
claude        default                                  5     0.0000    0.0000
openrouter    anthropic/claude-3.5-sonnet              3     0.0420    0.0420

30d totals: actual $0.0420 / estimated $0.0420
```

### `router capsule`

Manages session capsules.

```bash
# List all capsules
python3 -m router capsule --list

# Show a specific capsule's resume brief
python3 -m router capsule --show ~/.local/share/router/capsules/capsule-20260926-175116.json

# Create a manual capsule
python3 -m router capsule --task "important task"
```

### `router app`

Starts the full local control-center application.

```bash
.venv/bin/python -m router app
.venv/bin/python -m router app --port 8081
```

The older read-only dashboard remains available through `router dashboard` for compatibility.

## Configuration

### Config File Location

`~/.config/router/config.toml`

### Configuration Options

```toml
[policy]
daily_cap_usd = 5.0  # Daily spending limit for metered APIs

[routing]
routes = [
  { types = ["trivial"], adapter_order = ["cursor", "gemini"] },
  { types = ["standard"], adapter_order = ["claude", "cursor"] },
  { types = ["hard"], adapter_order = ["claude", "openrouter:anthropic/claude-3.5-sonnet"] },
]
```

### Environment Variables

- `ROUTER_HOME`: Override the default data directory (`~/.local/share/router`)
- `OPENROUTER_API_KEY`: Required for OpenRouter adapter

## Adapters

### Subscription Adapters

These use your existing CLI tools and don't incur additional costs:

| Adapter | CLI Command | Status |
|---------|-------------|--------|
| **Claude** | `claude` | ✅ Implemented |
| **Cursor** | `cursor-agent` | ✅ Implemented |
| **Gemini** | `gemini` | ✅ Implemented |
| **Devin** | `devin-desktop` | ✅ Implemented |
| **Codex** | `codex` | ✅ Implemented |
| **Perplexity** | `perplexity`, `pplx` | ⏳ CLI not available |

### Metered Adapters

- **OpenRouter**: Pay-per-use API access to 400+ models

## Capsules

### What is a Capsule?

A capsule preserves the complete context of a coding session, allowing seamless switching between adapters when one hits quota limits.

### Capsule Contents

- **Task**: Summary and classification
- **Plan**: Current execution plan and progress
- **Decisions**: Key decisions made during the session
- **Code State**: Git branch, files touched, diff summary
- **Conversation Digest**: Auto-generated summary
- **Handoff Notes**: Instructions for the next adapter

### Using Capsules

```bash
# Resume from a capsule
python3 -m router run --resume ~/.local/share/router/capsules/capsule-XXXXXX.json --task "continue"

# The router will inject the resume brief into the prompt
```

## Dashboard

### Features

- **Real-time metrics**: Total spend, active adapters, total runs, avoided cost
- **Quota visualization**: Vendor-reported percentage when available; otherwise available, depleted, or unknown
- **Spend tracking**: Cost breakdown by adapter over time
- **Recent runs**: Execution history with details
- **Auto-refresh**: Updates every 30 seconds
- **Local-only access**: Binds to `127.0.0.1` and does not expose repository files

### Accessing the Dashboard

```bash
python3 -m router dashboard
```

The browser opens automatically. If it does not, open `http://127.0.0.1:8080`.

## Troubleshooting

### Common Issues

#### CLI Not Found

```
perplexity    subscription False      perplexity CLI not installed (tried ['perplexity', 'pplx'])
```

**Solution**: Install the CLI tool or check the PATH.

#### Quota Depleted

```
[quota depleted mid-run - creating capsule for potential re-route]
```

**Solution**: The router detected quota depletion and created a capsule. Use `--resume` to continue with another adapter.

#### Authentication Required

```
Error: Authentication required. Please run 'agent login' first
```

**Solution**: Run the vendor's CLI login command (e.g., `cursor-agent login`).

#### Adapter Unavailable

```
adapter gemini is not available
```

**Solution**: Check that the CLI is installed and in PATH.

### Debugging

Enable verbose output:

```bash
python3 -m router run --task "test" --verbose
```

Check the ledger:

```bash
python3 -c "
from router.core.ledger import _conn
conn = _conn()
cursor = conn.cursor()
cursor.execute('SELECT * FROM runs ORDER BY ts DESC LIMIT 5')
for row in cursor.fetchall():
    print(row)
conn.close()
"
```

## Advanced Usage

### Custom Routing Tables

Create task-specific routing rules:

```toml
[routing]
routes = [
  { types = ["tests", "docs"], adapter_order = ["openrouter:deepseek-v3", "gemini"] },
  { types = ["feature"], adapter_order = ["claude", "cursor"], complexity_filter = ["low", "medium"] },
  { types = ["architecture"], adapter_order = ["claude", "openrouter:claude-opus"] },
]
```

### Capsule Workflow

1. Start a task: `python3 -m router run --task "complex refactor"`
2. If quota depleted, a capsule is created automatically
3. Resume with another adapter: `python3 -m router run --resume <capsule-path> --task "continue"`

### Multiple Adapters

The router will automatically failover through the hierarchy:
1. Claude (subscription)
2. Cursor (subscription)
3. Gemini (subscription)
4. Devin (subscription)
5. Codex (subscription)
6. OpenRouter (metered)

## Best Practices

1. **Start with dry runs**: Always use `--dry-run` first to see routing decisions
2. **Monitor quota**: Check `router status` regularly
3. **Use capsules**: Resume from capsules to avoid losing work
4. **Set spending caps**: Configure `daily_cap_usd` to prevent unexpected costs
5. **Review spend**: Use `router spend` and the dashboard to track usage

## Security Notes

- The only secret stored is `OPENROUTER_API_KEY` (if used)
- No vendor credentials are stored or transmitted
- All data is stored locally in `~/.local/share/router`
- The dashboard is local-only (localhost)

## Contributing

See the [BUILD_TIMELINE.md](BUILD_TIMELINE.md) for the development roadmap and current status.