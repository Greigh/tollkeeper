"""Local sqlite ledger: every routing decision and its cost.

This is the source of truth for spend -- vendor CLIs are advisory.
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
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

CREATE TABLE IF NOT EXISTS quota_observations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  adapter TEXT NOT NULL,
  state TEXT NOT NULL,
  detail TEXT,
  reset_at TEXT,
  observed_at TEXT NOT NULL,
  remaining_percent REAL
);
"""


def db_path() -> Path:
    """Ledger DB path under $ROUTER_HOME (created if missing)."""
    root = Path(os.environ.get("ROUTER_HOME", Path.home() / ".local" / "share" / "router"))
    root.mkdir(parents=True, exist_ok=True)
    return root / "ledger.db"


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(db_path())
    c.executescript(SCHEMA)
    columns = {row[1] for row in c.execute("PRAGMA table_info(quota_observations)")}
    if "remaining_percent" not in columns:
        c.execute("ALTER TABLE quota_observations ADD COLUMN remaining_percent REAL")
    return c


def log_run(*, task: str, task_class: str, adapter: str, model: str,
            dry_run: bool, est_cost_usd: float, actual_cost_usd: float | None = None) -> None:
    """Append one routing decision (and its cost) to the runs table."""
    with closing(_conn()) as c, c:
        c.execute(
            "INSERT INTO runs(ts,task,task_class,adapter,model,dry_run,est_cost_usd,actual_cost_usd)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (time.time(), task, task_class, adapter, model,
             int(dry_run), est_cost_usd, actual_cost_usd))


def today_spend() -> float:
    """Actual USD spent by non-dry-run runs since local midnight."""
    with closing(_conn()) as c, c:
        row = c.execute(
            "SELECT COALESCE(SUM(actual_cost_usd),0) FROM runs"
            " WHERE dry_run=0 AND ts > strftime('%s','now','start of day')").fetchone()
    return row[0] or 0.0


def spend_summary(days: int = 30):
    """Per-(adapter, model) run counts and costs over the last ``days`` days."""
    with closing(_conn()) as c, c:
        return c.execute(
            """SELECT adapter, model, COUNT(*),
                      SUM(CASE WHEN dry_run=0 THEN actual_cost_usd ELSE 0 END),
                      SUM(est_cost_usd)
               FROM runs WHERE ts > strftime('%s','now',?)
               GROUP BY adapter, model ORDER BY 4 DESC""",
            (f"-{days} days",)).fetchall()


def log_quota_observation(adapter: str, state: str, detail: str,
                          reset_at: str | None = None,
                          remaining_percent: float | None = None) -> None:
    """Record a vendor quota probe; percent must be 0-100 when present."""
    if remaining_percent is not None and not 0 <= remaining_percent <= 100:
        raise ValueError("remaining_percent must be between 0 and 100")
    with closing(_conn()) as c, c:
        c.execute(
            "INSERT INTO quota_observations(adapter,state,detail,reset_at,observed_at,remaining_percent)"
            " VALUES (?,?,?,?,?,?)",
            (adapter, state, detail, reset_at, datetime.now(timezone.utc).isoformat(),
             remaining_percent))


def latest_quota_observation(adapter: str) -> tuple[str, str, str | None, str, float | None] | None:
    """Most recent (state, detail, reset_at, observed_at, remaining_percent) row."""
    with closing(_conn()) as c, c:
        row = c.execute(
            "SELECT state, detail, reset_at, observed_at, remaining_percent"
            " FROM quota_observations WHERE adapter=? ORDER BY observed_at DESC LIMIT 1",
            (adapter,)).fetchone()
    return row
