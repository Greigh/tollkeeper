# Build timeline — tollkeeper

Derived from `DESIGN_DOC.md`. Status as of 2026-09-26.

## Where things stand

| Milestone | Status | Notes |
|---|---|---|
| M0 — Scaffolding | Done | CLI, config loader, sqlite ledger, adapter registry |
| M1 — Metered core | Done | OpenRouter adapter, hierarchy policy, `run`/`status` — validated against live `/models` |
| M2 — Claude Code adapter | Done | Quota probing + execution with sentinel detection |
| M3 — Capsules | Done | Full capsule implementation with auto-snapshot on depletion and resume brief injection |
| M4 — Multiple adapters | Done | Cursor, Gemini, Devin, Codex, Perplexity adapters implemented |
| M5 — Spend tracking | Done | Web dashboard with cost visualization, quota bars, and avoided cost tracking |
| M6 — Classifier routing | Done | Cheap-model classifier + TOML routing table support |
| M7 — Hardening | Done | Streaming UX, retry/backoff, version warnings, test suite |
| M8 — Control-center application | Done | FastAPI + React application with task execution, live output, settings, and capsule management |

**Current status:** The original M0-M7 milestones are complete. A full local control-center application has been built on top of the same core, with a reusable `RouterService` that both the CLI and application use.

## Completed work

### M2: Claude Code adapter (the hard part)

- [x] `probe_quota()`: run `claude --help`, capture output
- [x] Sentinel list: quota/depletion phrases → `QuotaState`
- [x] `run()`: shell out to `claude -p`, stream output, detect mid-run depletion
- [x] Ledger backstop: record observed headroom per run
- [x] Adapter version detection: warn when the CLI version changes under the sentinels

**Exit criteria met:** `router status` shows real quota state; `router run` routes correctly with depletion detection.

### M4: Multiple subscription adapters

- [x] `CursorAdapter`: probe + run via `cursor-agent`
- [x] `GeminiAdapter`: probe + run via `gemini`
- [x] `DevinAdapter`: probe + run via `devin-desktop`
- [x] `CodexAdapter`: probe + run via `codex`
- [x] `PerplexityAdapter`: probe via `perplexity` or `pplx` (not installed on this machine)
- [x] End-to-end: full hierarchy with depletion simulation tested

**Exit criteria met:** With subscription adapters depleted, routing fails over to the next available adapter or OpenRouter. Full hierarchy tested with simulated depletion.

### M5: Spend dashboard

- [x] `router dashboard`: local-only stdlib server on `127.0.0.1`
- [x] Cost per adapter, quota status, recent runs
- [x] Security hardening: security headers, local-only binding, no local file disclosure

### M3: Capsule implementation

- [x] On depletion mid-run: freeze capsule automatically
- [x] Resume-brief injection: next adapter receives the brief
- [x] `router capsule --list` and `--show` commands
- [x] Enhanced capsule format with plan, decisions, code_state, conversation_digest, handoff_notes

### M6: Smarter routing

- [x] Cheap-model task classifier with keyword fallback
- [x] TOML routing table per task class
- [x] `--dry-run` as the default-safe way to preview policy changes

### M7: Hardening

- [x] Streaming UX for long runs
- [x] Retry/backoff on transient metered failures
- [x] Adapter version warnings wired into `status`
- [x] Test suite: policy unit tests, adapter contract tests, ledger tests, capsule tests, dashboard tests
- [x] Vendor-reported quota percentages: parsed and displayed when a CLI reports them

### M8: Control-center application

- [x] FastAPI application (`router app`) serving a React + TypeScript frontend
- [x] Reusable `RouterService` shared between CLI and application
- [x] Live task execution with server-sent events
- [x] Provider health, vendor-reported quota, spend history, capsules, settings editor
- [x] Responsive layout with overview, run, providers, capsules, and settings views
- [x] Build pipeline: Vite frontend builds into `router/app/static/`

## Test status

```text
Ran 23 tests in 2.079s
OK
```

Tests cover adapters, policy, ledger, capsules, dashboard, and the application API.
