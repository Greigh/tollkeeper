"""Subscription adapters (M2 implementation).

v1 detects whether the vendor CLI is on PATH. M2 adds real quota probing
(CLI output parsing, sentinel detection) and execution (shelling out to the CLI).
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import select
import shutil
import sqlite3
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ..core.adapter import (AdapterHealth, ModelInfo, QuotaReport, QuotaState,
                             RunRequest, RunResult)
from ..core import credentials
from ..core.desktop import find_desktop_app
from ..core.ledger import latest_quota_observation, log_quota_observation


def _router_config_value(section: str, key: str) -> str | None:
    config_path = Path(os.environ.get(
        "ROUTER_CONFIG", Path.home() / ".config" / "router" / "config.toml"))
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    value = (config.get(section) or {}).get(key)
    return str(value) if value else None


def _parse_iso_datetime(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/131.0.0.0 Safari/537.36")


def _browser_headers(cookie: str, accept: str = "application/json",
                     referer: str | None = None) -> dict[str, str]:
    """Browser-like headers for cookie-authed JSON endpoints.

    Cloudflare fronting perplexity.ai/zed.dev/ampcode.com challenges
    non-browser UAs intermittently, so send the full same-origin set."""
    headers = {"Cookie": cookie, "Accept": accept, "User-Agent": _BROWSER_UA}
    if referer:
        headers["Referer"] = referer
        headers["Sec-Fetch-Site"] = "same-origin"
        headers["Sec-Fetch-Mode"] = "cors"
        headers["Sec-Fetch-Dest"] = "empty"
    return headers


def _num(value) -> float | None:
    """Coerce a JSON value to float; None stays None."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _window_kind(name: str) -> str:
    """Classify a window name for the per-item quota bars."""
    n = name.lower()
    if any(token in n for token in ("week", "7d", "secondary", "biw")):
        return "weekly"
    if any(token in n for token in ("month", "30d", "cycle", "billing", "period")):
        return "monthly"
    if any(token in n for token in
           ("5h", "hour", "daily", "day", "interval", "session", "primary")):
        return "daily"
    return "other"


def _meters_from_windows(
    windows: list[tuple[str, float, datetime | None]],
    default_label: str,
) -> list[dict[str, object]]:
    """Group (name, remaining%, reset) readings into per-item meters for the
    UI: [{label, windows: [{kind, percent, resetAt?}]}]. Names like
    "Gemini Models 5h" split into label "Gemini Models" + window "5h";
    "codex_foo-5h" splits on the first dash; "7d opus" maps to Opus."""
    order: list[str] = []
    grouped: dict[str, list[dict[str, object]]] = {}
    for name, remaining, reset in windows:
        label, kind_name = default_label, name
        stripped = name.strip()
        if stripped.lower().endswith(" opus"):
            label, kind_name = "Opus", stripped[:-5].strip()
        elif " " in stripped:
            label, _, kind_name = stripped.rpartition(" ")
        elif "-" in stripped and stripped.lower() not in ("5h", "weekly", "daily"):
            label, _, kind_name = stripped.partition("-")
        window: dict[str, object] = {
            "kind": _window_kind(kind_name or name),
            "percent": float(round(remaining)),
        }
        if reset is not None:
            window["resetAt"] = reset.isoformat()
        if label not in grouped:
            grouped[label] = []
            order.append(label)
        grouped[label].append(window)
    return [{"label": label, "windows": grouped[label]} for label in order]


class _WhichAdapter:
    """Base for subscription-CLI adapters.

    Resolves the vendor CLI (``candidates`` on PATH) or desktop app, runs
    prompts through it, and detects quota depletion via ``_sentinel_patterns``
    in the output. Subclasses override ``_usage_quota`` for real vendor reads.
    """

    kind = "subscription"
    candidates: tuple[str, ...] = ()
    label = "unnamed"

    def __init__(self) -> None:
        self.name = self.label
        self._cli_path: str | None = None
        self._desktop_path: Path | None = None
        self._sentinel_patterns: tuple[str, ...] = ()
        self._last_version: str | None = None

    def _get_cli_version(self) -> str | None:
        """Get the CLI version for version tracking."""
        if not self._cli_path:
            return None

        try:
            # Try common version flags
            for flag in ["--version", "-v", "version"]:
                try:
                    result = subprocess.run(
                        [self._cli_path, flag],
                        capture_output=True,
                        text=True,
                        timeout=5
                    )
                    if result.returncode == 0 and result.stdout:
                        # Extract version from output (common patterns)
                        output = result.stdout.strip()
                        # Look for version patterns like "1.2.3", "v1.2.3", "version 1.2.3"
                        version_match = re.search(r'(\d+\.\d+\.\d+)', output)
                        if version_match:
                            return version_match.group(1)
                        # If no version pattern found, return first line
                        return output.split('\n')[0][:50]
                except (subprocess.TimeoutExpired, FileNotFoundError):
                    continue
        except Exception:
            pass
        return None

    def is_available(self) -> bool:
        """True when a CLI candidate or the desktop app is found."""
        if self._cli_path is None:
            self._cli_path = next((c for c in self.candidates if shutil.which(c)), None)
        if self._desktop_path is None:
            self._desktop_path = find_desktop_app(self.name)
        return self._cli_path is not None or self._desktop_path is not None

    def _install_source(self) -> str:
        if self._cli_path:
            return f"CLI `{self._cli_path}`"
        if self._desktop_path:
            return f"desktop app `{self._desktop_path}`"
        return "not installed"

    def probe_quota(self, force: bool = False) -> QuotaReport:
        """Default probe: cached observation (<1h), else a CLI liveness check."""
        if not self.is_available():
            return QuotaReport(
                state=QuotaState.UNKNOWN,
                detail=f"{self.name} not installed (CLI tried: {list(self.candidates)})",
                observed_at=datetime.now(timezone.utc)
            )

        # Desktop-only install: we can see the app, but we can't probe quota without a CLI.
        if not self._cli_path and self._desktop_path:
            report = QuotaReport(
                state=QuotaState.UNKNOWN,
                detail=f"{self.name} desktop app installed ({self._desktop_path}); add the CLI to PATH to probe quota",
                observed_at=datetime.now(timezone.utc)
            )
            log_quota_observation(self.name, report.state.value, report.detail)
            return report

        recent = latest_quota_observation(self.name)
        if recent and not force:
            state, detail, reset_at, observed_at, remaining_percent = recent
            obs_dt = datetime.fromisoformat(observed_at)
            # Ensure both datetimes are timezone-aware for comparison
            if obs_dt.tzinfo is None:
                obs_dt = obs_dt.replace(tzinfo=timezone.utc)
            # Use cached observation if less than 1 hour old
            if (datetime.now(timezone.utc) - obs_dt).total_seconds() < 3600:
                reset_dt = datetime.fromisoformat(reset_at) if reset_at else None
                if reset_dt and reset_dt.tzinfo is None:
                    reset_dt = reset_dt.replace(tzinfo=timezone.utc)
                return QuotaReport(
                    state=QuotaState(state),
                    detail=detail,
                    reset_at=reset_dt,
                    observed_at=obs_dt,
                    remaining_percent=remaining_percent
                )

        assert self._cli_path is not None
        # Fresh probe: run a lightweight command
        try:
            result = subprocess.run(
                [self._cli_path, "--help"],
                capture_output=True,
                text=True,
                timeout=10
            )
            # If help works, assume OK for now (sentinel detection on real runs)
            report = QuotaReport(
                state=QuotaState.OK,
                detail=f"{self.name} is installed ({self._install_source()}); quota state will appear after the first run",
                observed_at=datetime.now(timezone.utc)
            )
            log_quota_observation(self.name, report.state.value, report.detail)
            return report
        except subprocess.TimeoutExpired:
            return QuotaReport(
                state=QuotaState.UNKNOWN,
                detail=f"{self.name} probe timed out; check that the CLI is responsive",
                observed_at=datetime.now(timezone.utc)
            )
        except Exception as e:
            return QuotaReport(
                state=QuotaState.UNKNOWN,
                detail=f"{self.name} probe failed: {e}",
                observed_at=datetime.now(timezone.utc)
            )

    def models(self) -> list[ModelInfo]:
        """A single free "default" model — subscription CLIs don't expose pricing."""
        return [
            ModelInfo(
                id="default",
                provider=self.label,
                input_per_1m=0.0,
                output_per_1m=0.0,
                quality_tier=1,
                context_window=200000,
                billed="subscription"
            )
        ]

    def health(self, force_probe: bool = False) -> AdapterHealth:
        """Availability + quota probe; notes CLI version changes."""
        if not self.is_available():
            return AdapterHealth(
                self.name, False,
                QuotaReport(
                    state=QuotaState.UNKNOWN,
                    detail=f"{self.name} not installed (CLI tried: {list(self.candidates)})",
                    observed_at=datetime.now(timezone.utc)
                ))

        quota = self.probe_quota(force=force_probe)
        health = AdapterHealth(self.name, True, quota)

        # Check for version changes (CLI only; desktop apps don't expose a version here)
        current_version = self._get_cli_version()
        if current_version and self._last_version and self._last_version != current_version:
            health.note = f"version changed: {self._last_version} → {current_version}"
        self._last_version = current_version

        return health

    def estimate_cost_usd(self, in_tokens: int, out_tokens: int,
                          model: str = "default") -> float:
        """Always 0 — the subscription is already paid for."""
        return 0.0

    def run(self, req: RunRequest) -> RunResult:
        """Execute via the provider CLI, streaming output and watching for
        depletion sentinels; returns depleted_mid_run when they match."""
        if not self.is_available():
            raise RuntimeError(f"{self.name} is not installed")
        if not self._cli_path:
            raise RuntimeError(
                f"{self.name} desktop app is installed but no CLI was found on PATH; "
                "add the CLI to PATH to enable execution."
            )

        # Build CLI command - can be overridden by subclasses
        cmd = self._build_command(req)

        # Run CLI with the prompt (base implementation - subclasses may override)
        process = None
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=req.workdir
            )

            # Stream output in real-time for better UX
            stdout_lines: list[str] = []
            stderr_lines: list[str] = []

            def read_stdout():
                """Drain stdout, forwarding each line to on_output."""
                if process is None or process.stdout is None:
                    return
                for line in process.stdout:
                    if req.on_output:
                        req.on_output("stdout", line)
                    else:
                        print(line, end='', flush=True)
                    stdout_lines.append(line)

            def read_stderr():
                """Drain stderr, forwarding each line to on_output."""
                if process is None or process.stderr is None:
                    return
                for line in process.stderr:
                    if req.on_output:
                        req.on_output("stderr", line)
                    else:
                        print(line, end='', flush=True, file=sys.stderr)
                    stderr_lines.append(line)

            import threading
            stdout_thread = threading.Thread(target=read_stdout)
            stderr_thread = threading.Thread(target=read_stderr)

            stdout_thread.start()
            stderr_thread.start()

            # Wait with timeout
            stdout_thread.join(timeout=req.timeout_s)
            stderr_thread.join(timeout=req.timeout_s)

            if stdout_thread.is_alive() or stderr_thread.is_alive():
                process.kill()
                raise RuntimeError(f"{self.name} run timed out after {req.timeout_s}s")

            process.wait()
            stdout = ''.join(stdout_lines)
            stderr = ''.join(stderr_lines)

            # Check for quota depletion in output
            combined_output = stdout + stderr
            is_depleted, reset_time = self._check_sentinels(combined_output)
            remaining_percent = self._extract_quota_percent(combined_output if is_depleted else stderr)

            if is_depleted:
                # Log the depletion observation
                log_quota_observation(
                    self.name,
                    QuotaState.DEPLETED.value,
                    f"Sentinel matched: quota depleted",
                    reset_time,
                    remaining_percent
                )
                return RunResult(
                    output=stdout,
                    model_used="default",
                    input_tokens=0,
                    output_tokens=0,
                    cost_usd=0.0,
                    depleted_mid_run=True
                )

            # Successful run (even if output is empty)
            if remaining_percent is not None:
                log_quota_observation(
                    self.name,
                    QuotaState.OK.value,
                    f"Vendor reported {remaining_percent:g}% quota remaining",
                    remaining_percent=remaining_percent
                )
            return RunResult(
                output=stdout if stdout else stderr if stderr else "Command completed with no output",
                model_used="default",
                input_tokens=0,  # Token counting for subscription CLIs is M2+
                output_tokens=0,
                cost_usd=0.0,
                depleted_mid_run=False
            )

        except subprocess.TimeoutExpired:
            if process is not None:
                process.kill()
            raise RuntimeError(f"{self.name} run timed out after {req.timeout_s}s")
        except FileNotFoundError:
            raise RuntimeError(f"{self.name} CLI not found: {self._cli_path}")
        except Exception as e:
            if process is not None:
                with contextlib.suppress(Exception):
                    process.kill()
            raise RuntimeError(f"{self.name} run failed: {e}")

    def _build_command(self, req: RunRequest) -> list[str]:
        """Build the CLI command for this adapter. Default is `-p` for prompt."""
        assert self._cli_path is not None
        return [self._cli_path, "-p", req.prompt]

    def _extract_quota_percent(self, output: str) -> float | None:
        patterns = (
            # "usage limit: 42% remaining"
            r"(?:quota|usage|limit)[^\n%]{0,80}?(\d{1,3}(?:\.\d+)?)\s*%\s*(?:remaining|left)",
            # "42% of your weekly limit left"
            r"(\d{1,3}(?:\.\d+)?)\s*%[^\n]{0,60}?(?:quota|usage|limit)[^\n]{0,30}?(?:remaining|left)",
            # "remaining: 42%"
            r"(?:remaining|used|available)[^\n%]{0,20}?(\d{1,3}(?:\.\d+)?)\s*%",
            # "42% remaining"
            r"(\d{1,3}(?:\.\d+)?)\s*%\s*(?:remaining|left)",
        )
        for pattern in patterns:
            match = re.search(pattern, output, re.IGNORECASE)
            if match:
                value = float(match.group(1))
                if 0 <= value <= 100:
                    return value
        return None

    def _check_sentinels(self, output: str) -> tuple[bool, str | None]:
        """Check output for quota depletion sentinels. Returns (is_depleted, reset_time)."""
        for pattern in self._sentinel_patterns:
            if re.search(pattern, output, re.IGNORECASE):
                # Try to extract reset time
                reset_match = re.search(r"(\d{4}-\d{2}-\d{2})|(\d+:\d{2})|(\d+ [hms])", output, re.IGNORECASE)
                reset_time = reset_match.group(0) if reset_match else None
                return True, reset_time
        return False, None


