# Tollkeeper — Design Doc

**Status:** v2 · **Date:** 2026-09-26
**Goal:** A MACC-like tool that auto-switches between the coding tools you already pay for (or get free) when one hits quota — but instead of dumb failover, it routes by task type and cost, preserves context across switches, and tracks spend. Sunk cost first, metered second.

Working CLI name: `router`. Product name: Tollkeeper.

---

## 1. Problem

Heavy AI-coding users burn through every subscription quota. Existing switchers do failover only — they don't choose the *cheapest capable* tool per task, they lose conversation context on every switch, and they tell you nothing about what you're spending.

## 2. Design principles

1. **Bring your own subscription.** Adapters shell out to CLIs the user already installed and authed (`claude`, `cursor-agent`, `gemini`, `codex`, `devin-desktop`, `perplexity`). The tool never handles vendor auth, credentials, or billing. The *only* secret it stores is one optional `OPENROUTER_API_KEY`.
2. **User-ordered hierarchy, metered last.** The user sets the route order (paid subscriptions → free subscriptions → metered); quota you're already paying for (or getting free) is burned before a single API cent is spent.
3. **Adapters isolate vendor churn.** Every vendor-specific hack lives inside one adapter. CLI output formats change; the interface doesn't.
4. **Switches must not start cold.** Session capsules carry task state across tools.
5. **Every run is metered locally.** Your own ledger is the source of truth for quota and spend — vendor CLIs are advisory.
6. **Core is a library, clients are thin.** All policy, routing, budget, and capsule logic lives in importable `router.core` and `router.service` modules. The CLI and the web application are thin clients over the same service. No business logic in client code.

## 3. Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Application (React + FastAPI)  │  CLI (`router ...`)      │
├─────────────────────────────────────────────────────────┤
│  `RouterService` (`router/service.py`)                  │
├─────────────────────────────────────────────────────────┤
│  Policy Engine                                            │
│   ├─ Task classifier (cheap model, cached, with fallback) │
│   ├─ Routing table (task type → adapter order)            │
│   └─ Budget guardrails (daily cap)                        │
├─────────────────────────────────────────────────────────┤
│  Adapter Registry                                         │
│   ├─ ClaudeCodeAdapter   (subprocess: `claude` CLI)      │
│   ├─ CursorAdapter       (subprocess: `cursor-agent`)       │
│   ├─ GeminiAdapter       (subprocess: `gemini` CLI)         │
│   ├─ CodexAdapter        (subprocess: `codex` CLI)          │
│   ├─ DevinAdapter        (subprocess: `devin-desktop`)      │
│   ├─ PerplexityAdapter   (subprocess: `perplexity`/`pplx`)│
│   └─ OpenRouterAdapter   (HTTPS, OpenAI-compatible API)     │
├─────────────────────────────────────────────────────────┤
│  State (sqlite)                                           │
│   ├─ quota_observations   (probe history per adapter)     │
│   ├─ runs                 (spend ledger)                    │
│   └─ capsules             (session snapshots on disk)       │
└─────────────────────────────────────────────────────────┘
```

## 4. Adapter interface spec

Language: Python 3.11+. All vendor specifics behind this contract:

```python
class QuotaState(Enum):
    OK = "ok"
    DEPLETED = "depleted"
    UNKNOWN = "unknown"

@dataclass
class QuotaReport:
    state: QuotaState
    detail: str
    reset_at: datetime | None = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    remaining_percent: float | None = None

@dataclass
class ModelInfo:
    id: str
    provider: str
    input_per_1m: float
    output_per_1m: float
    quality_tier: int
    context_window: int
    billed: Literal["subscription", "metered", "free"]

@dataclass
class RunRequest:
    prompt: str
    model: str | None = None
    timeout_s: int = 120
    workdir: Path = Path(".")
    on_output: Callable[[str, str], None] | None = None

@dataclass
class RunResult:
    output: str
    model_used: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    depleted_mid_run: bool = False

