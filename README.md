# Tollkeeper

[![CI](https://github.com/Greigh/tollkeeper/actions/workflows/ci.yml/badge.svg)](https://github.com/Greigh/tollkeeper/actions/workflows/ci.yml)
[![License: CC BY-NC-SA 4.0](https://img.shields.io/badge/License-CC%20BY--NC--SA%204.0-lightgrey.svg)](LICENSE)
![Python 3.14+](https://img.shields.io/badge/python-3.14+-blue.svg)

**A local-first control center and router for AI coding subscriptions.**
See every provider's real quota windows and reset times in one place, then
route each coding task to the tool that still has headroom — sunk cost first,
metered APIs last. Python 3.14+ · React/TypeScript frontend · no cloud, no
shared credentials.

## What it does

- **Quota dashboard** — probes each provider's actual usage API and renders
  per-item bars (e.g. Cursor's *Cursor Models* vs *Other Models* monthly pools,
  Antigravity's Gemini/Claude/GPT model groups) with reset timestamps.
- **Routing** — classifies your task, picks the cheapest capable adapter that
  isn't quota-depleted, runs it, and logs the decision and spend to a local
  SQLite ledger.
- **Credentials stay yours** — read-only access to what each tool already
  stores on your machine (OS keychain, IDE state DBs, config files). Nothing
  is refreshed, rewritten, or sent anywhere but the provider's own API.
- **Capsules** — snapshot task context when a provider runs dry so a switch
  doesn't lose the thread.
- **CLI + web app** — `router` CLI for scripting, local web UI at
  `http://127.0.0.1:8080`.

## Supported providers

| Provider | Quota source | Notes |
|---|---|---|
| Claude | OAuth token (Keychain / credentials file) | 5h + weekly windows |
| Cursor | IDE token → `api2.cursor.sh` Connect RPC | two monthly pools + on-demand spend |
| Gemini / Antigravity | jetski token / IDE state DB → Cloud Code API | per-model-group daily & weekly |
| Codex | `~/.codex/auth.json` | 5h/weekly windows |
| Devin | Devin Desktop local credentials | org credit balance |
| Copilot | `gh` CLI token → `api.github.com/copilot` | monthly snapshots |
| Perplexity | session cookie → `/rest/rate-limit/all` | exact remaining counts |
| Zed | `zed.session` cookie → cloud.zed.dev billing | token spend + edit predictions |
| Amp | CLI output / settings page | — |
| Zcode, DeepSeek, xAI, Kimi, MiniMax | API key balance/usage endpoints | untested against live accounts |
| OpenRouter | `OPENROUTER_API_KEY` | metered fallback + live model pricing |

Details and manual-setup walkthroughs: [PROVIDER_SETUP.md](PROVIDER_SETUP.md).

## Quickstart

```bash
cd ~/workspace/tollkeeper
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cd frontend && npm install && npm run build && cd ..

# Start the local application (opens the browser)
.venv/bin/python -m router app

# Or use the CLI
python3 -m router status
python3 -m router run --dry-run --task "refactor the auth module"
export OPENROUTER_API_KEY=sk-or-...        # metered fallback
python3 -m router run --task "write a python function that parses CSV dates"
python3 -m router spend --days 7
```

### Secrets

API keys and provider session cookies go in the OS keychain or the local
secrets file — never in config:

```bash
router cred set perplexity session_cookie   # prompts securely
router cred backend                          # which keychain backend is in use
```

The **Secrets** page in the UI offers the same thing with named presets
("Perplexity Cookie Key", "Zed Session Cookie", …).

### Local development with hot reload

```bash
.venv/bin/python scripts/dev.py
```

- Backend API: http://127.0.0.1:8080 (uvicorn `--reload` on `router/`)
- Dev frontend: http://127.0.0.1:5173 (Vite HMR, proxies `/api`)

Ctrl+C stops both.

## Install from wheel / standalone bundle

```bash
pip install dist/tollkeeper-0.2.0-py3-none-any.whl
router status && router app
```

Or build a self-contained executable (bundles Python + frontend):

```bash
make bundle
./dist/tollkeeper/tollkeeper
```

## How quota probing works

Each adapter reads the credential its tool already owns, calls the same
endpoint the vendor's UI uses, and reports **remaining** quota as whole-number
percents. Fields the vendor doesn't send are shown as unknown — never
zero-filled or guessed. Credentials are never refreshed or rewritten; when a
token expires the card tells you which app to open once.

## Documentation

- [HOW_TO_USE.md](HOW_TO_USE.md) — full usage guide
- [PROVIDER_SETUP.md](PROVIDER_SETUP.md) — per-provider credential setup
- [PROJECT_NOTES.md](PROJECT_NOTES.md) — project notes and decisions
- [DESIGN_DOC.md](DESIGN_DOC.md) — architecture and routing rationale
- [BUILD_TIMELINE.md](BUILD_TIMELINE.md) — build log

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). In short: `pytest` and
`npm run build` must pass, credentials are read-only, and quota numbers are
never synthesized.

## Security

See [SECURITY.md](SECURITY.md). Report credential-handling issues via a
private GitHub security advisory — never paste tokens into issues.

## License

[CC BY-NC-SA 4.0](LICENSE) — free to use, share, and modify non-commercially
with attribution and share-alike distribution.

## Layout

```
router/
  app/          # local FastAPI application and built frontend assets
  core/         # adapter protocol, policy, ledger, capsules, credentials
  adapters/     # subscription and metered provider integrations
  service.py    # reusable routing and execution workflow
  cli.py        # command-line client
frontend/       # React + TypeScript control-center source
```