class ClaudeCodeAdapter(_WhichAdapter):
    """Claude Code — OAuth usage endpoint via ~/.claude creds or Keychain."""

    label = "claude"
    candidates = ("claude",)

    def __init__(self) -> None:
        super().__init__()
        self._sentinel_patterns = (
            r"rate.?limit",
            r"quota",
            r"usage limit",
            r"try again at",
            r"429",
            r"too many requests",
            r"monthly limit",
            r"daily limit",
        )

    def _build_command(self, req: RunRequest) -> list[str]:
        """Claude Code uses `-p` for prompt, same as default."""
        assert self._cli_path is not None
        return [self._cli_path, "-p", req.prompt]

    def _claude_credentials(self) -> dict | None:
        """Read the Claude Code OAuth blob (read-only; never refreshed).

        Sources: `~/.claude/.credentials.json`, then the macOS Keychain
        (`Claude Code-credentials` — where the CLI stores it on macOS)."""
        credentials_path = Path.home() / ".claude" / ".credentials.json"
        data = None
        try:
            data = json.loads(credentials_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        if not isinstance(data, dict) and sys.platform == "darwin":
            try:
                result = subprocess.run(
                    ["security", "find-generic-password", "-w",
                     "-s", "Claude Code-credentials"],
                    capture_output=True, text=True, timeout=5)
                if result.returncode == 0 and result.stdout.strip():
                    data = json.loads(result.stdout.strip())
            except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
                pass
        return data if isinstance(data, dict) else None

    def _credential_state(self) -> str:
        """missing | expired | present — drives the probe failure message."""
        data = self._claude_credentials()
        if not data:
            return "missing"
        oauth = data.get("claudeAiOauth") or {}
        expires_at = oauth.get("expiresAt")
        if oauth.get("accessToken") and isinstance(expires_at, (int, float)) \
                and expires_at / 1000 <= time.time():
            return "expired"
        return "present" if oauth.get("accessToken") else "missing"

    def _claude_oauth_token(self) -> str | None:
        data = self._claude_credentials()
        if not data:
            return None
        oauth = data.get("claudeAiOauth") or {}
        token = oauth.get("accessToken")
        if not token:
            return None
        expires_at = oauth.get("expiresAt")
        if isinstance(expires_at, (int, float)) and expires_at / 1000 <= time.time():
            return None  # expired; read-only policy — degrade, don't refresh
        return token

    @staticmethod
    def _parse_iso_reset(value) -> datetime | None:
        return _parse_iso_datetime(value)

    def _usage_quota(self) -> QuotaReport | None:
        """Call the Anthropic OAuth usage endpoint for real utilization numbers.

        The token needs the `user:profile` scope; `user:inference`-only tokens
        get a 403, in which case we fall back to sentinel-based detection.
        """
        token = self._claude_oauth_token()
        if not token:
            return None

        request = urllib.request.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
            return None

        # utilization is percent *used* (0-100); buckets may be null on plans
        # that don't include that window.
        windows: list[tuple[str, str, float, datetime | None]] = []
        for key, name, extra_prefix in (
            ("five_hour", "5h", "daily"),
            ("seven_day", "7d", "weekly"),
            ("seven_day_opus", "7d opus", "opusWeekly"),
        ):
            bucket = data.get(key)
            if not isinstance(bucket, dict):
                continue
            utilization = bucket.get("utilization")
            if utilization is None:
                continue
            remaining = float(round(max(0.0, 100.0 - float(utilization))))
            windows.append((name, extra_prefix, remaining, self._parse_iso_reset(bucket.get("resets_at"))))

        if not windows:
            return None

        extra_usage = data.get("extra_usage") or {}
        extra_usage_detail = None
        if extra_usage.get("is_enabled") and extra_usage.get("used_credits") is not None:
            used = float(extra_usage["used_credits"])
            limit = extra_usage.get("monthly_limit")
            extra_usage_detail = (
                f"${used:.2f}/${float(limit):.2f} extra usage" if limit else f"${used:.2f} extra usage"
            )

        detail = f"Claude quota: {', '.join(f'{name} {remaining:g}% remaining' for name, _, remaining, _ in windows)}"
        if extra_usage_detail:
            detail += f" ({extra_usage_detail})"

        bar_percent = min(remaining for _, _, remaining, _ in windows)
        resets = [reset for _, _, _, reset in windows if reset is not None]
        reset_at = min(resets) if resets else None

        extra: dict[str, object] = {}
        for _, prefix, remaining, reset in windows:
            extra[f"{prefix}Percent"] = remaining
            if reset:
                extra[f"{prefix}ResetAt"] = reset.isoformat()
        if extra_usage.get("is_enabled"):
            if extra_usage.get("used_credits") is not None:
                extra["extraUsageUsd"] = float(extra_usage["used_credits"])
            if extra_usage.get("monthly_limit") is not None:
                extra["extraUsageLimitUsd"] = float(extra_usage["monthly_limit"])
        extra["meters"] = _meters_from_windows(
            [(name, remaining, reset) for name, _, remaining, reset in windows],
            "Claude")

        return QuotaReport(
            state=QuotaState.OK if bar_percent > 0 else QuotaState.DEPLETED,
            detail=detail,
            observed_at=datetime.now(timezone.utc),
            remaining_percent=bar_percent,
            reset_at=reset_at,
            extra=extra,
        )

    def probe_quota(self, force: bool = False) -> QuotaReport:
        """Prefer the real vendor usage read (logged); fall back to the base probe."""
        usage_report = self._usage_quota()
        if usage_report:
            log_quota_observation(
                self.name,
                usage_report.state.value,
                usage_report.detail,
                usage_report.reset_at.isoformat() if usage_report.reset_at else None,
                remaining_percent=usage_report.remaining_percent,
            )
            return usage_report
        state = self._credential_state()
        if state == "expired":
            report = QuotaReport(
                state=QuotaState.UNKNOWN,
                detail="claude OAuth token expired — run `claude` once to refresh it",
                observed_at=datetime.now(timezone.utc),
            )
            log_quota_observation(self.name, report.state.value, report.detail)
            return report
        if state == "present":
            return QuotaReport(
                state=QuotaState.UNKNOWN,
                detail="claude credentials found but usage endpoint unreachable or scope-limited",
                observed_at=datetime.now(timezone.utc),
            )
        return super().probe_quota(force=force)


class CursorAdapter(_WhichAdapter):
    """Cursor — cursor-agent CLI; quota from output sentinels only."""

    label = "cursor"
    candidates = ("cursor-agent", "cursor")

    def __init__(self) -> None:
        super().__init__()
        self._sentinel_patterns = (
            r"rate.?limit",
            r"quota",
            r"usage limit",
            r"429",
            r"too many requests",
            r"monthly limit",
            r"daily limit",
            r"request limit",
            r"api limit",
            r"credit limit",
        )

    def _build_command(self, req: RunRequest) -> list[str]:
        """Cursor Agent uses `-p` for print mode with prompt as argument."""
        assert self._cli_path is not None
        return [self._cli_path, "-p", req.prompt]

    @staticmethod
    def _jwt_is_expired(token: str) -> bool:
        """Check a JWT's exp claim without verifying the signature (read-only use)."""
        try:
            payload = token.split(".")[1]
            padded = payload + "=" * (-len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(padded))
            exp = claims.get("exp")
            return exp is None or float(exp) <= time.time()
        except (IndexError, ValueError, TypeError):
            return True

    def _cursor_access_token(self) -> str | None:
        """Read the Cursor access token (read-only; never refreshed here).

        The token is a short-lived JWT. Refreshing it requires impersonating
        Cursor's first-party OAuth client, so we just refuse expired tokens
        and let the user's own CLI/IDE refresh them.
        """
        # 1. cursor-agent CLI auth.json
        auth_path = Path.home() / ".config" / "cursor" / "auth.json"
        try:
            data = json.loads(auth_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            token = data.get("accessToken") or data.get("access_token")
            if token:
                return token

        # 2. Cursor IDE SQLite (macOS / Linux).
        app_state_paths = [
            Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb",
            Path.home() / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb",
        ]
        for state_path in app_state_paths:
            if not state_path.exists():
                continue
            try:
                conn = sqlite3.connect(state_path)
                try:
                    row = conn.execute(
                        "SELECT value FROM ItemTable WHERE key = 'cursorAuth/accessToken'"
                    ).fetchone()
                finally:
                    conn.close()
                if row and row[0]:
                    return row[0]
            except (OSError, sqlite3.Error):
                continue

        return None

    def _cursor_plan_name(self) -> str | None:
        """Plan name (`pro`, `business`, ...) from the IDE state DB."""
        for state_path in (
            Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb",
            Path.home() / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb",
        ):
            if not state_path.exists():
                continue
            try:
                conn = sqlite3.connect(state_path)
                try:
                    row = conn.execute(
                        "SELECT value FROM ItemTable WHERE key = 'cursorAuth/stripeMembershipType'"
                    ).fetchone()
                finally:
                    conn.close()
                if row and row[0]:
                    return str(row[0]).capitalize()
            except (OSError, sqlite3.Error):
                continue
        return None

    def _cursor_rpc(self, method: str, token: str) -> dict | None:
        """POST a Connect-RPC call to api2.cursor.sh; None on any failure."""
        request = urllib.request.Request(
            f"https://api2.cursor.sh/aiserver.v1.DashboardService/{method}",
            data=b"{}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Connect-Protocol-Version": "1",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, OSError,
                json.JSONDecodeError, TimeoutError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _parse_millis(value) -> datetime | None:
        try:
            return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            return None

    def _usage_quota(self) -> QuotaReport | None:
        """Cursor's Connect DashboardService — per-pool monthly usage.

        ``planUsage`` carries two pools: ``autoPercentUsed`` (Cursor Models:
        Composer/Grok/Vega) and ``apiPercentUsed`` (Other Models), each a
        percent *used* of the monthly included spend. ``spendLimitUsage``
        is on-demand dollar spend; ``GetHardLimit`` says whether it's enabled.
        """
        token = self._cursor_access_token()
        if not token or self._jwt_is_expired(token):
            return None

        data = self._cursor_rpc("GetCurrentPeriodUsage", token)
        if data is None:
            return None

        reset_at = self._parse_millis(data.get("billingCycleEnd"))
        cycle_start = self._parse_millis(data.get("billingCycleStart"))
        plan_name = self._cursor_plan_name()

        plan_usage = data.get("planUsage") or {}
        total_used = plan_usage.get("totalPercentUsed")
        if total_used is None:
            # Teams accounts report dollar spend instead of percentages.
            total_spend = data.get("totalSpend")
            if total_spend is None:
                return None
            spend_usd = round(int(total_spend) / 100, 2)
            detail = f"Cursor usage: ${spend_usd:.2f} spent this cycle"
            extra: dict[str, object] = {"spendUsd": spend_usd}
            if plan_name:
                extra["planName"] = plan_name
            if cycle_start:
                extra["billingCycleStart"] = cycle_start.isoformat()
            if reset_at:
                extra["billingCycleEnd"] = reset_at.isoformat()
            return QuotaReport(
                state=QuotaState.OK,
                detail=detail,
                observed_at=datetime.now(timezone.utc),
                remaining_percent=None,
                reset_at=reset_at,
                extra=extra,
            )

        # Percentages arrive as *used*; remaining is what the bar shows.
        remaining = float(round(max(0.0, 100.0 - float(total_used))))
        pools = (
            ("Cursor Models", "autoPercentUsed"),
            ("Other Models", "apiPercentUsed"),
        )
        parts = [f"{remaining:g}% remaining"]
        meters = []
        for label, key in pools:
            used = plan_usage.get(key)
            if used is None:
                continue
            pool_remaining = float(round(max(0.0, 100.0 - float(used))))
            parts.append(f"{label} {pool_remaining:g}%")
            window: dict[str, object] = {
                "kind": "monthly", "percent": pool_remaining}
            if reset_at:
                window["resetAt"] = reset_at.isoformat()
            meters.append({"label": label, "windows": [window]})
        detail = "Cursor usage: " + ", ".join(parts)

        extra = {"planPercent": remaining}
        if plan_name:
            extra["planName"] = plan_name
        for label, key in pools:
            used = plan_usage.get(key)
            if used is not None:
                extra[f"{label.lower().replace(' ', '')}PercentUsed"] = float(used)
        if meters:
            extra["meters"] = meters
        if cycle_start:
            extra["billingCycleStart"] = cycle_start.isoformat()
        if reset_at:
            extra["billingCycleEnd"] = reset_at.isoformat()
        # Surface the billing cycle in the existing weekly slot.
        extra["weeklyPercent"] = remaining
        if reset_at:
            extra["weeklyResetAt"] = reset_at.isoformat()

        # On-demand spend (a spend view, not quota): spendLimitUsage cents.
        spend = data.get("spendLimitUsage") or {}
        on_demand = _num(spend.get("totalSpend"))
        if on_demand is not None:
            extra["onDemandSpendUsd"] = round(on_demand / 100, 2)
            detail += f" · on-demand ${on_demand / 100:.2f}"
        hard_limit = self._cursor_rpc("GetHardLimit", token)
        if hard_limit is not None:
            extra["onDemandDisabled"] = bool(hard_limit.get("noUsageBasedAllowed"))
            if extra["onDemandDisabled"]:
                detail += " (disabled)"

        return QuotaReport(
            state=QuotaState.OK if remaining > 0 else QuotaState.DEPLETED,
            detail=detail,
            observed_at=datetime.now(timezone.utc),
            remaining_percent=remaining,
            reset_at=reset_at,
            extra=extra,
        )

    def probe_quota(self, force: bool = False) -> QuotaReport:
        """Prefer the real vendor usage read (logged); fall back to the base probe."""
        usage_report = self._usage_quota()
        if usage_report:
            log_quota_observation(
                self.name,
                usage_report.state.value,
                usage_report.detail,
                usage_report.reset_at.isoformat() if usage_report.reset_at else None,
                remaining_percent=usage_report.remaining_percent,
            )
            return usage_report
        return super().probe_quota(force=force)


class GeminiAdapter(_WhichAdapter):
    """Gemini — Antigravity quota API (read-only OAuth) or gemini CLI probe."""

    label = "gemini"
    candidates = ("gemini",)

    def __init__(self) -> None:
        super().__init__()
        self._sentinel_patterns = (
            r"quota",
            r"limit",
            r"429",
            r"rate.?limit",
            r"usage limit",
            r"too many requests",
            r"request limit",
            r"daily quota",
            r"monthly quota",
        )

    def _build_command(self, req: RunRequest) -> list[str]:
        """Gemini CLI uses `-p` for non-interactive mode."""
        assert self._cli_path is not None
        return [self._cli_path, "-p", req.prompt]

    @staticmethod
    def _post_json(url: str, token: str | None, body: dict,
                   extra_headers: dict[str, str] | None = None) -> dict | None:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        headers.update(extra_headers or {})
        request = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, OSError,
                json.JSONDecodeError, TimeoutError):
            return None

    @staticmethod
    def _get_json(url: str, token: str) -> dict | None:
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, OSError,
                json.JSONDecodeError, TimeoutError):
            return None

    @staticmethod
    def _jwt_claim(token: str, claim: str) -> str | None:
        try:
            payload = token.split(".")[1]
            padded = payload + "=" * (-len(payload) % 4)
            value = json.loads(base64.urlsafe_b64decode(padded)).get(claim)
            return str(value) if value else None
        except (IndexError, ValueError, TypeError):
            return None

    @staticmethod
    def _antigravity_ide_state() -> tuple[str | None, str | None, str | None]:
        """Read what the Antigravity IDE persists in its global state DB.

        `User/globalStorage/state.vscdb` holds `antigravityAuthStatus` (the
        current access token as `apiKey` + account email) and
        `antigravityUnifiedStateSync.userStatus` (a protobuf carrying the plan
        label, e.g. "Google AI Pro"). The stored access token is often stale —
        the IDE refreshes it on demand — so the token is a last-resort source,
        but the email/plan label are always valid for enrichment. Read-only
        sqlite. Returns (token, email, plan).
        """
        candidates = [
            Path.home() / "Library" / "Application Support" / "Antigravity"
                / "User" / "globalStorage" / "state.vscdb",  # macOS
            Path.home() / ".config" / "Antigravity" / "User"
                / "globalStorage" / "state.vscdb",           # Linux
        ]
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "Antigravity" / "User"
                                / "globalStorage" / "state.vscdb")  # Windows
        db_path = next((p for p in candidates if p.is_file()), None)
        if db_path is None:
            return None, None, None
        try:
            db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                rows = dict(db.execute(
                    "SELECT key, value FROM ItemTable WHERE key IN "
                    "('antigravityAuthStatus', 'antigravityUnifiedStateSync.userStatus')"))
            finally:
                db.close()
        except sqlite3.Error:
            return None, None, None

        token = email = plan = None
        auth = rows.get("antigravityAuthStatus")
        if auth:
            try:
                data = json.loads(auth)
                if data.get("apiKey"):
                    token = str(data["apiKey"])
                if data.get("email"):
                    email = str(data["email"])
            except json.JSONDecodeError:
                pass
        status = rows.get("antigravityUnifiedStateSync.userStatus")
        if status:
            try:
                raw = base64.b64decode(status)
                for blob in re.findall(rb"[\x21-\x7e]{100,}", raw):
                    inner = base64.b64decode(blob)
                    match = re.search(rb"Google AI (?:Pro|Ultra)", inner)
                    if match:
                        plan = match.group(0).decode()
                        break
                    match = re.search(rb"g\d-\w+-tier", inner)
                    if match:
                        plan = match.group(0).decode()
            except (ValueError, UnicodeDecodeError):
                pass
        return token, email, plan

    def _antigravity_credentials(self) -> tuple[str | None, str | None, str | None]:
        """(token, account email, plan) — jetski file → IDE state DB → keyring.

        The token lives ~1 hour; opening Antigravity (IDE or CLI) is the
        sanctioned refresh path. A stale token just means a 401 and we
        degrade to sentinels.
        """
        ide_token, ide_email, ide_plan = self._antigravity_ide_state()
        for token_path in (
            Path.home() / ".gemini" / "jetski-standalone-oauth-token",
            Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token",
        ):
            try:
                data = json.loads(token_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            token = (data.get("token") or {}).get("access_token") or data.get("access_token")
            if not token:
                continue
            expiry = (data.get("token") or {}).get("expiry") or data.get("expiry")
            if expiry:
                parsed = _parse_iso_datetime(expiry)
                if parsed and parsed <= datetime.now(timezone.utc):
                    continue  # expired; read-only policy — degrade
            return token, ide_email, ide_plan

        if ide_token:
            return ide_token, ide_email, ide_plan

        # OS keyring: service `gemini`, account `antigravity` (all three
        # platforms via core.credentials; Windows uses Credential Manager).
        raw = credentials.get("gemini", "antigravity")
        if raw is None and sys.platform.startswith("linux"):
            # Older entries may be stored under the `user` attribute.
            try:
                result = subprocess.run(
                    ["secret-tool", "lookup", "service", "gemini", "user", "antigravity"],
                    capture_output=True, text=True, timeout=5)
                if result.returncode == 0 and result.stdout.strip():
                    raw = result.stdout.strip()
            except (subprocess.TimeoutExpired, OSError):
                pass
        if raw:
            raw = raw.strip()
            # go-keyring entries are base64-JSON with a `go-keyring` prefix.
            if raw.startswith("go-keyring"):
                blob = raw.split(":", 1)[1] if ":" in raw else raw[len("go-keyring"):]
                try:
                    raw = base64.b64decode(blob).decode("utf-8")
                except (ValueError, UnicodeDecodeError):
                    pass
            try:
                data = json.loads(raw)
                token = data.get("token", {}).get("access_token") or data.get("access_token") or raw
                return str(token), data.get("email") or ide_email, ide_plan
            except json.JSONDecodeError:
                return raw, ide_email, ide_plan
        return None, ide_email, ide_plan

    def _gemini_cli_auth_type(self) -> str | None:
        """Auth type selected in ~/.gemini/settings.json (None = file absent)."""
        settings_path = Path.home() / ".gemini" / "settings.json"
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        auth = settings.get("security", {}).get("auth", {})
        return auth.get("selectedType") or settings.get("authType") or settings.get("selectedAuthType")

    def _antigravity_quota(self) -> QuotaReport | None:
        """Antigravity surface (§4b): retrieveUserQuotaSummary.

        The IDE talks to the daily sandbox host — the prod host can return
        stale/divergent windows (observed: prod 100% vs daily 1%), so daily
        is preferred.
        """
        token, account, plan = self._antigravity_credentials()
        if not token:
            return None
        headers = {"User-Agent": "antigravity"}
        data = None
        host = None
        for host in ("daily-cloudcode-pa.sandbox.googleapis.com",
                     "cloudcode-pa.googleapis.com"):
            data = self._post_json(
                f"https://{host}/v1internal:retrieveUserQuotaSummary",
                token, {}, headers)
            if data is not None:
                break
        if data is None:
            # 401 = stale token; 403 #3501 = licensing wall, not auth failure.
            return None

        # remainingFraction is 0-1 where 1 = full; proto3 omits full buckets,
        # and a bucket missing the field entirely is full too.
        windows: list[tuple[str, str, float, datetime | None]] = []
        for group in data.get("groups") or []:
            group_name = group.get("displayName") or "models"
            for bucket in group.get("buckets") or []:
                fraction = bucket.get("remainingFraction", 1.0)
                remaining = float(round(max(0.0, min(100.0, float(fraction) * 100.0))))
                window = bucket.get("window") or bucket.get("bucketId") or "?"
                windows.append((
                    f"{group_name} {window}",
                    bucket.get("bucketId") or window,
                    remaining,
                    _parse_iso_datetime(bucket.get("resetTime")),
                ))
        if not windows:
            return None

        detail = "antigravity" + (f" ({account})" if account else "")
        detail += (f" [{plan}]" if plan else "") + ": "
        detail += ", ".join(f"{name} {remaining:g}%" for name, _, remaining, _ in windows)
        extra: dict[str, object] = {}
        if account:
            extra["account"] = account
        if plan:
            extra["planName"] = plan
        for _, bucket_id, remaining, reset in windows:
            extra[f"{bucket_id}Percent"] = remaining
            if reset:
                extra[f"{bucket_id}ResetAt"] = reset.isoformat()
        # Surface the Gemini pool in the existing daily/weekly UI slots.
        by_bucket = {bucket_id: (remaining, reset) for _, bucket_id, remaining, reset in windows}
        for bucket_id, prefix in (("gemini-5h", "daily"), ("gemini-weekly", "weekly")):
            entry = by_bucket.get(bucket_id)
            if entry is not None:
                remaining, reset = entry
                extra[f"{prefix}Percent"] = remaining
                if reset:
                    extra[f"{prefix}ResetAt"] = reset.isoformat()

        meters = _meters_from_windows(
            [(name, remaining, reset) for name, _, remaining, reset in windows],
            "models")

        # Per-model buckets from retrieveUserQuota (same host) as extras.
        if host:
            per_model = self._post_json(
                f"https://{host}/v1internal:retrieveUserQuota",
                token, {}, headers)
            for bucket in (per_model or {}).get("buckets") or []:
                if not isinstance(bucket, dict):
                    continue
                model = bucket.get("modelId")
                frac = bucket.get("remainingFraction")
                if model and isinstance(frac, (int, float)):
                    percent = float(round(float(frac) * 100.0))
                    # Flat extra only — model buckets share the group pools,
                    # so per-model meters would render 20+ identical bars.
                    extra[f"model-{model}Percent"] = percent
        extra["meters"] = meters

        # Bar/state reflect the Gemini pool only — the third-party
        # Claude/GPT bucket is a separate meter, not this adapter's quota.
        gemini_windows = [w for w in windows
                          if not w[1].startswith("3p")] or windows
        gemini_min = float(round(min(r for _, _, r, _ in gemini_windows)))
        return QuotaReport(
            state=QuotaState.OK if gemini_min > 0 else QuotaState.DEPLETED,
            detail=detail,
            observed_at=datetime.now(timezone.utc),
            remaining_percent=gemini_min,
            reset_at=min((r for _, _, _, r in gemini_windows if r), default=None),
            extra=extra,
        )

    def _gemini_cli_quota(self) -> QuotaReport | None:
        """Gemini CLI / Code Assist surface (§4a/4c): retrieveUserQuota."""
        auth_type = self._gemini_cli_auth_type()
        if auth_type and auth_type != "oauth-personal":
            return None  # api-key → §4d, vertex-ai → §4e

        creds_path = Path.home() / ".gemini" / "oauth_creds.json"
        try:
            creds = json.loads(creds_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        token = creds.get("access_token")
        expiry = creds.get("expiry_date")
        if not token or (expiry and float(expiry) / 1000 <= time.time()):
            return None  # expired; read-only policy means degrade, not refresh
        account = self._jwt_claim(creds.get("id_token") or "", "email")

        cloudcode = "https://cloudcode-pa.googleapis.com/v1internal"
        assist = self._post_json(
            f"{cloudcode}:loadCodeAssist", token,
            {"metadata": {"ideType": "GEMINI_CLI", "pluginType": "GEMINI"}})
        if assist is None:
            return None  # includes the consumer-tier 403 product wall

        # Product-wall signal: the account has no eligible tier — e.g. the
        # 2026 deprecation moved Code Assist for individuals into the
        # Antigravity suite, and standard-tier requires a licensed project.
        eligible = [t for t in assist.get("allowedTiers") or []
                    if isinstance(t, dict) and t.get("id")]
        walled = any(t.get("reasonCode") == "UNSUPPORTED_CLIENT"
                     for t in assist.get("ineligibleTiers") or []
                     if isinstance(t, dict))
        if not assist.get("currentTier") and not assist.get("cloudaicompanionProject"):
            if walled or all(t.get("userDefinedCloudaicompanionProject") for t in eligible):
                return QuotaReport(
                    state=QuotaState.UNKNOWN,
                    detail=("gemini-cli: account has no eligible Code Assist tier "
                            "(consumer tier discontinued — use the Antigravity "
                            "suite or a licensed GCP project)"),
                    observed_at=datetime.now(timezone.utc),
                )

        project = assist.get("cloudaicompanionProject")
        if not project:
            projects = self._get_json(
                "https://cloudresourcemanager.googleapis.com/v1/projects", token) or {}
            project = next(
                (p.get("projectId") for p in projects.get("projects", [])
                 if str(p.get("projectId", "")).startswith("gen-lang-client")),
                None)
        if not project:
            return None

        quota = self._post_json(
            f"{cloudcode}:retrieveUserQuota", token, {"project": project})
        if quota is None:
            return None

        buckets = quota.get("buckets") or quota.get("quotaBuckets") or []
        windows: list[tuple[str, float, datetime | None]] = []
        for bucket in buckets:
            fraction = bucket.get("remainingFraction", 1.0)
            remaining = float(round(max(0.0, min(100.0, float(fraction) * 100.0))))
            label = bucket.get("modelId") or bucket.get("bucketId") or "model"
            windows.append((label, remaining, _parse_iso_datetime(bucket.get("resetTime"))))
        if not windows:
            return None

        paid_tier = (assist.get("paidTier") or {}).get("name")
        tier_id = (assist.get("currentTier") or {}).get("id") or (assist.get("tier") or {}).get("id")
        tier_names = {
            "standard-tier": "Paid",
            "free-tier": "Workspace" if self._jwt_claim(creds.get("id_token") or "", "hd") else "Free",
            "legacy-tier": "Legacy",
        }
        tier = paid_tier or (tier_names.get(tier_id) if tier_id else None)

        label = "gemini-cli"
        if account:
            label += f" ({account})"
        if tier:
            label += f" [{tier}]"
        detail = label + ": " + ", ".join(f"{name} {remaining:g}%" for name, remaining, _ in windows)

        extra: dict[str, object] = {"cliProject": project}
        if account:
            extra["account"] = account
        if tier:
            extra["tier"] = tier
        for name, remaining, reset in windows:
            key = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")
            extra[f"cli-{key}Percent"] = remaining
            if reset:
                extra[f"cli-{key}ResetAt"] = reset.isoformat()
        extra["meters"] = _meters_from_windows(windows, "Gemini")

        return QuotaReport(
            state=QuotaState.OK,
            detail=detail,
            observed_at=datetime.now(timezone.utc),
            remaining_percent=min(r for _, r, _ in windows),
            reset_at=min((r for _, _, r in windows if r), default=None),
            extra=extra,
        )

    def _api_key_probe(self) -> QuotaReport | None:
        """Gemini API key surface (§4d): free liveness check via countTokens.

        There is no endpoint for live RPM/RPD remaining; consumption tracking
        belongs in the ledger. This only verifies the key is valid.
        """
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key and self._gemini_cli_auth_type() not in ("gemini-api-key", "api-key"):
            return None
        if not api_key:
            return QuotaReport(
                state=QuotaState.UNKNOWN,
                detail="api-key auth selected but no GEMINI_API_KEY/GOOGLE_API_KEY in env",
                observed_at=datetime.now(timezone.utc),
            )
        model = "gemini-2.0-flash"
        data = self._post_json(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:countTokens?key={api_key}",
            None, {"contents": [{"parts": [{"text": "ping"}]}]})
        if data is None:
            return QuotaReport(
                state=QuotaState.UNKNOWN,
                detail="api-key: key rejected or endpoint unreachable",
                observed_at=datetime.now(timezone.utc),
            )
        return QuotaReport(
            state=QuotaState.OK,
            detail="api-key: valid (rate limits tracked via local ledger)",
            observed_at=datetime.now(timezone.utc),
        )

    def _vertex_quota(self) -> QuotaReport | None:
        """Vertex AI surface (§4e): Cloud Quotas reports limits, not consumption."""
        adc_path = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
        if not adc_path.exists():
            return None
        try:
            adc = json.loads(adc_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            adc = {}
        project = os.environ.get("GOOGLE_CLOUD_PROJECT") or adc.get("quota_project_id")
        if not project or not shutil.which("gcloud"):
            return None
        try:
            result = subprocess.run(
                ["gcloud", "auth", "print-access-token"],
                capture_output=True, text=True, timeout=10)
        except (subprocess.TimeoutExpired, OSError):
            return None
        token = result.stdout.strip() if result.returncode == 0 else None
        if not token:
            return None

        # quotaInfos needs the project *number*; resolve it if we only have an id.
        project_number = project if str(project).isdigit() else None
        if not project_number:
            info = self._get_json(
                f"https://cloudresourcemanager.googleapis.com/v1/projects/{project}", token)
            project_number = str(info.get("projectNumber", "")) if info else None
        if not project_number:
            return None

        quotas = self._get_json(
            "https://cloudquotas.googleapis.com/v1/projects/"
            f"{project_number}/locations/global/services/aiplatform.googleapis.com/quotaInfos",
            token)
        if quotas is None:
            return None
        interesting = (
            "GenerateContentRequestsPerMinutePerBaseModel",
            "OnlinePredictionRequestsPerMinutePerProjectPerRegion",
            "GlobalOnlinePredictionTokensPerMinutePerBaseModel",
        )
        parts = []
        for entry in quotas.get("quotaInfos") or []:
            if entry.get("quotaId") not in interesting:
                continue
            for dim in entry.get("dimensionsInfo") or []:
                value = (dim.get("details") or {}).get("quotaValue")
                if value is not None:
                    dims = dim.get("dimensions") or {}
                    scope = dims.get("base_model") or dims.get("region") or "global"
                    parts.append(f"{entry['quotaId'].split('Per')[0]}[{scope}]={value}")
        detail = f"vertex ({project_number}): " + (
            "; ".join(parts) if parts else "limits fetched but none matched known quotaIds")
        return QuotaReport(
            state=QuotaState.OK,
            detail=detail,
            observed_at=datetime.now(timezone.utc),
            remaining_percent=None,
            extra={"vertexProject": project_number, "vertexLimits": parts},
        )

    def _usage_quota(self) -> QuotaReport | None:
        """Probe every Gemini surface whose credentials exist; merge results."""
        surfaces = [
            self._antigravity_quota(),
            self._gemini_cli_quota(),
            self._api_key_probe(),
            self._vertex_quota(),
        ]
        reports = [r for r in surfaces if r is not None]
        if not reports:
            return None

        percents = [r.remaining_percent for r in reports if r.remaining_percent is not None]
        resets = [r.reset_at for r in reports if r.reset_at is not None]
        extra: dict[str, object] = {}
        for r in reports:
            extra.update(r.extra or {})

        # Detail shows only surfaces with real quota data; UNKNOWN notices
        # (license walls, expired creds) appear only when nothing else did.
        data_reports = [r for r in reports if r.remaining_percent is not None]
        display = data_reports or reports
        parts = []
        for r in display:
            source, _, body = r.detail.partition(":")
            source = source.split(" (")[0].split(" [")[0].strip()
            parts.append(f"Gemini ({source}): {body.strip()}")
        detail = " | ".join(parts)

        bar_percent = min(percents) if percents else None
        state = QuotaState.DEPLETED if bar_percent == 0 else (
            QuotaState.OK if data_reports else QuotaState.UNKNOWN)

        return QuotaReport(
            state=state,
            detail=detail,
            observed_at=datetime.now(timezone.utc),
            remaining_percent=bar_percent,
            reset_at=min(resets) if resets else None,
            extra=extra,
        )

    def _credential_state(self) -> str:
        """missing | expired | present — drives the probe failure message."""
        creds_path = Path.home() / ".gemini" / "oauth_creds.json"
        try:
            creds = json.loads(creds_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            creds = None
        if isinstance(creds, dict) and creds.get("access_token"):
            expiry = creds.get("expiry_date")
            if expiry and float(expiry) / 1000 <= time.time():
                return "expired"
            return "present"
        for token_path in (
            Path.home() / ".gemini" / "jetski-standalone-oauth-token",
            Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token",
        ):
            try:
                data = json.loads(token_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            inner = data.get("token") or data
            if not isinstance(inner, dict) or not inner.get("access_token"):
                continue
            expiry = inner.get("expiry") or inner.get("expiry_date")
            parsed = _parse_iso_datetime(expiry)
            if isinstance(expiry, (int, float)) and expiry / 1000 <= time.time():
                return "expired"
            if parsed and parsed <= datetime.now(timezone.utc):
                return "expired"
            return "present"
        if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
            return "present"
        return "missing"

    def probe_quota(self, force: bool = False) -> QuotaReport:
        """Prefer the real vendor usage read (logged); fall back to the base probe."""
        usage_report = self._usage_quota()
        if usage_report:
            log_quota_observation(
                self.name,
                usage_report.state.value,
                usage_report.detail,
                usage_report.reset_at.isoformat() if usage_report.reset_at else None,
                remaining_percent=usage_report.remaining_percent,
            )
            return usage_report
        state = self._credential_state()
        if state == "expired":
            report = QuotaReport(
                state=QuotaState.UNKNOWN,
                detail="gemini OAuth token expired — open Antigravity once to refresh it",
                observed_at=datetime.now(timezone.utc),
            )
            log_quota_observation(self.name, report.state.value, report.detail)
            return report
        if state == "present":
            return QuotaReport(
                state=QuotaState.UNKNOWN,
                detail="gemini credentials found but quota endpoints unreachable or denied",
                observed_at=datetime.now(timezone.utc),
            )
        return super().probe_quota(force=force)


class DevinAdapter(_WhichAdapter):
    """Devin — desktop app credentials probed via the Connect server."""

    label = "devin"
    candidates = ("devin-desktop",)
    _DEVIN_CONNECT_SERVER = "https://server.codeium.com"

    def __init__(self) -> None:
        super().__init__()
        self._sentinel_patterns = (
            r"quota",
            r"limit",
            r"rate.?limit",
            r"usage limit",
            r"429",
            r"too many requests",
            r"request limit",
            r"monthly limit",
            r"daily limit",
        )

    def _build_command(self, req: RunRequest) -> list[str]:
        """Devin Desktop CLI - use chat subcommand if available, otherwise direct prompt."""
        assert self._cli_path is not None
        return [self._cli_path, "chat", req.prompt]

    def _devin_credentials(self) -> tuple[str | None, str]:
        """Read local Devin credentials (CLI toml or Desktop app SQLite).

        The session token is read in memory only and is never logged or persisted.
        """
        # 1. Devin CLI credentials.
        credentials_path = Path.home() / ".local" / "share" / "devin" / "credentials.toml"
        if credentials_path.exists():
            try:
                data = tomllib.loads(credentials_path.read_text(encoding="utf-8"))
            except (tomllib.TOMLDecodeError, OSError):
                pass
            else:
                api_key = data.get("windsurf_api_key")
                server = data.get("api_server_url") or self._DEVIN_CONNECT_SERVER
                if api_key:
                    return api_key, server

        # 2. Devin Desktop app SQLite (macOS / Linux).
        app_state_paths = [
            Path.home() / "Library" / "Application Support" / "Devin" / "User" / "globalStorage" / "state.vscdb",
            Path.home() / ".config" / "Devin" / "User" / "globalStorage" / "state.vscdb",
        ]
        for state_path in app_state_paths:
            if not state_path.exists():
                continue
            try:
                conn = sqlite3.connect(state_path)
                try:
                    row = conn.execute(
                        "SELECT value FROM ItemTable WHERE key = 'windsurfAuthStatus'"
                    ).fetchone()
                finally:
                    conn.close()
                if row and row[0]:
                    auth_state = json.loads(row[0])
                    api_key = auth_state.get("apiKey")
                    if api_key:
                        return api_key, self._DEVIN_CONNECT_SERVER
            except (OSError, sqlite3.Error, json.JSONDecodeError):
                continue

        return None, self._DEVIN_CONNECT_SERVER

    def _parse_unix_reset(self, value) -> datetime | None:
        try:
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        except (ValueError, TypeError):
            return None

    def _get_user_status_quota(self) -> QuotaReport | None:
        """Call Devin's Connect GetUserStatus endpoint for the quota shown in the app."""
        session_token, server = self._devin_credentials()
        if not session_token:
            return None

        url = f"{server}/exa.seat_management_pb.SeatManagementService/GetUserStatus"
        body = json.dumps({
            "metadata": {
                "apiKey": session_token,
                "ideName": "devin",
                "ideVersion": "1.126.0",
                "extensionName": "devin",
                "extensionVersion": "1.126.0",
                "locale": "en",
            }
        }).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Connect-Protocol-Version": "1",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError, TimeoutError):
            return None

        user_status = data.get("userStatus", {})
        plan_status = user_status.get("planStatus", {})
        plan_info = plan_status.get("planInfo", {})

        # Non-quota plans use legacy credit balances; there are no quota windows.
        billing_strategy = plan_info.get("billingStrategy")
        if billing_strategy and billing_strategy != "BILLING_STRATEGY_QUOTA":
            return None

        # These fields are proto3 implicit scalars: the server omits them when
        # zero. A missing percent with a positive reset timestamp means the
        # window exists and is fully exhausted (0% remaining); when both are
        # missing the plan has no such window at all.
        def resolve_window(pct_key: str, reset_key: str, hidden: bool):
            """Return (percent, reset) for a plan-status window, honoring proto3
            implicit-zero rules; None when the plan has no such window."""
            if hidden:
                return None
            raw_reset = plan_status.get(reset_key)
            reset = self._parse_unix_reset(raw_reset)
            if reset is not None and reset.timestamp() <= 0:
                reset = None
            pct = plan_status.get(pct_key)
            if pct is None:
                if reset is None:
                    return None
                pct = 0
            return float(round(float(pct))), reset

        daily = resolve_window(
            "dailyQuotaRemainingPercent",
            "dailyQuotaResetAtUnix",
            plan_info.get("hideDailyQuota", False),
        )
        weekly = resolve_window(
            "weeklyQuotaRemainingPercent",
            "weeklyQuotaResetAtUnix",
            plan_info.get("hideWeeklyQuota", False),
        )

        if daily is None and weekly is None:
            return None

        overage = plan_status.get("overageBalanceMicros")
        overage_usd = round(int(overage) / 1_000_000, 2) if overage else None

        parts: list[str] = []
        if daily is not None:
            parts.append(f"daily {daily[0]:g}%")
        if weekly is not None:
            parts.append(f"weekly {weekly[0]:g}%")
        detail = f"Devin quota: {', '.join(parts)} remaining"
        if overage_usd is not None:
            detail += f" (${overage_usd:.2f} overage)"

        values = [w[0] for w in (daily, weekly) if w is not None]
        bar_percent = min(values) if values else None

        resets = [w[1] for w in (daily, weekly) if w is not None and w[1] is not None]
        reset_at = min(resets) if resets else None

        extra: dict[str, object] = {
            "planName": plan_info.get("planName") or plan_status.get("planName"),
            "hideDaily": plan_info.get("hideDailyQuota", False),
            "hideWeekly": plan_info.get("hideWeeklyQuota", False),
        }
        if daily is not None:
            extra["dailyPercent"] = daily[0]
            if daily[1]:
                extra["dailyResetAt"] = daily[1].isoformat()
        if weekly is not None:
            extra["weeklyPercent"] = weekly[0]
            if weekly[1]:
                extra["weeklyResetAt"] = weekly[1].isoformat()
        if overage_usd is not None:
            extra["overageUsd"] = overage_usd
        meter_windows = _meters_from_windows(
            [(name, w[0], w[1]) for name, w in (("daily", daily), ("weekly", weekly))
             if w is not None], "Devin")
        if meter_windows:
            extra["meters"] = meter_windows

        return QuotaReport(
            state=QuotaState.OK if (bar_percent is None or bar_percent > 0) else QuotaState.DEPLETED,
            detail=detail,
            observed_at=datetime.now(timezone.utc),
            remaining_percent=bar_percent,
            reset_at=reset_at,
            extra=extra,
        )

    def probe_quota(self, force: bool = False) -> QuotaReport:
        """Prefer the Connect user-status quota read; fall back to base."""
        status_report = self._get_user_status_quota()
        if status_report:
            log_quota_observation(
                self.name,
                status_report.state.value,
                status_report.detail,
                remaining_percent=status_report.remaining_percent,
                reset_at=status_report.reset_at.isoformat() if status_report.reset_at else None,
            )
            return status_report
        return super().probe_quota(force=force)


class CodexAdapter(_WhichAdapter):
    """Codex — ChatGPT wham usage endpoint plus app-server JSON-RPC fallback."""

    label = "codex"
    candidates = ("codex",)

    def __init__(self) -> None:
        super().__init__()
        self._sentinel_patterns = (
            r"quota",
            r"limit",
            r"rate.?limit",
            r"usage limit",
            r"429",
            r"too many requests",
            r"request limit",
            r"monthly limit",
            r"daily limit",
            r"credit limit",
        )

    def _build_command(self, req: RunRequest) -> list[str]:
        """Codex CLI uses 'exec' subcommand for non-interactive execution."""
        assert self._cli_path is not None
        return [self._cli_path, "exec", req.prompt]

    @staticmethod
    def _jwt_claim(token: str, claim: str) -> str | None:
        try:
            payload = token.split(".")[1]
            padded = payload + "=" * (-len(payload) % 4)
            value = json.loads(base64.urlsafe_b64decode(padded)).get(claim)
            return str(value) if value is not None else None
        except (IndexError, ValueError, TypeError):
            return None

    def _codex_auth(self) -> tuple[str, str | None] | None:
        """Read $CODEX_HOME/auth.json or ~/.codex/auth.json (read-only)."""
        codex_home = os.environ.get("CODEX_HOME")
        candidates = []
        if codex_home:
            candidates.append(Path(codex_home) / "auth.json")
        candidates.append(Path.home() / ".codex" / "auth.json")
        for auth_path in candidates:
            if not auth_path.exists():
                continue
            try:
                data = json.loads(auth_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            tokens = data.get("tokens") or {}
            token = tokens.get("access_token")
            if not token:
                continue
            account_id = tokens.get("account_id") or self._jwt_claim(
                token, "https://api.openai.com/auth.chatgpt_account_id")
            return token, account_id
        return None

    def _codex_base_url(self) -> str:
        """chatgpt_base_url override from ~/.codex/config.toml, if set."""
        codex_home = os.environ.get("CODEX_HOME")
        config_path = (Path(codex_home) if codex_home else Path.home() / ".codex") / "config.toml"
        try:
            config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return ""
        return str(config.get("chatgpt_base_url") or "").rstrip("/")

    def _usage_quota(self) -> QuotaReport | None:
        """Call ChatGPT's wham usage endpoint for real rate-limit windows.

        Read-only: never refresh tokens. A 401/403 means the CLI owns the
        session — degrade to sentinels and let `codex` refresh on next use.
        """
        auth = self._codex_auth()
        if not auth:
            return None
        token, account_id = auth

        base = self._codex_base_url()
        url = (f"{base}/api/codex/usage" if base
               else "https://chatgpt.com/backend-api/wham/usage")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if account_id:
            headers["chatgpt-account-id"] = account_id
        request = urllib.request.Request(url, headers=headers)
        data = None
        reset_credits = None
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = json.loads(response.read().decode("utf-8"))
            # Best-effort reset-credit inventory (read-only; never redeem).
            credits_url = (f"{base}/api/codex/rate-limit-reset-credits" if base
                           else "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits")
            credits_req = urllib.request.Request(credits_url, headers=headers)
            with contextlib.suppress(urllib.error.HTTPError, urllib.error.URLError,
                                     OSError, json.JSONDecodeError, TimeoutError):
                with urllib.request.urlopen(credits_req, timeout=15) as credits_resp:
                    reset_credits = json.loads(credits_resp.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, OSError,
                json.JSONDecodeError, TimeoutError):
            pass

        source = None
        if not isinstance(data, dict):
            # No-network fallback: local JSON-RPC `account/rateLimits/read`.
            rpc = self._app_server_rate_limits()
            if rpc is not None:
                data = self._snakeize(rpc)
                if isinstance(data, dict):
                    # The RPC nests limits under `rateLimits`; flatten so the
                    # wham-shaped parser below sees `rate_limit` + `plan_type`.
                    for nest_key in ("rate_limit", "rate_limits", "result"):
                        nested = data.get(nest_key)
                        if isinstance(nested, dict):
                            merged = dict(nested)
                            merged.update({k: v for k, v in data.items()
                                           if k != nest_key})
                            data = merged
                            break
                    if not isinstance(data.get("rate_limit"), dict) and (
                            isinstance(data.get("primary_window"), dict)
                            or isinstance(data.get("secondary_window"), dict)):
                        data = {**data, "rate_limit": dict(data)}
                    source = "app-server"
        if not isinstance(data, dict):
            return None

        def window(name: str, w: dict) -> tuple[str, str, float, datetime | None]:
            """Normalize one wham window dict to (name, name, remaining%, reset)."""
            used = float(w.get("used_percent") or 0.0)
            remaining = float(round(max(0.0, 100.0 - used)))
            reset = w.get("reset_at")
            reset_dt = None
            if reset:
                with contextlib.suppress(ValueError, TypeError):
                    reset_dt = datetime.fromtimestamp(int(reset), tz=timezone.utc)
            return name, name, remaining, reset_dt

        windows: list[tuple[str, str, float, datetime | None]] = []
        rate_limit = data.get("rate_limit")
        if not isinstance(rate_limit, dict):
            rate_limit = {}
        for key, name in (("primary_window", "5h"), ("secondary_window", "weekly")):
            w = rate_limit.get(key)
            if isinstance(w, dict):
                windows.append(window(name, w))
        for entry in data.get("additional_rate_limits") or []:
            if not isinstance(entry, dict):
                continue
            label = entry.get("id") or entry.get("name") or "extra"
            if "used_percent" in entry:
                windows.append(window(label, entry))
            else:
                for sub_key, sub_w in (entry.get("rate_limit") or {}).items():
                    if isinstance(sub_w, dict):
                        windows.append(window(f"{label}-{sub_key.replace('_window', '')}", sub_w))
        if not windows:
            return None

        plan = data.get("plan_type")
        detail = "Codex usage" + (f" [{plan}]" if plan else "")
        if source:
            detail += f" (via {source})"
        detail += ": "
        detail += ", ".join(f"{name} {remaining:g}% remaining" for name, _, remaining, _ in windows)

        extra: dict[str, object] = {}
        if plan:
            extra["planName"] = plan
        key_map = {"5h": "daily", "weekly": "weekly"}
        for name, _, remaining, reset in windows:
            prefix = key_map.get(name, re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-"))
            extra[f"{prefix}Percent"] = remaining
            if reset:
                extra[f"{prefix}ResetAt"] = reset.isoformat()
        extra["meters"] = _meters_from_windows(
            [(name, remaining, reset) for name, _, remaining, reset in windows],
            "Codex")

        if reset_credits is not None:
            extra["resetCredits"] = reset_credits

        bar_percent = min(remaining for _, _, remaining, _ in windows)
        resets = [r for _, _, _, r in windows if r is not None]
        return QuotaReport(
            state=QuotaState.OK if bar_percent > 0 else QuotaState.DEPLETED,
            detail=detail,
            observed_at=datetime.now(timezone.utc),
            remaining_percent=bar_percent,
            reset_at=min(resets) if resets else None,
            extra=extra,
        )

    @staticmethod
    def _snakeize(value):
        """Recursively convert camelCase keys to snake_case (app-server JSON-RPC
        responses use camelCase while wham uses snake_case)."""
        if isinstance(value, dict):
            out = {}
            for key, sub in value.items():
                snake = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", str(key)).lower()
                out[snake] = CodexAdapter._snakeize(sub)
            return out
        if isinstance(value, list):
            return [CodexAdapter._snakeize(item) for item in value]
        return value

    def _app_server_rate_limits(self) -> dict | None:
        """Offline fallback: JSON-RPC `account/rateLimits/read` on the local
        `codex app-server`. No network, consumes zero model quota."""
        if not self._cli_path:
            return None
        try:
            proc = subprocess.Popen(
                [self._cli_path, "-s", "read-only", "-a", "untrusted", "app-server"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True)
        except OSError:
            return None
        try:
            stdin = proc.stdin
            stdout = proc.stdout
            if stdin is None or stdout is None:
                return None
            deadline = time.monotonic() + 15.0

            def send(msg: dict) -> None:
                """Write one JSON-RPC message line to the app-server."""
                stdin.write(json.dumps(msg) + "\n")
                stdin.flush()

            def read_reply(want_id: int):
                """Read lines until the reply for ``want_id`` or the deadline."""
                while time.monotonic() < deadline:
                    ready, _, _ = select.select(
                        [stdout], [], [],
                        max(0.0, deadline - time.monotonic()))
                    if not ready:
                        break
                    line = stdout.readline()
                    if not line:
                        break
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # server may emit non-RPC log lines
                    if msg.get("id") == want_id:
                        return msg.get("result")
                return None

            send({"id": 0, "method": "initialize",
                  "params": {"clientInfo": {"name": "tollkeeper", "version": "0.2.0"}}})
            if read_reply(0) is None:
                return None
            send({"method": "initialized"})
            send({"id": 1, "method": "account/rateLimits/read", "params": {}})
            result = read_reply(1)
            return result if isinstance(result, dict) else None
        except (OSError, BrokenPipeError):
            return None
        finally:
            with contextlib.suppress(Exception):
                proc.kill()

    def probe_quota(self, force: bool = False) -> QuotaReport:
        """Prefer the real vendor usage read (logged); fall back to the base probe."""
        usage_report = self._usage_quota()
        if usage_report:
            log_quota_observation(
                self.name,
                usage_report.state.value,
                usage_report.detail,
                usage_report.reset_at.isoformat() if usage_report.reset_at else None,
                remaining_percent=usage_report.remaining_percent,
            )
            return usage_report
        return super().probe_quota(force=force)


class PerplexityAdapter(_WhichAdapter):
    """Perplexity — pasted session cookie; per-product limit counts."""

    label = "perplexity"
    candidates = ("perplexity", "pplx")

    def __init__(self) -> None:
        super().__init__()
        self._sentinel_patterns = (
            r"quota",
            r"limit",
            r"rate.?limit",
            r"usage limit",
            r"429",
            r"too many requests",
            r"request limit",
            r"monthly limit",
            r"daily limit",
            r"credit limit",
        )

    def _build_command(self, req: RunRequest) -> list[str]:
        """Perplexity CLI command (placeholder - CLI doesn't exist yet)."""
        assert self._cli_path is not None
        return [self._cli_path, req.prompt]

    def _session_cookie(self) -> str | None:
        """Build the Perplexity Cookie header from stored credentials.

        `session_cookie` accepts a bare `__Secure-next-auth.session-token`
        value, a `name=value` pair, or a whole copied Cookie header.
        `pplx_session` is optional and must be a `name=value` pair (the
        cookie name embeds a per-user UUID). Sources: env
        (`PERPLEXITY_SESSION_COOKIE` / `PERPLEXITY_PPLX_SESSION`), OS
        keychain via `router cred set`, then config.toml. Browser
        cookie-store scraping is deliberately not implemented.
        """
        env_cookie = os.environ.get("PERPLEXITY_SESSION_COOKIE")
        cookie = (env_cookie
                  or credentials.get("perplexity", "session_cookie")
                  or _router_config_value("perplexity", "session_cookie"))
        if not cookie:
            return None
        # Bare token values wrap in the NextAuth cookie name; anything with
        # '=' is already a complete name=value pair or full Cookie header.
        if "=" not in cookie.split(";")[0]:
            cookie = f"__Secure-next-auth.session-token={cookie}"
        # Optional second session cookie: its name embeds a per-user UUID
        # (__Secure-pplx.session.<uuid>), so it must be stored as name=value.
        pplx = (os.environ.get("PERPLEXITY_PPLX_SESSION")
                or credentials.get("perplexity", "pplx_session")
                or _router_config_value("perplexity", "pplx_session"))
        if pplx:
            cookie = f"{cookie}; {pplx}"
        return cookie

    @staticmethod
    def _get_json(url: str, cookie: str):
        request = urllib.request.Request(
            url, headers=_browser_headers(cookie, referer="https://www.perplexity.ai/"))
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, OSError,
                json.JSONDecodeError, TimeoutError):
            return None

    def _api_credits(self, cookie: str) -> dict[str, object]:
        """Best-effort prepaid API-credits view from console.perplexity.ai RPCs.

        Cookie-authed; the API key itself can't read this. Needs an org-id
        discovery step. This is a spend/balance view, not quota — attached
        under `extra["apiCredits"]` only."""
        base = "https://www.perplexity.ai/rest/pplx-api/v2"
        extra: dict[str, object] = {}
        groups = self._get_json(f"{base}/groups", cookie)
        items = groups if isinstance(groups, list) else (
            (groups or {}).get("groups") or (groups or {}).get("data") or []
            if isinstance(groups, dict) else [])
        if not items:
            return extra
        org = items[0] if isinstance(items[0], dict) else {}
        org_id = org.get("id") or org.get("org_id") or org.get("group_id")
        if not org_id:
            return extra

        credits: dict[str, object] = {"org": str(org_id)}
        info = self._get_json(f"{base}/groups/{org_id}", cookie)
        if isinstance(info, dict):
            tier = info.get("tier") or info.get("api_tier") or info.get("subscription_tier")
            if tier:
                credits["tier"] = str(tier)
            for key in ("balance", "available_balance", "credit_balance"):
                value = info.get(key) or (info.get("billing") or {}).get(key)
                if isinstance(value, (int, float)):
                    credits["balanceUsd"] = float(value)
                    break
        usage = self._get_json(f"{base}/groups/{org_id}/usage", cookie)
        if isinstance(usage, dict):
            for key in ("total", "spend", "total_spend", "amount"):
                value = usage.get(key)
                if isinstance(value, (int, float)):
                    credits["spendUsd"] = float(value)
                    break
            else:
                usage_items = usage.get("items") or usage.get("usage")
                if isinstance(usage_items, list):
                    amounts = [
                        float(i.get("amount", i.get("total", 0)) or 0)
                        for i in usage_items if isinstance(i, dict)
                    ]
                    if amounts:
                        credits["spendUsd"] = sum(amounts)
        if len(credits) > 1:
            extra["apiCredits"] = credits
        return extra

    def _usage_quota(self) -> QuotaReport | None:
        """Call Perplexity's web-session rate-limit endpoint.

        Remaining-only API: reports exact integers, no denominators, no reset
        timestamps. Never synthesize a percentage from a missing denominator.
        """
        cookie = self._session_cookie()
        if not cookie:
            return None

        request = urllib.request.Request(
            "https://www.perplexity.ai/rest/rate-limit/all",
            headers=_browser_headers(cookie, referer="https://www.perplexity.ai/"),
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                body = e.read()[:2000].decode("utf-8", errors="replace")
                challenge = "Just a moment" in body or "challenge-platform" in body
                return QuotaReport(
                    state=QuotaState.UNKNOWN,
                    detail=("perplexity blocked by Cloudflare bot-check — "
                            "session cookie may be fine; retry later" if challenge
                            else "perplexity session expired — paste a fresh cookie "
                                 "(see PROVIDER_SETUP.md)"),
                    observed_at=datetime.now(timezone.utc),
                )
            return None
        except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
            return None

        parts: list[str] = []
        extra: dict[str, object] = {}
        counts: list[int] = []

        free = data.get("free_queries")
        if isinstance(free, dict):
            detail_info = free.get("remaining_detail") or {}
            if free.get("available") and detail_info.get("kind") == "exact":
                remaining = detail_info.get("remaining")
                if remaining is not None:
                    counts.append(int(remaining))
                    parts.append(f"{int(remaining)} free")
                    extra["freeRemaining"] = int(remaining)

        # Dynamic `remaining_*` scan — Pro accounts surface additional
        # pools (agentic research, labs, model limits) that free accounts
        # never send, so don't rely on a fixed key list.
        known = {"remaining_pro": "Pro", "remaining_research": "Research",
                 "remaining_agentic_research": "Agentic Research",
                 "remaining_labs": "Labs"}
        meters: list[dict] = []
        for key, value in sorted(data.items()):
            if not key.startswith("remaining_") or not isinstance(value, (int, float)):
                continue
            name = known.get(key) or key[len("remaining_"):].replace("_", " ").title()
            counts.append(int(value))
            parts.append(f"{int(value)} {name}")
            extra[f"{name.lower().replace(' ', '')}Remaining"] = int(value)

        for model, value in (data.get("model_specific_limits") or {}).items():
            label = str(model)
            limit = None
            if isinstance(value, dict):
                limit = value.get("limit") or value.get("monthly_limit")
                detail_info = value.get("remaining_detail") or {}
                value = (value.get("remaining")
                         if value.get("remaining") is not None
                         else detail_info.get("remaining"))
            if value is None:
                continue
            counts.append(int(value))
            parts.append(f"{int(value)} {label}")
            key = re.sub(r"[^A-Za-z0-9]+", "-", label).strip("-")
            extra[f"{key}Remaining"] = int(value)
            if isinstance(limit, (int, float)) and limit > 0:
                percent = round(max(0.0, min(100.0, 100.0 * float(value) / float(limit))))
                meters.append({"label": label, "windows": [
                    {"kind": "monthly", "percent": percent}]})

        # Pro connector limits (slack, notion, scholar…) — nonzero only.
        sources = (data.get("sources") or {}).get("source_to_limit") or {}
        tracked = {
            name: {"limit": lim.get("monthly_limit"),
                   "remaining": lim.get("remaining")}
            for name, lim in sources.items()
            if isinstance(lim, dict) and (lim.get("monthly_limit") or 0) > 0
        }
        if tracked:
            extra["sourceLimits"] = tracked

        if meters:
            extra["meters"] = meters
        if not parts:
            return None

        # API-credits side (separate prepaid pool; spend view, best-effort).
        extra.update(self._api_credits(cookie))

        detail = "Perplexity: " + ", ".join(parts) + " left"
        return QuotaReport(
            state=QuotaState.DEPLETED if counts and all(c == 0 for c in counts) else QuotaState.OK,
            detail=detail,
            observed_at=datetime.now(timezone.utc),
            remaining_percent=None,  # no denominator exists; never invent one
            extra=extra,
        )

    def probe_quota(self, force: bool = False) -> QuotaReport:
        """Prefer the real vendor usage read (logged); fall back to the base probe."""
        usage_report = self._usage_quota()
        if usage_report:
            log_quota_observation(
                self.name,
                usage_report.state.value,
                usage_report.detail,
                remaining_percent=usage_report.remaining_percent,
            )
            return usage_report
        if not self._session_cookie():
            # There is no Perplexity CLI — a pasted cookie is the only
            # credential path, so "not installed" would be misleading.
            report = QuotaReport(
                state=QuotaState.UNKNOWN,
                detail=("perplexity not configured — paste a session cookie: "
                        "`router cred set perplexity session_cookie` "
                        "(see PROVIDER_SETUP.md)"),
                observed_at=datetime.now(timezone.utc),
            )
            log_quota_observation(self.name, report.state.value, report.detail)
            return report
        return QuotaReport(
            state=QuotaState.UNKNOWN,
            detail="perplexity session cookie set but rate-limit endpoint unreachable",
            observed_at=datetime.now(timezone.utc),
        )


class CopilotAdapter(_WhichAdapter):
    """GitHub Copilot — quota snapshots via copilot_internal token exchange."""

    label = "copilot"
    candidates = ("copilot", "gh")

    def __init__(self) -> None:
        super().__init__()
        self._sentinel_patterns = (
            r"rate.?limit",
            r"quota",
            r"usage limit",
            r"429",
            r"too many requests",
            r"request limit",
            r"premium.?request",
            r"credit limit",
        )

    def _build_command(self, req: RunRequest) -> list[str]:
        """GitHub Copilot CLI."""
        assert self._cli_path is not None
        return [self._cli_path, "-p", req.prompt]

    def _github_token(self) -> str | None:
        """Read a GitHub token (read-only; never minted or refreshed here)."""
        if shutil.which("gh"):
            try:
                result = subprocess.run(
                    ["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()
            except (subprocess.TimeoutExpired, OSError):
                pass

        env_token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if env_token:
            return env_token

        hosts_path = Path.home() / ".config" / "gh" / "hosts.yml"
        try:
            text = hosts_path.read_text(encoding="utf-8")
        except OSError:
            return None
        match = re.search(r"^\s*oauth_token:\s*(\S+)", text, re.MULTILINE)
        return match.group(1) if match else None

    _GH_HEADERS = {
        "Accept": "application/json",
        "Editor-Version": "vscode/1.96.2",
        "Editor-Plugin-Version": "copilot-chat/0.26.7",
        "User-Agent": "GitHubCopilotChat/0.26.7",
        "X-Github-Api-Version": "2025-04-01",
    }

    def _gh_get(self, url: str, token: str) -> dict | list | None:
        request = urllib.request.Request(
            url, headers={**self._GH_HEADERS, "Authorization": f"token {token}"})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, OSError,
                json.JSONDecodeError, TimeoutError):
            return None

    @staticmethod
    def _parse_windows(data: dict) -> tuple[str | None, list[tuple[str, float, str, datetime | None]]]:
        """Parse quota snapshots into (plan, windows). Strict allowlist: a
        missing/changed field means "no reading" — never a guessed zero."""
        plan = data.get("copilot_plan") or data.get("access_type_sku")
        windows: list[tuple[str, float, str, datetime | None]] = []

        snapshots = data.get("quota_snapshots")
        if isinstance(snapshots, dict):
            reset = _parse_iso_datetime(data.get("quota_reset_date_utc"))
            for name, snap in snapshots.items():
                if not isinstance(snap, dict) or snap.get("unlimited"):
                    continue
                entitlement = snap.get("entitlement")
                remaining = snap.get("remaining")
                percent = snap.get("percent_remaining")
                if entitlement is None or remaining is None or percent is None:
                    continue  # changed shape -> no reading, never a guess
                windows.append((
                    name,
                    float(round(max(0.0, float(percent)))),
                    f"{name} {round(float(percent)):g}% ({int(remaining)}/{int(entitlement)})",
                    reset,
                ))
        elif isinstance(data.get("monthly_quotas"), dict) or isinstance(data.get("limited_user_quotas"), dict):
            # Free tier: monthly_quotas = entitlement, limited_user_quotas = remaining.
            monthly = data.get("monthly_quotas") or {}
            limited = data.get("limited_user_quotas") or {}
            reset = _parse_iso_datetime(data.get("limited_user_reset_date"))
            for name in sorted(set(monthly) | set(limited)):
                entitlement = monthly.get(name)
                remaining = limited.get(name)
                if entitlement is None or remaining is None or not entitlement:
                    continue
                percent = float(round(max(0.0, float(remaining) / float(entitlement) * 100.0)))
                windows.append((
                    name, percent,
                    f"{name} {percent:g}% ({int(remaining)}/{int(entitlement)})",
                    reset,
                ))
        return plan, windows

    @staticmethod
    def _usage_amount_usd(data) -> float | None:
        """Extract a dollar total from a billing usage payload (tolerant)."""
        if not isinstance(data, dict):
            return None
        for key in ("totalAmount", "total_amount", "netAmount"):
            value = data.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        cents = data.get("totalAmountInCents")
        if isinstance(cents, (int, float)):
            return cents / 100.0
        items = data.get("usageItems") or data.get("usage_items")
        if isinstance(items, list) and items:
            total = 0.0
            found = False
            for item in items:
                if not isinstance(item, dict):
                    continue
                for key in ("grossAmount", "netAmount", "amount"):
                    value = item.get(key)
                    if isinstance(value, (int, float)):
                        total += float(value)
                        found = True
                        break
            return total if found else None
        return None

    def _billing_usage(self, token: str) -> dict[str, object]:
        """Best-effort spend view: personal billing endpoints first; if both
        are empty the seat is org-paid, so try the first org's endpoints."""
        extra: dict[str, object] = {}
        user = self._gh_get("https://api.github.com/user", token)
        login = (user or {}).get("login") if isinstance(user, dict) else None
        if not login:
            return extra

        premium = self._usage_amount_usd(self._gh_get(
            f"https://api.github.com/users/{login}/settings/billing/premium_request/usage",
            token))
        credits = self._usage_amount_usd(self._gh_get(
            f"https://api.github.com/users/{login}/settings/billing/ai_credit/usage",
            token))
        if premium is not None:
            extra["premiumRequestUsageUsd"] = premium
        if credits is not None:
            extra["aiCreditUsageUsd"] = credits
        if premium is not None or credits is not None:
            return extra

        # Empty on both -> org-paid seat; query the org billing endpoints.
        orgs = self._gh_get("https://api.github.com/user/orgs", token)
        if not isinstance(orgs, list) or not orgs:
            return extra
        org_login = orgs[0].get("login") if isinstance(orgs[0], dict) else None
        if not org_login:
            return extra
        premium = self._usage_amount_usd(self._gh_get(
            f"https://api.github.com/organizations/{org_login}/settings/billing/premium_request/usage",
            token))
        credits = self._usage_amount_usd(self._gh_get(
            f"https://api.github.com/organizations/{org_login}/settings/billing/ai_credit/usage",
            token))
        if premium is not None:
            extra["premiumRequestUsageUsd"] = premium
        if credits is not None:
            extra["aiCreditUsageUsd"] = credits
        if premium is not None or credits is not None:
            extra["billedTo"] = org_login
        return extra

    def _usage_quota(self) -> QuotaReport | None:
        """Call copilot_internal/user for quota snapshots."""
        token = self._github_token()
        if not token:
            return None

        data = self._gh_get("https://api.github.com/copilot_internal/user", token)
        if not isinstance(data, dict):
            return None
        plan, windows = self._parse_windows(data)

        if not windows:
            # Free-tier variant some tools use; same snapshot shapes apply.
            v2 = self._gh_get("https://api.github.com/copilot_internal/v2/token", token)
            if isinstance(v2, dict):
                plan2, windows = self._parse_windows(v2)
                plan = plan or plan2
        if not windows:
            return None

        detail = "Copilot quota" + (f" [{plan}]" if plan else "") + ": "
        detail += ", ".join(part for _, _, part, _ in windows)
        bar_percent = min(p for _, p, _, _ in windows)
        resets = [r for _, _, _, r in windows if r is not None]

        extra: dict[str, object] = {}
        if plan:
            extra["planName"] = plan
        for name, percent, _, reset in windows:
            key = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")
            extra[f"{key}Percent"] = percent
            if reset:
                extra[f"{key}ResetAt"] = reset.isoformat()
        # Surface the strictest finite quota in the existing weekly UI slot.
        strictest = min(windows, key=lambda w: w[1])
        extra["weeklyPercent"] = strictest[1]
        if strictest[3]:
            extra["weeklyResetAt"] = strictest[3].isoformat()
        meters = []
        for name, pct, _, reset in windows:
            window: dict[str, object] = {"kind": "monthly", "percent": float(round(pct))}
            if reset:
                window["resetAt"] = reset.isoformat()
            meters.append({"label": name.replace("_", " "), "windows": [window]})
        extra["meters"] = meters

        # Billing-side consumption (spend, not remaining). Needs `user` scope;
        # silently skipped when the token lacks it.
        extra.update(self._billing_usage(token))
        billed_parts = []
        if "premiumRequestUsageUsd" in extra:
            billed_parts.append(f"${extra['premiumRequestUsageUsd']:.2f} premium requests")
        if "aiCreditUsageUsd" in extra:
            billed_parts.append(f"${extra['aiCreditUsageUsd']:.2f} AI credits")
        if billed_parts:
            detail += " | billed: " + ", ".join(billed_parts)
            if "billedTo" in extra:
                detail += f" (org {extra['billedTo']})"

        return QuotaReport(
            state=QuotaState.OK if bar_percent > 0 else QuotaState.DEPLETED,
            detail=detail,
            observed_at=datetime.now(timezone.utc),
            remaining_percent=bar_percent,
            reset_at=min(resets) if resets else None,
            extra=extra,
        )

    def probe_quota(self, force: bool = False) -> QuotaReport:
        """Prefer the real vendor usage read (logged); fall back to the base probe."""
        usage_report = self._usage_quota()
        if usage_report:
            log_quota_observation(
                self.name,
                usage_report.state.value,
                usage_report.detail,
                usage_report.reset_at.isoformat() if usage_report.reset_at else None,
                remaining_percent=usage_report.remaining_percent,
            )
            return usage_report
        return super().probe_quota(force=force)


class ZedAdapter(_WhichAdapter):
    """Zed — billing usage via session cookie or editor credential."""

    label = "zed"
    candidates = ("zed",)

    def __init__(self) -> None:
        super().__init__()
        self._sentinel_patterns = (
            r"quota",
            r"rate.?limit",
            r"usage limit",
            r"429",
            r"too many requests",
            r"token spend",
            r"edit predictions",
        )

    def _build_command(self, req: RunRequest) -> list[str]:
        """Zed is an editor, not a headless CLI — runs are unsupported."""
        raise RuntimeError("zed does not support headless prompt runs")

    def run(self, req: RunRequest) -> RunResult:
        """No headless prompt API exists, so run() hands the prompt to the
        desktop editor: write it to a temp file and open it via `zed`.
        The agent panel picks it up from there."""
        if not self.is_available():
            raise RuntimeError("zed is not installed")
        if not self._cli_path:
            raise RuntimeError(
                "zed desktop app found but no `zed` CLI on PATH")
        import tempfile
        fd, tmp_name = tempfile.mkstemp(prefix="zed-prompt-", suffix=".md")
        os.close(fd)
        tmp = Path(tmp_name)
        tmp.write_text(req.prompt, encoding="utf-8")
        try:
            subprocess.Popen(
                [self._cli_path, str(tmp)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=req.workdir)
        except OSError as e:
            raise RuntimeError(f"zed run failed: {e}")
        return RunResult(
            output=(f"prompt opened in Zed ({tmp}); Zed has no headless "
                    "prompt API — continue in the agent panel"),
            model_used="default", input_tokens=0, output_tokens=0,
            cost_usd=0.0)

    def _session_cookie(self) -> str | None:
        """Read the manually-configured `zed.session` cookie (read-only).

        The dashboard session cookie is required — the editor's native
        credential is rejected by the billing endpoints.
        """
        return (os.environ.get("ZED_SESSION_COOKIE")
                or credentials.get("zed", "session_cookie")
                or _router_config_value("zed", "session_cookie"))

    def is_available(self) -> bool:
        """CLI on PATH, the bundled Zed.app CLI, or a configured credential."""
        available = super().is_available()
        if not self._cli_path:
            # The `zed` CLI ships inside the app bundle even when it isn't
            # symlinked onto PATH.
            bundled = Path("/Applications/Zed.app/Contents/MacOS/cli")
            if bundled.is_file():
                self._cli_path = str(bundled)
                return True
        return available

    def _editor_token(self) -> str | None:
        """The editor's own credential (read-only). Billing endpoints reject
        it, but `/client/users/me` accepts it — enough for plan + Edit
        Predictions when no dashboard cookie is configured."""
        return (os.environ.get("ZED_EDITOR_TOKEN")
                or credentials.get("zed", "editor_token")
                or _router_config_value("zed", "editor_token")
                or self._keychain_editor_auth())

    @staticmethod
    def _keychain_editor_auth() -> str | None:
        """Zed's collab credential from the OS keychain (macOS).

        Stored as an internet password for `https://zed.dev`: the account
        field is the numeric user id, the secret is JSON `{id, token}`.
        cloud.zed.dev expects `Authorization: <user_id> <token>`, so the
        returned value is the complete header value."""
        if sys.platform != "darwin":
            return None
        try:
            proc = subprocess.run(
                ["security", "find-internet-password", "-s", "https://zed.dev", "-g"],
                capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return None
        line = next((l for l in proc.stderr.splitlines()
                     if l.startswith("password:")), None)
        acct = next((l for l in proc.stdout.splitlines()
                     if '"acct"' in l), None)
        if not line or not acct:
            return None
        try:
            secret = json.loads(line.split(":", 1)[1].strip().strip('"'))
            user_id = acct.split("=", 1)[1].strip().strip('"')
            token = secret.get("token")
            if token and user_id.isdigit():
                return f"{user_id} {token}"
        except (json.JSONDecodeError, AttributeError, IndexError):
            pass
        return None

    def _get_json(self, url: str, cookie: str) -> dict | None:
        request = urllib.request.Request(
            url, headers=_browser_headers(f"zed.session={cookie}",
                                          referer="https://zed.dev/"))
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, OSError,
                json.JSONDecodeError, TimeoutError):
            return None

    def _editor_quota(self) -> QuotaReport | None:
        """Fallback via `GET /client/users/me` with the editor credential.

        Shows plan + Edit Predictions only — this endpoint does not expose
        the Token Spend allowance, so that meter is never synthesized."""
        token = self._editor_token()
        if not token:
            return None
        # Keychain-sourced creds are already "<user_id> <token>" (collab
        # auth format); pasted tokens go out as Bearer.
        auth = token if " " in token else f"Bearer {token}"
        request = urllib.request.Request(
            "https://cloud.zed.dev/client/users/me",
            headers={"Authorization": auth, "Accept": "application/json",
                     "User-Agent": "Zed/0.200.0 (macOS)"})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return QuotaReport(
                    state=QuotaState.UNKNOWN,
                    detail=("zed editor token rejected — for quota, paste the "
                            "dashboard cookie: `router cred set zed "
                            "session_cookie` (see PROVIDER_SETUP.md)"),
                    observed_at=datetime.now(timezone.utc),
                )
            return None
        except (urllib.error.URLError, OSError,
                json.JSONDecodeError, TimeoutError):
            return None
        if not isinstance(data, dict):
            return None

        plan = (data.get("plan") or data.get("plan_name")
                or (data.get("subscription") or {}).get("plan")
                or (data.get("subscription") or {}).get("name"))
        parts: list[str] = []
        percents: list[float] = []
        extra: dict[str, object] = {}
        if plan:
            extra["planName"] = plan
        ep = (data.get("edit_predictions") or data.get("editPredictions")
              or (data.get("usage") or {}).get("edit_predictions") or {})
        if isinstance(ep, dict):
            used = ep.get("used") if ep.get("used") is not None else ep.get("usage")
            limit = ep.get("limit")
            if isinstance(used, (int, float)) and isinstance(limit, (int, float)) and limit > 0:
                percent = float(round(max(0.0, (float(limit) - float(used)) / float(limit) * 100.0)))
                parts.append(f"Edit Predictions {percent:g}% ({used:g}/{limit:g})")
                percents.append(percent)
                extra["editPredictionsPercent"] = percent
                extra["editPredictionsUsed"] = float(used)
                extra["editPredictionsLimit"] = float(limit)
            elif isinstance(used, (int, float)):
                parts.append(f"Edit Predictions unlimited ({used:g} used)")
        if not parts and not plan:
            return None
        if not parts:
            parts.append("edit predictions unlimited")
        detail = "Zed" + (f" [{plan}]" if plan else "") + ": " + ", ".join(parts)
        detail += " (editor credential — token spend not exposed)"
        if percents:
            extra["weeklyPercent"] = min(percents)
            extra["meters"] = [{"label": "Edit Predictions", "windows": [
                {"kind": "other", "percent": float(round(min(percents)))}]}]
        return QuotaReport(
            state=QuotaState.DEPLETED if percents and min(percents) == 0 else QuotaState.OK,
            detail=detail,
            observed_at=datetime.now(timezone.utc),
            remaining_percent=min(percents) if percents else None,
            extra=extra,
        )

    def _usage_quota(self) -> QuotaReport | None:
        """Call Zed's cloud billing usage endpoint for metered AI windows.

        Token Spend and Edit Predictions are independent meters; a null
        `limit` means Unlimited (rendered without a meter). Missing or
        malformed fields are omitted, never zero-filled.
        """
        cookie = self._session_cookie()
        if not cookie:
            # No dashboard cookie: the editor credential can't reach billing
            # endpoints, but /client/users/me still yields plan + predictions.
            return self._editor_quota()

        data = self._get_json("https://cloud.zed.dev/frontend/billing/usage", cookie)
        if data is None:
            return None
        plan = data.get("plan")
        usage = data.get("current_usage")
        if not isinstance(usage, dict):
            usage = data

        meters = (
            ("Token Spend", "tokenSpend", ("token_spend", "tokenSpend", "tokenSpendUsage")),
            ("Edit Predictions", "editPredictions", ("edit_predictions", "editPredictions")),
        )
        parts: list[str] = []
        percents: list[float] = []
        extra: dict[str, object] = {}
        if plan:
            extra["planName"] = str(plan)
        item_meters: list[dict[str, object]] = []
        item_windows: list[dict[str, object]] = []
        for label, prefix, keys in meters:
            meter = next((usage[k] for k in keys if isinstance(usage.get(k), dict)), None)
            if meter is None:
                continue
            # cents-denominated fields on Token Spend; unit-less used/limit
            # on Edit Predictions — prefer whichever pair is present.
            limit = meter.get("limit", meter.get("limit_in_cents"))
            used = meter.get("usage", meter.get("used"))
            if used is None:
                used = meter.get("spend_in_cents")
            if limit is None:
                if isinstance(used, (int, float)):
                    parts.append(f"{label} unlimited ({used:g} used)")
                else:
                    parts.append(f"{label} unlimited")
                continue
            if not isinstance(used, (int, float)) or not isinstance(limit, (int, float)) or limit <= 0:
                continue  # malformed -> omitted, never zero-filled
            remaining_pct = float(round(max(0.0, (float(limit) - float(used)) / float(limit) * 100.0)))
            parts.append(f"{label} {remaining_pct:g}% ({used:g}/{limit:g})")
            percents.append(remaining_pct)
            extra[f"{prefix}Percent"] = remaining_pct
            extra[f"{prefix}Used"] = float(used)
            extra[f"{prefix}Limit"] = float(limit)
            item_window: dict[str, object] = {
                "kind": "monthly", "percent": float(round(remaining_pct))}
            item_windows.append(item_window)
            item_meters.append({"label": label, "windows": [item_window]})
        if not parts:
            return None

        # Optional enrichment: plan name + Token Spend reset. A failure here
        # must not hide valid windows from the usage endpoint.
        plan = None
        reset_at = None
        sub = self._get_json(
            "https://cloud.zed.dev/frontend/billing/subscriptions/current", cookie)
        if sub:
            sub_obj = sub.get("subscription") or {}
            period = sub_obj.get("period") or {}
            plan = (sub.get("plan") or (sub.get("plan_info") or {}).get("name")
                    or sub_obj.get("name"))
            reset_at = _parse_iso_datetime(
                sub.get("reset_at") or sub.get("period_end")
                or sub.get("current_period_end") or period.get("end_at"))
        if plan:
            extra["planName"] = plan
        if percents:
            strictest = min(percents)
            extra["weeklyPercent"] = strictest
            if reset_at:
                extra["weeklyResetAt"] = reset_at.isoformat()
                for item_window in item_windows:
                    item_window["resetAt"] = reset_at.isoformat()
            extra["meters"] = item_meters

        detail = "Zed" + (f" [{plan}]" if plan else "") + ": " + ", ".join(parts)
        return QuotaReport(
            state=QuotaState.DEPLETED if percents and min(percents) == 0 else QuotaState.OK,
            detail=detail,
            observed_at=datetime.now(timezone.utc),
            remaining_percent=min(percents) if percents else None,
            reset_at=reset_at,
            extra=extra,
        )

    def probe_quota(self, force: bool = False) -> QuotaReport:
        """Prefer the real vendor usage read (logged); fall back to the base probe."""
        usage_report = self._usage_quota()
        if usage_report:
            log_quota_observation(
                self.name,
                usage_report.state.value,
                usage_report.detail,
                usage_report.reset_at.isoformat() if usage_report.reset_at else None,
                remaining_percent=usage_report.remaining_percent,
            )
            return usage_report
        if not self._session_cookie() and not self._editor_token():
            report = QuotaReport(
                state=QuotaState.UNKNOWN,
                detail=("zed not configured — paste the dashboard cookie "
                        "(`router cred set zed session_cookie`) or the editor "
                        "token (`router cred set zed editor_token`) "
                        "(see PROVIDER_SETUP.md)"),
                observed_at=datetime.now(timezone.utc),
            )
            log_quota_observation(self.name, report.state.value, report.detail)
            return report
        return QuotaReport(
            state=QuotaState.UNKNOWN,
            detail="zed credentials found but cloud endpoints unreachable",
            observed_at=datetime.now(timezone.utc),
        )

class ZcodeAdapter(_WhichAdapter):
    """Z.ai ZCode (GLM Coding Plan) — API-key authed monitor endpoints."""
    label = "zcode"
    candidates: tuple[str, ...] = ()

    def __init__(self) -> None:
        super().__init__()
        self._sentinel_patterns = (
            r"quota",
            r"rate.?limit",
            r"429",
            r"too many requests",
            r"usage limit",
            r"insufficient",
        )

    def _api_key(self) -> str | None:
        return (
            os.environ.get("ZAI_API_KEY")
            or os.environ.get("ZHIPUAI_API_KEY")
            or _router_config_value("zcode", "api_key")
        )

    def _monitor_base(self) -> str:
        return (
            os.environ.get("ZAI_MONITOR_BASE")
            or _router_config_value("zcode", "monitor_base")
            or "https://api.z.ai"
        ).rstrip("/")

    def is_available(self) -> bool:
        """Available with a configured API key even without a local CLI."""
        return self._api_key() is not None or super().is_available()

    def _build_command(self, req: RunRequest) -> list[str]:
        raise RuntimeError("zcode runs go through the GLM coding plan API; no local CLI")

    def run(self, req: RunRequest) -> RunResult:
        """Dispatch the prompt through the GLM coding plan API directly —
        there is no local CLI to shell to."""
        api_key = self._api_key()
        if not api_key:
            raise RuntimeError("zcode is not configured (no ZAI_API_KEY / ZHIPUAI_API_KEY)")
        model = req.model or "glm-4.6"
        body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": req.prompt}],
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{self._monitor_base()}/api/coding/paas/v4/chat/completions",
            data=body,
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json", "Accept": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(request, timeout=req.timeout_s) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (402, 403, 429):
                log_quota_observation(
                    self.name, QuotaState.DEPLETED.value,
                    f"HTTP {e.code} from coding plan API")
                return RunResult(output="", model_used=model, input_tokens=0,
                                 output_tokens=0, cost_usd=0.0, depleted_mid_run=True)
            raise RuntimeError(f"zcode API error: HTTP {e.code}")
        except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as e:
            raise RuntimeError(f"zcode run failed: {e}")

        choice = (data.get("choices") or [{}])[0]
        output = ((choice.get("message") or {}).get("content")
                  or choice.get("text") or "")
        usage = data.get("usage") or {}
        if req.on_output and output:
            req.on_output("stdout", output)
        return RunResult(
            output=output,
            model_used=str(data.get("model") or model),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            cost_usd=0.0,  # subscription plan, not per-token billing
        )

    @staticmethod
    def _unwrap(data: dict) -> dict:
        """Monitor responses are wrapped in an envelope — unwrap before parsing."""
        inner = data.get("data")
        return inner if isinstance(inner, dict) else data

    def _get_json(self, url: str, api_key: str) -> dict | None:
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = json.loads(response.read().decode("utf-8"))
            return self._unwrap(data) if isinstance(data, dict) else None
        except (urllib.error.HTTPError, urllib.error.URLError, OSError,
                json.JSONDecodeError, TimeoutError):
            return None

    def _usage_quota(self) -> QuotaReport | None:
        api_key = self._api_key()
        if not api_key:
            return None
        base = self._monitor_base()

        quota = self._get_json(f"{base}/api/monitor/usage/quota/limit", api_key)
        if quota is None:
            return None

        # Rolling 5-hour usage percent (used, not remaining) + active plan.
        used_pct = None
        for key in ("percentage", "percentUsed", "usagePercent", "used_percent", "percent"):
            value = quota.get(key)
            if isinstance(value, (int, float)):
                used_pct = float(value)
                break
        plan = quota.get("subscription") or quota.get("planName") or quota.get("plan")
        resets = _parse_iso_datetime(
            quota.get("resetTime") or quota.get("reset_at") or quota.get("nextResetTime"))

        parts: list[str] = []
        remaining = None
        if used_pct is not None:
            remaining = float(round(max(0.0, 100.0 - used_pct)))
            parts.append(f"5h rolling {remaining:g}% remaining")
        if plan:
            parts.append(f"plan {plan}")

        extra: dict[str, object] = {}
        if plan:
            extra["planName"] = plan
        if remaining is not None:
            extra["dailyPercent"] = remaining  # closest existing UI slot to the 5h window
            if resets:
                extra["dailyResetAt"] = resets.isoformat()

        # Credit grants: count + earliest expiry, best-effort.
        grants = self._get_json(f"{base}/api/paas/v4/user/credit_grants", api_key)
        if grants:
            grant_list = grants.get("grants") or grants.get("items") or []
            if isinstance(grant_list, list) and grant_list:
                parts.append(f"{len(grant_list)} credit grants")
                extra["creditGrants"] = len(grant_list)
                expiries = [
                    _parse_iso_datetime(g.get("expiry") or g.get("expires_at") or g.get("expired_time"))
                    for g in grant_list if isinstance(g, dict)
                ]
                expiries = [e for e in expiries if e]
                if expiries:
                    extra["earliestGrantExpiry"] = min(expiries).isoformat()

        # Analytics feeds — per-model usage + tool invocations. Not quota
        # windows, so they land in `extra` only.
        model_usage = self._get_json(f"{base}/api/monitor/usage/model-usage", api_key)
        if model_usage:
            samples = model_usage.get("items") or model_usage.get("usage") or model_usage
            extra["modelUsage"] = samples
        tool_usage = self._get_json(f"{base}/api/monitor/usage/tool-usage", api_key)
        if tool_usage:
            samples = tool_usage.get("items") or tool_usage.get("usage") or tool_usage
            extra["toolUsage"] = samples

        if not parts:
            return None
        return QuotaReport(
            state=QuotaState.DEPLETED if remaining == 0 else QuotaState.OK,
            detail="Zcode: " + ", ".join(parts),
            observed_at=datetime.now(timezone.utc),
            remaining_percent=remaining,
            reset_at=resets,
            extra=extra,
        )

    def probe_quota(self, force: bool = False) -> QuotaReport:
        """Prefer the real vendor usage read (logged); fall back to the base probe."""
        usage_report = self._usage_quota()
        if usage_report:
            log_quota_observation(
                self.name,
                usage_report.state.value,
                usage_report.detail,
                usage_report.reset_at.isoformat() if usage_report.reset_at else None,
                remaining_percent=usage_report.remaining_percent,
            )
            return usage_report
        if self._api_key():
            return QuotaReport(
                state=QuotaState.UNKNOWN,
                detail="zcode: API key set but monitor endpoints unreachable",
                observed_at=datetime.now(timezone.utc),
            )
        return super().probe_quota(force=force)


class DeepSeekAdapter(_WhichAdapter):
    """DeepSeek metered API — balance check only; the ledger tracks usage."""
    label = "deepseek"
    kind = "metered"
    candidates: tuple[str, ...] = ()

    def __init__(self) -> None:
        super().__init__()
        self._sentinel_patterns = (
            r"insufficient",
            r"balance",
            r"429",
            r"quota",
            r"rate.?limit",
        )

    def _api_key(self) -> str | None:
        return os.environ.get("DEEPSEEK_API_KEY") or _router_config_value("deepseek", "api_key")

    def is_available(self) -> bool:
        """Available with a configured API key even without a local CLI."""
        return self._api_key() is not None or super().is_available()

    def _build_command(self, req: RunRequest) -> list[str]:
        raise RuntimeError("deepseek has no local CLI; runs would go through its API")

    def _usage_quota(self) -> QuotaReport | None:
        api_key = self._api_key()
        if not api_key:
            return None
        request = urllib.request.Request(
            "https://api.deepseek.com/user/balance",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, OSError,
                json.JSONDecodeError, TimeoutError):
            return None

        parts: list[str] = []
        balances: list[dict[str, object]] = []
        extra: dict[str, object] = {"balances": balances}
        total_all = 0.0
        for info in data.get("balance_infos") or []:
            if not isinstance(info, dict):
                continue
            currency = info.get("currency") or ""
            total = info.get("total_balance")
            if total is None:
                continue
            try:
                total_f = float(total)
            except (TypeError, ValueError):
                continue
            parts.append(f"{total} {currency}")
            total_all += total_f
            entry: dict[str, object] = {"currency": currency, "total": total_f}
            for key in ("granted_balance", "topped_up_balance"):
                sub = info.get(key)
                if sub is None:
                    continue
                try:
                    entry[key.replace("_balance", "")] = float(sub)
                except (TypeError, ValueError):
                    pass
            balances.append(entry)
        if not parts:
            return None

        detail = "deepseek balance: " + ", ".join(parts)
        return QuotaReport(
            state=QuotaState.DEPLETED if total_all <= 0 else QuotaState.OK,
            detail=detail,
            observed_at=datetime.now(timezone.utc),
            remaining_percent=None,  # balance has no window denominator
            extra=extra,
        )

    def probe_quota(self, force: bool = False) -> QuotaReport:
        """Prefer the real vendor usage read (logged); fall back to the base probe."""
        usage_report = self._usage_quota()
        if usage_report:
            log_quota_observation(
                self.name,
                usage_report.state.value,
                usage_report.detail,
                remaining_percent=usage_report.remaining_percent,
            )
            return usage_report
        if self._api_key():
            return QuotaReport(
                state=QuotaState.UNKNOWN,
                detail="deepseek: API key set but balance endpoint unreachable",
                observed_at=datetime.now(timezone.utc),
            )
        return super().probe_quota(force=force)


class XAIAdapter(_WhichAdapter):
    """xAI / Grok — CLI-proxy subscription path plus API-key credit balance."""
    label = "grok"
    candidates = ("grok",)
    _CLI_PROXY = "https://cli-chat-proxy.grok.com/v1"

    def __init__(self) -> None:
        super().__init__()
        self._sentinel_patterns = (
            r"quota",
            r"rate.?limit",
            r"429",
            r"402",
            r"403",
            r"too many requests",
            r"usage limit",
            r"credit",
        )

    def _build_command(self, req: RunRequest) -> list[str]:
        assert self._cli_path is not None
        return [self._cli_path, "-p", req.prompt]

    def _grok_cli_credentials(self) -> tuple[str, str] | None:
        """Read ~/.grok/auth.json (read-only; `grok` owns refresh)."""
        auth_path = Path.home() / ".grok" / "auth.json"
        try:
            data = json.loads(auth_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        tokens = data.get("tokens") or data
        if not isinstance(tokens, dict):
            return None
        # The token is nested under a key like "https://auth.x.ai::<id>.key".
        token_entry = next(
            (v for k, v in tokens.items() if "auth.x.ai" in str(k)), None)
        token = None
        if isinstance(token_entry, dict):
            token = token_entry.get("access_token") or token_entry.get("token")
        elif isinstance(token_entry, str):
            token = token_entry
        if not token:
            return None
        subject = (data.get("subject") or data.get("user_id")
                   or (tokens.get("subject") if isinstance(tokens, dict) else None)
                   or GeminiAdapter._jwt_claim(token, "sub"))
        return token, (subject or "")

    def _cli_get(self, path: str, token: str, subject: str) -> dict | None:
        request = urllib.request.Request(
            f"{self._CLI_PROXY}/{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "X-XAI-Token-Auth": "xai-grok-cli",
                "x-userid": subject,
                "Accept": "application/json",
            })
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, OSError,
                json.JSONDecodeError, TimeoutError):
            return None

    @staticmethod
    def _val(wrapper) -> float | None:
        """Unwrap `{val: n}` scalar wrappers; absent stays absent."""
        value = wrapper.get("val") if isinstance(wrapper, dict) else wrapper
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _cli_subscription_quota(self) -> QuotaReport | None:
        creds = self._grok_cli_credentials()
        if not creds:
            return None
        token, subject = creds

        data = self._cli_get("billing?format=credits", token, subject)
        if data is None:
            return None
        config = data.get("config") or {}

        used_pct = None
        credit_pct = config.get("creditUsagePercent")
        if isinstance(credit_pct, (int, float)):
            used_pct = float(credit_pct)
        else:
            # Fallback: on-demand usage / cap. Absent is unknown, never 0%.
            demand_used = self._val(config.get("onDemandUsed"))
            demand_cap = self._val(config.get("onDemandCap"))
            if demand_used is not None and demand_cap:
                used_pct = demand_used / demand_cap * 100.0

        period = config.get("currentPeriod") or {}
        period_type = period.get("type", "")
        reset_at = _parse_iso_datetime(period.get("end"))
        window_label = {"USAGE_PERIOD_TYPE_WEEKLY": "weekly"}.get(period_type, "monthly")

        parts: list[str] = []
        remaining = None
        if used_pct is not None:
            remaining = float(round(max(0.0, 100.0 - used_pct)))
            parts.append(f"{window_label} {remaining:g}% remaining")
        monthly_limit = self._val(config.get("monthlyLimit"))
        monthly_used = self._val(config.get("used"))
        if monthly_limit and monthly_used is not None:
            parts.append(f"${monthly_used:g}/${monthly_limit:g} monthly")
        prepaid = self._val(config.get("prepaid_balance"))
        if prepaid is not None:
            parts.append(f"${prepaid:g} prepaid")

        plan = None
        settings = self._cli_get("settings", token, subject)
        if settings:
            plan = settings.get("subscription_tier_display")

        if not parts and plan is None:
            return None

        extra: dict[str, object] = {}
        if plan:
            extra["planName"] = plan
        if remaining is not None:
            slot = "weekly" if window_label == "weekly" else "daily"
            extra[f"{slot}Percent"] = remaining
            grok_window: dict[str, object] = {
                "kind": window_label, "percent": float(round(remaining))}
            if reset_at:
                extra[f"{slot}ResetAt"] = reset_at.isoformat()
                grok_window["resetAt"] = reset_at.isoformat()
            extra["meters"] = [{"label": "Credits", "windows": [grok_window]}]
        if monthly_limit is not None:
            extra["monthlyLimitUsd"] = monthly_limit
        if monthly_used is not None:
            extra["monthlyUsedUsd"] = monthly_used
        if prepaid is not None:
            extra["prepaidBalanceUsd"] = prepaid

        return QuotaReport(
            state=QuotaState.DEPLETED if remaining == 0 else QuotaState.OK,
            detail="grok" + (f" [{plan}]" if plan else "") + ": " + ", ".join(parts),
            observed_at=datetime.now(timezone.utc),
            remaining_percent=remaining,
            reset_at=reset_at,
            extra=extra,
        )

    def _api_key_quota(self) -> QuotaReport | None:
        """Separate API-key pool: api.x.ai/v1/billing/credits."""
        api_key = (os.environ.get("XAI_API_KEY")
                   or _router_config_value("grok", "api_key"))
        if not api_key:
            return None
        request = urllib.request.Request(
            "https://api.x.ai/v1/billing/credits",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, OSError,
                json.JSONDecodeError, TimeoutError):
            return None
        balance = (data.get("balance") or data.get("credits")
                   or (data.get("data") or {}).get("balance"))
        if balance is None:
            return None
        try:
            balance_f = float(balance)
        except (TypeError, ValueError):
            return None
        return QuotaReport(
            state=QuotaState.DEPLETED if balance_f <= 0 else QuotaState.OK,
            detail=f"grok api-key: ${balance_f:g} credits",
            observed_at=datetime.now(timezone.utc),
            remaining_percent=None,
            extra={"apiKeyBalanceUsd": balance_f},
        )

    def _usage_quota(self) -> QuotaReport | None:
        return self._cli_subscription_quota() or self._api_key_quota()

    def probe_quota(self, force: bool = False) -> QuotaReport:
        """Prefer the real vendor usage read (logged); fall back to the base probe."""
        usage_report = self._usage_quota()
        if usage_report:
            log_quota_observation(
                self.name,
                usage_report.state.value,
                usage_report.detail,
                usage_report.reset_at.isoformat() if usage_report.reset_at else None,
                remaining_percent=usage_report.remaining_percent,
            )
            return usage_report
        if (Path.home() / ".grok" / "auth.json").exists() or os.environ.get("XAI_API_KEY"):
            return QuotaReport(
                state=QuotaState.UNKNOWN,
                detail="grok: credentials present but endpoints unreachable — run `grok` once to refresh the CLI session",
                observed_at=datetime.now(timezone.utc),
            )
        return super().probe_quota(force=force)


class AmpAdapter(_WhichAdapter):
    """Amp — `amp usage` output preferred; AMP_API_KEY RPC fallback."""
    label = "amp"
    candidates = ("amp",)

    def __init__(self) -> None:
        super().__init__()
        self._sentinel_patterns = (
            r"quota",
            r"rate.?limit",
            r"429",
            r"too many requests",
            r"usage limit",
            r"out of (?:credits|usage)",
        )

    def _build_command(self, req: RunRequest) -> list[str]:
        assert self._cli_path is not None
        return [self._cli_path, "-p", req.prompt]

    def _cli_usage_text(self) -> str | None:
        """Run `amp usage`; the output format is server-authoritative."""
        if self._cli_path is None and not self.is_available():
            return None
        if not self._cli_path:
            return None
        try:
            result = subprocess.run(
                [self._cli_path, "usage"], capture_output=True, text=True, timeout=15)
        except (subprocess.TimeoutExpired, OSError):
            return None
        if result.returncode != 0:
            return None
        return (result.stdout or "") + (result.stderr or "")

    @staticmethod
    def _money(text: str) -> float | None:
        try:
            return float(text.replace(",", "").lstrip("$"))
        except (ValueError, AttributeError):
            return None

    def _parse_usage_text(self, text: str) -> tuple[list[str], list[float], dict]:
        """Parse `amp usage` text. No-fabrication rules: balance-only rows
        carry no percentage; unknown stays unknown."""
        parts: list[str] = []
        percents: list[float] = []
        extra: dict[str, object] = {}
        amp_meters: list[dict[str, object]] = []

        # Amount form: "agent usage $12.34 of $50.00" (labels drift; match amounts)
        for match in re.finditer(
                r"([A-Za-z ]+?)\s*(?:usage|balance)?\s*[:.]?\s*\$?([\d,]+(?:\.\d+)?)\s*(?:of|/)\s*\$?([\d,]+(?:\.\d+)?)",
                text, re.IGNORECASE):
            label = match.group(1).strip().lower() or "usage"
            remaining = self._money(match.group(2))
            limit = self._money(match.group(3))
            if remaining is None or not limit:
                continue
            percent = float(round(max(0.0, min(100.0, remaining / limit * 100.0))))
            parts.append(f"{label} ${remaining:g}/${limit:g} ({percent:g}%)")
            percents.append(percent)
            extra[re.sub(r"[^A-Za-z0-9]+", "-", label).strip("-") + "Percent"] = percent
            amp_window: dict[str, object] = {"kind": "other", "percent": float(round(percent))}
            if "free" in label:
                amp_window["kind"] = "daily"
                reset = self._amp_free_reset()
                if reset:
                    extra["dailyResetAt"] = reset.isoformat()
                    amp_window["resetAt"] = reset.isoformat()
            amp_meters.append({"label": label, "windows": [amp_window]})

        # Percent form: "42% remaining" / "58% used"
        for match in re.finditer(r"([\d.]+)\s*%\s*(remaining|left|used)", text, re.IGNORECASE):
            value = float(match.group(1))
            percent = float(round(value if match.group(2).lower() != "used" else 100.0 - value))
            if percent not in percents:
                parts.append(f"{percent:g}% remaining")
                percents.append(percent)

        # Bare balances: "$12.34 credit balance", "8 orb hours" -> no percent
        for match in re.finditer(r"(credit balance|credits?)\s*[:.]?\s*\$?([\d,]+(?:\.\d+)?)",
                                 text, re.IGNORECASE):
            amount = self._money(match.group(2))
            if amount is not None:
                parts.append(f"${amount:g} credits")
                extra.setdefault("creditBalanceUsd", amount)

        if amp_meters:
            extra["meters"] = amp_meters
        return parts, percents, extra

    @staticmethod
    def _amp_free_reset() -> datetime | None:
        """Amp Free daily quota resets at 8:00 PM America/New_York."""
        try:
            ny = ZoneInfo("America/New_York")
        except Exception:
            return None
        now = datetime.now(ny)
        reset = datetime.combine(now.date(), dt_time(20, 0), tzinfo=ny)
        if reset <= now:
            reset += timedelta(days=1)
        return reset

    def _session_cookie(self) -> str | None:
        """Last-resort manual paste of an ampcode.com session cookie."""
        return (os.environ.get("AMP_SESSION_COOKIE")
                or credentials.get("amp", "session_cookie")
                or _router_config_value("amp", "session_cookie"))

    def _settings_quota(self) -> QuotaReport | None:
        """Last resort: the ampcode.com/settings page payload via a pasted
        browser cookie. No documented structured endpoint — the same text
        parser scans whatever the page returns."""
        cookie = self._session_cookie()
        if not cookie:
            return None
        request = urllib.request.Request(
            "https://ampcode.com/settings",
            headers=_browser_headers(cookie, "text/html,application/json",
                                     referer="https://ampcode.com/"))
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                body = response.read().decode("utf-8", errors="replace")
        except (urllib.error.HTTPError, urllib.error.URLError, OSError,
                TimeoutError):
            return None
        parts, percents, extra = self._parse_usage_text(body)
        if not parts:
            return None
        reset = _parse_iso_datetime(extra.get("dailyResetAt"))
        return QuotaReport(
            state=QuotaState.DEPLETED if percents and min(percents) == 0 else QuotaState.OK,
            detail="amp (settings page): " + ", ".join(parts),
            observed_at=datetime.now(timezone.utc),
            remaining_percent=min(percents) if percents else None,
            reset_at=reset,
            extra=extra,
        )

    def _api_quota(self) -> QuotaReport | None:
        api_key = os.environ.get("AMP_API_KEY") or _router_config_value("amp", "api_key")
        if not api_key:
            return None
        request = urllib.request.Request(
            "https://ampcode.com/api/internal?userDisplayBalanceInfo",
            data=b"{}",
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json", "Accept": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, OSError,
                json.JSONDecodeError, TimeoutError):
            return None

        parts: list[str] = []
        percents: list[float] = []
        extra: dict[str, object] = {}
        entries = data.get("balances") or data.get("rows") or data.get("items") or []
        extra_meters: list[dict[str, object]] = []
        if isinstance(entries, dict):
            entries = [dict(v, name=k) if isinstance(v, dict) else {"name": k, "balance": v}
                       for k, v in entries.items()]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or entry.get("label") or entry.get("meter") or "balance")
            remaining = self._money(str(entry.get("remaining", entry.get("balance", ""))))
            limit = self._money(str(entry.get("limit", ""))) if entry.get("limit") is not None else None
            if remaining is None:
                continue
            if limit:
                percent = float(round(max(0.0, min(100.0, remaining / limit * 100.0))))
                parts.append(f"{name} ${remaining:g}/${limit:g} ({percent:g}%)")
                percents.append(percent)
                extra[re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-") + "Percent"] = percent
                api_window: dict[str, object] = {
                    "kind": "other", "percent": float(round(percent))}
                if "free" in name.lower():
                    api_window["kind"] = "daily"
                    reset = self._amp_free_reset()
                    if reset:
                        extra["dailyResetAt"] = reset.isoformat()
                        api_window["resetAt"] = reset.isoformat()
                extra_meters.append({"label": name, "windows": [api_window]})
            else:
                parts.append(f"{name} ${remaining:g}")
                extra[re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-") + "Usd"] = remaining
        if not parts:
            return None
        if extra_meters:
            extra["meters"] = extra_meters
        return QuotaReport(
            state=QuotaState.DEPLETED if percents and min(percents) == 0 else QuotaState.OK,
            detail="amp: " + ", ".join(parts),
            observed_at=datetime.now(timezone.utc),
            remaining_percent=min(percents) if percents else None,
            reset_at=_parse_iso_datetime(extra.get("dailyResetAt")),
            extra=extra,
        )

    def _usage_quota(self) -> QuotaReport | None:
        text = self._cli_usage_text()
        if text:
            parts, percents, extra = self._parse_usage_text(text)
            if parts:
                return QuotaReport(
                    state=QuotaState.DEPLETED if percents and min(percents) == 0 else QuotaState.OK,
                    detail="amp: " + ", ".join(parts),
                    observed_at=datetime.now(timezone.utc),
                    remaining_percent=min(percents) if percents else None,
                    reset_at=_parse_iso_datetime(extra.get("dailyResetAt")),
                    extra=extra,
                )
        return self._api_quota() or self._settings_quota()

    def probe_quota(self, force: bool = False) -> QuotaReport:
        """Prefer the real vendor usage read (logged); fall back to the base probe."""
        usage_report = self._usage_quota()
        if usage_report:
            log_quota_observation(
                self.name,
                usage_report.state.value,
                usage_report.detail,
                usage_report.reset_at.isoformat() if usage_report.reset_at else None,
                remaining_percent=usage_report.remaining_percent,
            )
            return usage_report
        return super().probe_quota(force=force)


class KimiAdapter(_WhichAdapter):
    """Kimi Code membership (api.kimi.com) — distinct from Moonshot PAYG."""
    label = "kimi"
    candidates = ("kimi", "kimi-code")

    def __init__(self) -> None:
        super().__init__()
        self._sentinel_patterns = (
            r"quota",
            r"rate.?limit",
            r"429",
            r"too many requests",
            r"usage limit",
            r"insufficient",
        )

    def _build_command(self, req: RunRequest) -> list[str]:
        assert self._cli_path is not None
        return [self._cli_path, "-p", req.prompt]

    def _membership_token(self) -> str | None:
        """Read the CLI's OAuth credential (read-only; the CLI owns refresh)."""
        creds_path = Path.home() / ".kimi-code" / "credentials" / "kimi-code.json"
        try:
            data = json.loads(creds_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        token = (data.get("access_token") or data.get("accessToken")
                 or (data.get("token") or {}).get("access_token"))
        return token or None

    def _get_json(self, url: str, token: str) -> dict | None:
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, OSError,
                json.JSONDecodeError, TimeoutError):
            return None

    @staticmethod
    def _num(value) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _membership_quota(self) -> QuotaReport | None:
        token = self._membership_token()
        if not token:
            return None

        usages = self._get_json("https://api.kimi.com/coding/v1/usages", token)
        if usages is None:
            usages = self._get_json("https://api.kimi.ai/coding/v1/usages", token)
        if usages is None:
            return None

        me = self._get_json("https://api.kimi.com/coding/v1/me", token) or {}
        account = me.get("email")
        tier = me.get("user_level_name")
        goods_v2 = me.get("goods_version") == 2  # weekly window suppressed on V2

        parts: list[str] = []
        percents: list[float] = []
        extra: dict[str, object] = {}
        if account:
            extra["account"] = account
        if tier:
            extra["planName"] = tier

        # limits[] carries exact window counts — prefer over the lagging ratios.
        meter_windows: list[tuple[str, float, datetime | None]] = []
        for entry in usages.get("limits") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or entry.get("window") or "limit")
            remaining = self._num(entry.get("remaining"))
            limit = self._num(entry.get("limit"))
            if remaining is None and limit:
                used = self._num(entry.get("used"))
                if used is not None:
                    remaining = limit - used
            if remaining is None or limit is None or limit <= 0:
                continue
            percent = float(round(max(0.0, min(100.0, remaining / limit * 100.0))))
            parts.append(f"{name} {remaining:g}/{limit:g} ({percent:g}%)")
            percents.append(percent)
            key = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")
            extra[f"{key}Percent"] = percent
            reset = _parse_iso_datetime(entry.get("reset_at") or entry.get("resetAt"))
            if reset:
                extra[f"{key}ResetAt"] = reset.isoformat()
            meter_windows.append((name, percent, reset))

        # Ratio pools: usages.limit_5h / limit_7d / limit_month_*, used ratio.
        ratio_map = usages.get("usages") or {}
        for key, label, slot in (
            ("limit_5h", "5h", "daily"),
            ("limit_7d", "7d", "weekly"),
            ("limit_month_total", "month", None),
            ("limit_month_code", "month-code", None),
        ):
            if slot == "weekly" and goods_v2:
                continue  # V2 plans suppress the weekly window
            ratio = self._num(ratio_map.get(key))
            if ratio is None or f"{label}Percent" in extra:
                continue
            used_ratio = ratio / 100.0 if ratio > 1 else ratio
            remaining = float(round(max(0.0, (1.0 - used_ratio) * 100.0)))
            parts.append(f"{label} {remaining:g}% remaining")
            percents.append(remaining)
            extra[f"{label}Percent"] = remaining
            if slot:
                extra[f"{slot}Percent"] = remaining
            meter_windows.append((label, remaining, None))

        if not parts:
            return None
        meters = _meters_from_windows(meter_windows, "Kimi")
        if meters:
            extra["meters"] = meters
        label = "kimi" + (f" ({account})" if account else "") + (f" [{tier}]" if tier else "")
        return QuotaReport(
            state=QuotaState.DEPLETED if percents and min(percents) == 0 else QuotaState.OK,
            detail=label + ": " + ", ".join(parts),
            observed_at=datetime.now(timezone.utc),
            remaining_percent=min(percents) if percents else None,
            extra=extra,
        )

    def _payg_quota(self) -> QuotaReport | None:
        """Moonshot Open Platform PAYG balance — a separate product/pool."""
        api_key = os.environ.get("KIMI_API_KEY") or _router_config_value("kimi", "api_key")
        if not api_key:
            return None
        request = urllib.request.Request(
            "https://api.moonshot.ai/v1/users/me/balance",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, OSError,
                json.JSONDecodeError, TimeoutError):
            return None
        data = data.get("data") if isinstance(data.get("data"), dict) else data
        available = self._num(data.get("available_balance"))
        if available is None:
            return None
        extra: dict[str, object] = {"availableBalance": available}
        parts = [f"${available:g} available"]
        for key, label in (("voucher_balance", "voucher"), ("cash_balance", "cash")):
            value = self._num(data.get(key))
            if value is not None:
                parts.append(f"${value:g} {label}")
                extra[f"{label}Balance"] = value
        return QuotaReport(
            state=QuotaState.DEPLETED if available <= 0 else QuotaState.OK,
            detail="kimi PAYG: " + ", ".join(parts),
            observed_at=datetime.now(timezone.utc),
            remaining_percent=None,
            extra=extra,
        )

    def _usage_quota(self) -> QuotaReport | None:
        return self._membership_quota() or self._payg_quota()

    def probe_quota(self, force: bool = False) -> QuotaReport:
        """Prefer the real vendor usage read (logged); fall back to the base probe."""
        usage_report = self._usage_quota()
        if usage_report:
            log_quota_observation(
                self.name,
                usage_report.state.value,
                usage_report.detail,
                usage_report.reset_at.isoformat() if usage_report.reset_at else None,
                remaining_percent=usage_report.remaining_percent,
            )
            return usage_report
        return super().probe_quota(force=force)


class MiniMaxAdapter(_WhichAdapter):
    """MiniMax coding plan — token_plan/remains, interval + weekly windows."""
    label = "minimax"
    candidates: tuple[str, ...] = ()

    def __init__(self) -> None:
        super().__init__()
        self._sentinel_patterns = (
            r"quota",
            r"rate.?limit",
            r"429",
            r"too many requests",
            r"insufficient",
            r"token.?plan",
        )

    def _api_key(self) -> str | None:
        key = (os.environ.get("MINIMAX_API_KEY")
               or _router_config_value("minimax", "api_key"))
        if key:
            return key
        config_path = Path.home() / ".mmx" / "config.json"
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data.get("api_key") or data.get("apiKey") or data.get("access_token")

    def _base(self) -> str:
        return (os.environ.get("MINIMAX_API_BASE")
                or _router_config_value("minimax", "api_base")
                or "https://api.minimax.io").rstrip("/")

    def is_available(self) -> bool:
        """Available with a configured API key even without a local CLI."""
        return self._api_key() is not None or super().is_available()

    def _build_command(self, req: RunRequest) -> list[str]:
        raise RuntimeError("minimax has no local CLI; runs would go through its API")

    @staticmethod
    def _num(value) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _ms(value) -> datetime | None:
        try:
            return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return None

    def _usage_quota(self) -> QuotaReport | None:
        api_key = self._api_key()
        if not api_key:
            return None
        base = self._base()
        for path in ("/v1/token_plan/remains",
                     "/v1/api/openplatform/coding_plan/remains"):
            request = urllib.request.Request(
                base + path,
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    data = json.loads(response.read().decode("utf-8"))
            except (urllib.error.HTTPError, urllib.error.URLError, OSError,
                    json.JSONDecodeError, TimeoutError):
                continue
            base_resp = data.get("base_resp") or {}
            if base_resp.get("status_code") not in (None, 0):
                return None  # API-level error; don't parse partial data
            break
        else:
            return None

        parts: list[str] = []
        percents: list[float] = []
        resets: list[datetime] = []
        extra: dict[str, object] = {}
        meter_windows: list[tuple[str, float, datetime | None]] = []
        for entry in data.get("model_remains") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("model_name") or entry.get("model") or "model")
            key = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")

            # Rolling interval window: counts preferred, *_remaining_percent
            # is already "remaining" (no inversion needed).
            interval_pct = None
            total = self._num(entry.get("current_interval_total_count"))
            used = self._num(entry.get("current_interval_usage_count"))
            if total and total > 0 and used is not None:
                interval_pct = float(round(max(0.0, (1.0 - used / total) * 100.0)))
            else:
                for k, v in entry.items():
                    if k.endswith("remaining_percent") and "weekly" not in k:
                        interval_pct = self._num(v)
                        if interval_pct is not None:
                            interval_pct = float(round(interval_pct))
                        break
            if interval_pct is not None:
                parts.append(f"{name} interval {interval_pct:g}%")
                percents.append(interval_pct)
                extra[f"{key}IntervalPercent"] = interval_pct
                end = self._ms(entry.get("end_time"))
                if end:
                    resets.append(end)
                    extra[f"{key}IntervalResetAt"] = end.isoformat()
                meter_windows.append((f"{name} interval", interval_pct, end))
                if name == "general":
                    extra["dailyPercent"] = interval_pct
                    if end:
                        extra["dailyResetAt"] = end.isoformat()

            # Weekly window.
            weekly_pct = None
            wtotal = self._num(entry.get("weekly_total_count"))
            wused = self._num(entry.get("weekly_usage_count"))
            if wtotal and wtotal > 0 and wused is not None:
                weekly_pct = float(round(max(0.0, (1.0 - wused / wtotal) * 100.0)))
            else:
                wv = entry.get("weekly_remaining_percent")
                weekly_pct = self._num(wv)
                if weekly_pct is not None:
                    weekly_pct = float(round(weekly_pct))
            if weekly_pct is not None:
                parts.append(f"{name} weekly {weekly_pct:g}%")
                percents.append(weekly_pct)
                extra[f"{key}WeeklyPercent"] = weekly_pct
                wend = self._ms(entry.get("weekly_end_time"))
                if wend:
                    resets.append(wend)
                meter_windows.append((f"{name} weekly", weekly_pct, wend))
                if name == "general":
                    extra["weeklyPercent"] = weekly_pct
                    if wend:
                        extra["weeklyResetAt"] = wend.isoformat()

        if not parts:
            return None
        minimax_meters = _meters_from_windows(meter_windows, "MiniMax")
        if minimax_meters:
            extra["meters"] = minimax_meters
        return QuotaReport(
            state=QuotaState.DEPLETED if percents and min(percents) == 0 else QuotaState.OK,
            detail="minimax: " + ", ".join(parts),
            observed_at=datetime.now(timezone.utc),
            remaining_percent=min(percents) if percents else None,
            reset_at=min(resets) if resets else None,
            extra=extra,
        )

    def probe_quota(self, force: bool = False) -> QuotaReport:
        """Prefer the real vendor usage read (logged); fall back to the base probe."""
        usage_report = self._usage_quota()
        if usage_report:
            log_quota_observation(
                self.name,
                usage_report.state.value,
                usage_report.detail,
                usage_report.reset_at.isoformat() if usage_report.reset_at else None,
                remaining_percent=usage_report.remaining_percent,
            )
            return usage_report
        if self._api_key():
            return QuotaReport(
                state=QuotaState.UNKNOWN,
                detail="minimax: API key set but token_plan endpoint unreachable",
                observed_at=datetime.now(timezone.utc),
            )
        return super().probe_quota(force=force)