class Adapter(Protocol):
    name: str
    kind: str

    def is_available(self) -> bool: ...
    def probe_quota(self) -> QuotaReport: ...
    def models(self) -> list[ModelInfo]: ...
    def health(self) -> AdapterHealth: ...
    def estimate_cost_usd(self, in_tokens: int, out_tokens: int, model: str) -> float: ...
    def run(self, req: RunRequest) -> RunResult: ...
```

### 4.1 Concrete adapters

| Adapter | Mechanism | Auth | Quota sensing |
|---|---|---|---|
| `ClaudeCodeAdapter` | `claude -p` | user's existing login | sentinel parse + vendor-reported percentage when available + ledger |
| `CursorAdapter` | `cursor-agent -p` | user's existing login | sentinel parse + vendor-reported percentage when available + ledger |
| `GeminiAdapter` | `gemini -p` | user's existing login | sentinel parse + vendor-reported percentage when available + ledger |
| `CodexAdapter` | `codex exec` | user's existing login | sentinel parse + vendor-reported percentage when available + ledger |
| `DevinAdapter` | `devin-desktop chat` | user's existing login | sentinel parse + vendor-reported percentage when available + ledger |
| `PerplexityAdapter` | `perplexity` / `pplx` | user's existing login | sentinel parse when CLI available |
| `OpenRouterAdapter` | `POST openrouter.ai/api/v1/chat/completions` | `OPENROUTER_API_KEY` env | no quota — pure metered; pricing from `/models` endpoint, cached 24h |

New tool = new adapter subclass. Nothing else changes.

## 5. Quota sensing

Three layers, in order of trust:

1. **Explicit probe** — if a vendor ever exposes a real quota endpoint, use it. (None do today in a stable way.)
2. **Sentinel parsing** — each CLI adapter ships regexes matched against stderr/stdout: `rate.?limit`, `quota`, `usage limit`, `429`, etc. On match → `DEPLETED` with `reset_at` parsed if present.
3. **Vendor-reported percentage** — when a CLI emits text like `42% remaining`, the adapter extracts and stores `remaining_percent` alongside the observation.
4. **Local ledger (backstop)** — every probe result is written to `quota_observations`. Policy consults the latest observation before routing. A `DEPLETED` adapter is skipped until its `reset_at` passes or a fresh probe says otherwise.

## 6. Session capsule format

Capsule v1 — JSON on disk, portable across adapters:

```json
{
  "capsule_version": 1,
  "task": {
    "summary": "Add per-IP rate limiting to the Express API",
    "type": "standard",
    "complexity": "standard"
  },
  "plan": {
    "goal": "",
    "todos": [],
    "current_step": 0
  },
  "decisions": [],
  "code_state": {
    "branch": "main",
    "files_touched": [],
    "diff_summary": "no changes",
    "tests_status": "not run"
  },
  "route": {
    "adapter": "claude",
    "model": "default"
  },
  "reason": "sunk cost first: claude healthy, $0 marginal",
  "est_cost_usd": 0.0,
  "output_head": "...",
  "full_output": "...",
  "conversation_digest": "",
  "handoff_notes": "Continue from where the previous adapter left off.",
  "created_by": "claude",
  "created_at": "2026-09-26T20:00:00+00:00",
  "triggered_by": "manual"
}
```

Capsule snapshot triggers: quota depletion mid-run, explicit `router capsule --task`, or task-type downgrade.

## 7. Policy engine

### 7.1 Task classifier

A cheap model (Gemini Flash via OpenRouter, with keyword fallback) labels each request.

- **Classes:** `trivial` · `standard` · `hard`
- Fallback heuristic: keyword matching when the model call fails or no key is present.

### 7.2 Routing table

User-editable TOML in `~/.config/router/config.toml`:

```toml
[routing]
routes = [
  { types = ["trivial"], adapter_order = ["cursor", "gemini"] },
  { types = ["standard"], adapter_order = ["claude", "cursor"] },
  { types = ["hard"], adapter_order = ["claude", "openrouter:anthropic/claude-3.5-sonnet"] },
]
```

Selection algorithm:

1. Filter adapters: `is_available()` and latest quota ≠ `DEPLETED`.
2. Walk the user-configured route order; first healthy adapter wins.
3. Metered adapters sit last by default — subscription options are exhausted before API spend.

### 7.3 Budget guardrails

- Daily metered cap (`daily_cap_usd`, default $5). Hard stop at cap.
- `--dry-run` prints the routing decision + estimated cost without executing.

## 8. Spend ledger (sqlite)

Implemented schema:

```sql
CREATE TABLE runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  task TEXT NOT NULL,
  task_class TEXT NOT NULL,
  adapter TEXT NOT NULL,
  model TEXT NOT NULL,
  dry_run INTEGER NOT NULL,
  est_cost_usd REAL NOT NULL,
  actual_cost_usd REAL
);

CREATE TABLE quota_observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  adapter TEXT NOT NULL,
  state TEXT NOT NULL,
  detail TEXT,
  reset_at TEXT,
  observed_at TEXT NOT NULL,
  remaining_percent REAL
);
```

`router spend` and the dashboard render per-adapter / per-model cost and recent runs.

## 9. CLI surface

```
router app                          # start the control-center application
router status                       # adapter health + quota
router run "task description"       # classify → route → execute (streaming)
router run --dry-run "..."          # show routing decision + cost estimate only
router run --adapter cursor "..."   # force adapter (skip policy)
router spend [--days 7]             # cost summary
router capsule --list               # recent capsules
router capsule --show <path>        # inspect / resume manually
router capsule --task "..."        # create a manual capsule
router dashboard                    # legacy read-only dashboard (compatibility)
```

Config: `~/.config/router/config.toml` — routing table, daily cap.

## 10. UI

The primary UI is a local control-center application (`router app`). It is a thin client over `RouterService`.

- React + TypeScript frontend, built with Vite.
- FastAPI backend on `127.0.0.1`.
- Live task execution via server-sent events.
- Provider health, vendor-reported quota, spend history, capsule browser, settings editor.

A legacy read-only dashboard (`router dashboard`) is kept for compatibility.

### Rules for all UIs

- No business logic in UI code — routing, budgets, capsules stay in `router.core` and `router.service`.
- UIs never hold credentials. The dashboard reads sqlite; adapters shell out to the same CLIs as the CLI.
- Everything a UI can do stays doable from the CLI.

## 11. Build order

- **M0 — Scaffolding.** CLI skeleton, config loader, sqlite init, adapter registry.
- **M1 — Metered core.** `OpenRouterAdapter` + policy stub + `run`/`status`.
- **M2 — First subscription adapter.** `ClaudeCodeAdapter` via CLI subprocess + sentinel list + ledger.
- **M3 — Capsules.** Snapshot on depletion, resume-brief injection.
- **M4 — Cursor + Gemini + Codex + Devin + Perplexity adapters.** Same pattern as M2.
- **M5 — Spend dashboard.** Ledger queries and local web dashboard.
- **M6 — Classifier routing.** Cheap-model task labeling + TOML routing table + dry-run.
- **M7 — Hardening.** Streaming UX, retry/backoff, adapter version warnings, tests.
- **M8 — Control-center application.** FastAPI + React app on top of the existing `RouterService`.

## 12. Risks & open questions

1. **Sentinel brittleness.** Quota detection is heuristic; vendor CLI updates can break it. Mitigation: ledger backstop + adapter version warnings. Accept graceful degradation to `UNKNOWN`.
2. **Capsule quality.** Resume briefs determine whether switches feel seamless. Needs real-world iteration.
3. **Token counting.** Client-side counts are estimates. Fine for budgeting, not for billing disputes.
4. **Vendor ToS.** Driving a vendor's CLI that the user installed and authed is normal automation. Never share credentials between users.
5. **Scope creep.** v1 is single-user, local-only. Team features, cloud, and a TUI are later.

## 13. Non-goals (v1)

- Handling vendor auth, billing, or account management.
- Building a new editor or IDE extension.
- Cloud-hosted anything. All state is local sqlite and local files.
- Managing other people's subscriptions — every user brings their own.

---

*Status 2026-09-26: M0-M8 are implemented and tested. The CLI, legacy dashboard, and full control-center application share the same `RouterService` and core logic.*
