"""Tests for adapter implementations."""
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from router.core.adapter import QuotaState, RunRequest
from router.core.desktop import find_desktop_app
from router.adapters.subscriptions import (ClaudeCodeAdapter, CursorAdapter,
                                          GeminiAdapter, DevinAdapter,
                                          CodexAdapter, CopilotAdapter,
                                          PerplexityAdapter, ZedAdapter,
                                          ZcodeAdapter, DeepSeekAdapter, XAIAdapter,
                                          AmpAdapter, KimiAdapter, MiniMaxAdapter)


class TestAdapterContract(unittest.TestCase):
    """Test that all adapters implement the required interface."""

    def test_adapter_interface(self):
        """All adapters must implement the Adapter protocol."""
        adapter_classes = [
            ClaudeCodeAdapter, CursorAdapter, GeminiAdapter,
            DevinAdapter, CodexAdapter, CopilotAdapter, PerplexityAdapter,
            ZedAdapter, ZcodeAdapter, DeepSeekAdapter, XAIAdapter, AmpAdapter,
            KimiAdapter, MiniMaxAdapter
        ]

        for adapter_class in adapter_classes:
            with self.subTest(adapter=adapter_class.__name__):
                adapter = adapter_class()
                self.assertTrue(hasattr(adapter, 'name'))
                self.assertTrue(hasattr(adapter, 'kind'))
                expected_kind = "metered" if adapter_class is DeepSeekAdapter else "subscription"
                self.assertEqual(adapter.kind, expected_kind)
                self.assertTrue(hasattr(adapter, 'is_available'))
                self.assertTrue(hasattr(adapter, 'probe_quota'))
                self.assertTrue(hasattr(adapter, 'models'))
                self.assertTrue(hasattr(adapter, 'health'))
                self.assertTrue(hasattr(adapter, 'estimate_cost_usd'))
                self.assertTrue(hasattr(adapter, 'run'))

    def test_claude_adapter_sentinels(self):
        adapter = ClaudeCodeAdapter()
        is_depleted, reset_time = adapter._check_sentinels("rate limit exceeded")
        self.assertTrue(is_depleted)

        is_depleted, _ = adapter._check_sentinels("quota exceeded")
        self.assertTrue(is_depleted)

        is_depleted, _ = adapter._check_sentinels("usage limit reached")
        self.assertTrue(is_depleted)

        is_depleted, _ = adapter._check_sentinels("normal output")
        self.assertFalse(is_depleted)


class TestSentinelDetection(unittest.TestCase):
    """Test sentinel pattern detection across adapters."""

    def test_claude_sentinels(self):
        adapter = ClaudeCodeAdapter()

        test_cases = [
            "Error: rate limit exceeded",
            "API quota exceeded",
            "Monthly usage limit reached",
            "HTTP 429: too many requests",
            "Daily limit exceeded",
        ]

        for case in test_cases:
            with self.subTest(case=case):
                is_depleted, _ = adapter._check_sentinels(case)
                self.assertTrue(is_depleted, f"Failed to detect sentinel in: {case}")

    def test_vendor_reported_quota_percentage(self):
        adapter = ClaudeCodeAdapter()
        self.assertEqual(adapter._extract_quota_percent("Usage limit: 42% remaining"), 42.0)
        self.assertEqual(adapter._extract_quota_percent("You have 7.5% of your weekly limit left"), 7.5)
        self.assertEqual(adapter._extract_quota_percent("Remaining: 89%"), 89.0)
        self.assertEqual(adapter._extract_quota_percent("25% remaining"), 25.0)
        self.assertIsNone(adapter._extract_quota_percent("Task is 80% complete"))
        self.assertIsNone(adapter._extract_quota_percent("Quota is available"))

    def test_non_sentinel_output(self):
        adapter = ClaudeCodeAdapter()

        test_cases = [
            "Hello world",
            "Task completed successfully",
            "Running tests...",
            "Model output here",
        ]

        for case in test_cases:
            with self.subTest(case=case):
                is_depleted, _ = adapter._check_sentinels(case)
                self.assertFalse(is_depleted, f"False positive on: {case}")


class TestDesktopDetection(unittest.TestCase):
    """Test detection of installed desktop applications."""

    def test_find_desktop_app_returns_none_for_missing_app(self):
        empty_dir = Path(tempfile.mkdtemp())
        with patch("router.core.desktop._app_dirs", return_value=[empty_dir]):
            self.assertIsNone(find_desktop_app("cursor"))

    def test_adapter_shows_desktop_app_when_no_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_dir = Path(tmp) / "Applications"
            app_dir.mkdir()
            bundle = app_dir / "Cursor.app"
            bundle.mkdir()
            # Force the adapter to think no CLI is on PATH, apps live in tmp,
            # and we're on macOS so .app bundles are inspected.
            with patch("router.core.desktop._app_dirs", return_value=[app_dir]):
                with patch("router.core.desktop.SYSTEM", "Darwin"):
                    with patch("router.adapters.subscriptions.shutil.which", return_value=None):
                        adapter = CursorAdapter()
                        self.assertTrue(adapter.is_available())
                        self.assertIsNone(adapter._cli_path)
                        self.assertIsNotNone(adapter._desktop_path)

    def test_desktop_only_probe_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_dir = Path(tmp) / "Applications"
            app_dir.mkdir()
            bundle = app_dir / "Claude.app"
            bundle.mkdir()
            with patch("router.core.desktop._app_dirs", return_value=[app_dir]):
                with patch("router.core.desktop.SYSTEM", "Darwin"):
                    with patch("router.adapters.subscriptions.shutil.which", return_value=None):
                        with patch("router.adapters.subscriptions.urllib.request.urlopen",
                                   side_effect=OSError("no network in tests")):
                            with patch.object(ClaudeCodeAdapter, "_claude_credentials",
                                              return_value=None):
                                adapter = ClaudeCodeAdapter()
                                report = adapter.probe_quota()
                        self.assertEqual(report.state, QuotaState.UNKNOWN)
                        self.assertIn("desktop app", report.detail.lower())


class TestDevinGetUserStatusQuota(unittest.TestCase):
    """Test Devin Desktop GetUserStatus quota probing."""

    def _make_response(self, data: dict):
        response = Mock()
        response.read.return_value = json.dumps(data).encode("utf-8")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def _write_credentials(self, home: Path) -> None:
        creds_dir = home / ".local" / "share" / "devin"
        creds_dir.mkdir(parents=True)
        creds_dir.joinpath("credentials.toml").write_text(
            'windsurf_api_key = "devin-session-token$test"\n', encoding="utf-8"
        )

    def test_devin_get_user_status_parses_quota(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._write_credentials(home)
            status_response = {
                "userStatus": {
                    "planStatus": {
                        "planInfo": {"planName": "Max", "hideDailyQuota": False, "isDevin": True},
                        "dailyQuotaRemainingPercent": 73,
                        "weeklyQuotaRemainingPercent": 45,
                        "dailyQuotaResetAtUnix": "1780560000",
                        "weeklyQuotaResetAtUnix": "1780819200",
                    }
                }
            }

            def urlopen(request, timeout=None):
                url = request.full_url if hasattr(request, "full_url") else request.get_full_url()
                self.assertIn("SeatManagementService/GetUserStatus", url)
                return self._make_response(status_response)

            with patch("pathlib.Path.home", return_value=home):
                with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                    adapter = DevinAdapter()
                    report = adapter.probe_quota()
        self.assertIsNotNone(report)
        self.assertEqual(report.remaining_percent, 45.0)  # bar uses the tighter value
        self.assertIn("daily 73%", report.detail)
        self.assertIn("weekly 45%", report.detail)
        self.assertIsNotNone(report.reset_at)

    def test_devin_get_user_status_shows_daily_and_weekly(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._write_credentials(home)
            status_response = {
                "userStatus": {
                    "planStatus": {
                        "planInfo": {"planName": "Pro", "hideDailyQuota": False, "isDevin": True},
                        "dailyQuotaRemainingPercent": 50,
                        "weeklyQuotaRemainingPercent": 75,
                        "dailyQuotaResetAtUnix": "1780560000",
                        "weeklyQuotaResetAtUnix": "1780819200",
                    }
                }
            }

            def urlopen(request, timeout=None):
                return self._make_response(status_response)

            with patch("pathlib.Path.home", return_value=home):
                with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                    adapter = DevinAdapter()
                    report = adapter.probe_quota()
        self.assertIsNotNone(report)
        self.assertEqual(report.remaining_percent, 50.0)  # bar uses the tighter daily limit
        self.assertIn("daily 50%", report.detail)
        self.assertIn("weekly 75%", report.detail)

    def test_devin_sqlite_credentials_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            app_dir = home / "Library" / "Application Support" / "Devin" / "User" / "globalStorage"
            app_dir.mkdir(parents=True)
            import sqlite3
            db_path = app_dir / "state.vscdb"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
                conn.execute(
                    "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
                    ("windsurfAuthStatus", json.dumps({"apiKey": "devin-session-token$sqlite"}))
                )
                conn.commit()
            finally:
                conn.close()

            status_response = {
                "userStatus": {
                    "planStatus": {
                        "planInfo": {"planName": "Teams", "hideDailyQuota": False, "isDevin": True},
                        "dailyQuotaRemainingPercent": 60,
                        "weeklyQuotaRemainingPercent": 80,
                        "dailyQuotaResetAtUnix": "1780560000",
                        "weeklyQuotaResetAtUnix": "1780819200",
                    }
                }
            }

            calls = []
            def urlopen(request, timeout=None):
                url = request.full_url if hasattr(request, "full_url") else request.get_full_url()
                calls.append(url)
                return self._make_response(status_response)

            with patch("pathlib.Path.home", return_value=home):
                with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                    adapter = DevinAdapter()
                    report = adapter.probe_quota()
        self.assertIsNotNone(report)
        self.assertEqual(report.remaining_percent, 60.0)
        self.assertIn("daily 60%", report.detail)
        self.assertIn("weekly 80%", report.detail)
        self.assertIn("SeatManagementService/GetUserStatus", calls[0])

    def test_devin_get_user_status_hides_daily_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._write_credentials(home)
            status_response = {
                "userStatus": {
                    "planStatus": {
                        "planInfo": {"planName": "Max", "hideDailyQuota": True, "isDevin": True},
                        "dailyQuotaRemainingPercent": 73,
                        "weeklyQuotaRemainingPercent": 45,
                        "dailyQuotaResetAtUnix": "1780560000",
                        "weeklyQuotaResetAtUnix": "1780819200",
                    }
                }
            }

            def urlopen(request, timeout=None):
                return self._make_response(status_response)

            with patch("pathlib.Path.home", return_value=home):
                with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                    adapter = DevinAdapter()
                    report = adapter.probe_quota()
        self.assertIsNotNone(report)
        self.assertEqual(report.remaining_percent, 45.0)
        self.assertNotIn("daily", report.detail)
        self.assertIn("weekly 45%", report.detail)
        self.assertIsNotNone(report.extra)
        assert report.extra is not None
        self.assertNotIn("dailyPercent", report.extra)

    def test_devin_get_user_status_missing_percent_with_reset_is_exhausted(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._write_credentials(home)
            status_response = {
                "userStatus": {
                    "planStatus": {
                        "planInfo": {"planName": "Pro", "isDevin": True},
                        # proto3 omits zero-valued scalars: daily is fully used up.
                        "weeklyQuotaRemainingPercent": 60,
                        "dailyQuotaResetAtUnix": "1780560000",
                        "weeklyQuotaResetAtUnix": "1780819200",
                    }
                }
            }

            def urlopen(request, timeout=None):
                return self._make_response(status_response)

            with patch("pathlib.Path.home", return_value=home):
                with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                    adapter = DevinAdapter()
                    report = adapter.probe_quota()
        self.assertIsNotNone(report)
        self.assertEqual(report.remaining_percent, 0.0)
        self.assertIn("daily 0%", report.detail)
        self.assertIn("weekly 60%", report.detail)
        self.assertIsNotNone(report.extra)
        assert report.extra is not None
        self.assertEqual(report.extra["dailyPercent"], 0.0)

    def test_devin_get_user_status_no_windows_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._write_credentials(home)
            status_response = {
                "userStatus": {
                    "planStatus": {
                        "planInfo": {"planName": "Pro", "isDevin": True},
                    }
                }
            }

            def urlopen(request, timeout=None):
                return self._make_response(status_response)

            with patch("pathlib.Path.home", return_value=home):
                with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                    adapter = DevinAdapter()
                    report = adapter._get_user_status_quota()
        self.assertIsNone(report)

    def test_devin_get_user_status_non_quota_billing_strategy(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._write_credentials(home)
            status_response = {
                "userStatus": {
                    "planStatus": {
                        "planInfo": {
                            "planName": "Legacy",
                            "billingStrategy": "BILLING_STRATEGY_CREDITS",
                            "isDevin": True,
                        },
                        "dailyQuotaRemainingPercent": 50,
                        "weeklyQuotaRemainingPercent": 75,
                    }
                }
            }

            def urlopen(request, timeout=None):
                return self._make_response(status_response)

            with patch("pathlib.Path.home", return_value=home):
                with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                    adapter = DevinAdapter()
                    report = adapter._get_user_status_quota()
        self.assertIsNone(report)

    def test_devin_get_user_status_falls_back_when_no_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with patch("router.adapters.subscriptions.shutil.which", return_value=None):
                with patch("router.adapters.subscriptions.find_desktop_app", return_value=None):
                    with patch("pathlib.Path.home", return_value=home):
                        adapter = DevinAdapter()
                        report = adapter.probe_quota()
        self.assertEqual(report.state, QuotaState.UNKNOWN)
        self.assertIn("not installed", report.detail.lower())


class TestClaudeUsageQuota(unittest.TestCase):
    """Test Claude Code OAuth usage endpoint quota probing."""

    def _make_response(self, data: dict):
        response = Mock()
        response.read.return_value = json.dumps(data).encode("utf-8")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def _write_credentials(self, home: Path) -> None:
        creds_dir = home / ".claude"
        creds_dir.mkdir(parents=True)
        creds_dir.joinpath(".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "claude-oauth-token$test"}}),
            encoding="utf-8",
        )

    def test_claude_usage_parses_utilization(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._write_credentials(home)
            usage_response = {
                "five_hour": {"utilization": 35.0, "resets_at": "2026-01-20T18:00:00Z"},
                "seven_day": {"utilization": 12.0, "resets_at": "2026-01-27T00:00:00Z"},
                "seven_day_opus": {"utilization": 0.0, "resets_at": None},
                "extra_usage": {"is_enabled": True, "monthly_limit": 100.0, "used_credits": 12.5},
            }

            requests = []
            def urlopen(request, timeout=None):
                requests.append(request)
                return self._make_response(usage_response)

            with patch("pathlib.Path.home", return_value=home):
                with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                    adapter = ClaudeCodeAdapter()
                    report = adapter.probe_quota()
        self.assertIsNotNone(report)
        # utilization is % used; the bar reports % remaining (tightest window).
        self.assertEqual(report.remaining_percent, 65.0)
        self.assertIn("5h 65% remaining", report.detail)
        self.assertIn("7d 88% remaining", report.detail)
        self.assertIn("7d opus 100% remaining", report.detail)
        self.assertIn("$12.50/$100.00 extra usage", report.detail)
        self.assertIsNotNone(report.reset_at)
        self.assertIsNotNone(report.extra)
        assert report.extra is not None
        self.assertEqual(report.extra["dailyPercent"], 65.0)
        self.assertEqual(report.extra["weeklyPercent"], 88.0)
        self.assertEqual(report.extra["opusWeeklyPercent"], 100.0)
        self.assertEqual(report.extra["extraUsageUsd"], 12.5)
        meters = report.extra["meters"]
        self.assertIsInstance(meters, list)
        by_label = {m["label"]: m["windows"] for m in meters}
        claude = {w["kind"]: w for w in by_label["Claude"]}
        self.assertEqual(claude["daily"]["percent"], 65.0)
        self.assertEqual(claude["weekly"]["percent"], 88.0)
        self.assertIn("2026-01-27", claude["weekly"]["resetAt"])
        opus = {w["kind"]: w["percent"] for w in by_label["Opus"]}
        self.assertEqual(opus, {"weekly": 100.0})

        sent = requests[0]
        self.assertIn("api.anthropic.com/api/oauth/usage", sent.full_url)
        self.assertEqual(sent.get_header("Authorization"), "Bearer claude-oauth-token$test")
        self.assertEqual(sent.get_header("Anthropic-beta"), "oauth-2025-04-20")

    def test_claude_usage_skips_null_buckets(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._write_credentials(home)
            usage_response = {
                "five_hour": None,
                "seven_day": {"utilization": 40.0, "resets_at": "2026-01-27T00:00:00Z"},
                "seven_day_opus": None,
                "extra_usage": {"is_enabled": False},
            }

            def urlopen(request, timeout=None):
                return self._make_response(usage_response)

            with patch("pathlib.Path.home", return_value=home):
                with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                    adapter = ClaudeCodeAdapter()
                    report = adapter.probe_quota()
        self.assertIsNotNone(report)
        self.assertEqual(report.remaining_percent, 60.0)
        self.assertNotIn("5h", report.detail)
        self.assertIn("7d 60% remaining", report.detail)

    def test_claude_usage_falls_back_on_403(self):
        import email.message
        import urllib.error
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._write_credentials(home)

            def urlopen(request, timeout=None):
                raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", email.message.Message(), None)

            with patch("pathlib.Path.home", return_value=home):
                with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                    adapter = ClaudeCodeAdapter()
                    report = adapter._usage_quota()
        self.assertIsNone(report)

    def test_claude_usage_falls_back_without_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with patch("pathlib.Path.home", return_value=home):
                with patch.object(ClaudeCodeAdapter, "_claude_credentials",
                                  return_value=None):
                    adapter = ClaudeCodeAdapter()
                    report = adapter._usage_quota()
        self.assertIsNone(report)

    def test_claude_keychain_credentials(self):
        """macOS keychain blob (Claude Code-credentials) is honored."""
        blob = {"claudeAiOauth": {
            "accessToken": "sk-ant-oat01-test",
            "expiresAt": (time.time() + 3600) * 1000,
        }}
        def urlopen(request, timeout=None):
            return self._make_response({
                "five_hour": {"utilization": 20, "resets_at": "2026-09-27T18:00:00Z"},
            })
        with patch.object(ClaudeCodeAdapter, "_claude_credentials", return_value=blob):
            with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                adapter = ClaudeCodeAdapter()
                report = adapter._usage_quota()
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report.remaining_percent, 80.0)
        self.assertIn("5h 80% remaining", report.detail)

    def test_claude_expired_token_reports_guidance(self):
        blob = {"claudeAiOauth": {
            "accessToken": "sk-ant-oat01-test",
            "expiresAt": (time.time() - 3600) * 1000,
        }}
        with patch.object(ClaudeCodeAdapter, "_claude_credentials", return_value=blob):
            adapter = ClaudeCodeAdapter()
            self.assertEqual(adapter._credential_state(), "expired")
            report = adapter.probe_quota()
        self.assertEqual(report.state, QuotaState.UNKNOWN)
        self.assertIn("expired", report.detail)


class TestCursorUsageQuota(unittest.TestCase):
    """Test Cursor GetCurrentPeriodUsage quota probing."""

    def _make_response(self, data: dict):
        response = Mock()
        response.read.return_value = json.dumps(data).encode("utf-8")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def _make_jwt(self, exp: float) -> str:
        import base64
        def b64(obj: dict) -> str:
            raw = json.dumps(obj).encode("utf-8")
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        return f"{b64({'alg': 'none'})}.{b64({'exp': exp})}.sig"

    def _write_auth(self, home: Path, token: str) -> None:
        auth_dir = home / ".config" / "cursor"
        auth_dir.mkdir(parents=True)
        auth_dir.joinpath("auth.json").write_text(
            json.dumps({"accessToken": token}), encoding="utf-8"
        )

    def test_cursor_usage_parses_plan_usage(self):
        import time
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._write_auth(home, self._make_jwt(time.time() + 3600))
            usage_response = {
                "planUsage": {"totalPercentUsed": 42.0, "autoPercentUsed": 30.0, "apiPercentUsed": 12.0},
                "totalSpend": 1250,
                "billingCycleStart": "1756684800000",
                "billingCycleEnd": "1759276800000",
            }

            requests = []
            def urlopen(request, timeout=None):
                requests.append(request)
                return self._make_response(usage_response)

            with patch("pathlib.Path.home", return_value=home):
                with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                    adapter = CursorAdapter()
                    report = adapter.probe_quota()
        self.assertIsNotNone(report)
        # totalPercentUsed arrives as % used; the bar reports % remaining.
        self.assertEqual(report.remaining_percent, 58.0)
        self.assertIn("58% remaining", report.detail)
        self.assertIn("Cursor Models 70%", report.detail)
        self.assertIn("Other Models 88%", report.detail)
        self.assertIsNotNone(report.reset_at)
        self.assertIsNotNone(report.extra)
        assert report.extra is not None
        self.assertEqual(report.extra["weeklyPercent"], 58.0)
        self.assertEqual(report.extra["billingCycleEnd"], "2025-10-01T00:00:00+00:00")
        meters = report.extra["meters"]
        by_label = {m["label"]: m["windows"][0] for m in meters}
        self.assertEqual(by_label["Cursor Models"]["percent"], 70.0)
        self.assertEqual(by_label["Cursor Models"]["kind"], "monthly")
        self.assertEqual(by_label["Other Models"]["percent"], 88.0)
        self.assertIn("2025-10-01", by_label["Cursor Models"]["resetAt"])

        sent = requests[0]
        self.assertIn("GetCurrentPeriodUsage", sent.full_url)
        self.assertEqual(sent.get_header("Connect-protocol-version"), "1")

    def test_cursor_usage_teams_dollar_spend(self):
        import time
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._write_auth(home, self._make_jwt(time.time() + 3600))
            usage_response = {
                "planUsage": {},
                "totalSpend": 1250,
                "billingCycleEnd": "1759276800000",
            }

            def urlopen(request, timeout=None):
                return self._make_response(usage_response)

            with patch("pathlib.Path.home", return_value=home):
                with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                    adapter = CursorAdapter()
                    report = adapter.probe_quota()
        self.assertIsNotNone(report)
        self.assertIsNone(report.remaining_percent)
        self.assertIn("$12.50 spent", report.detail)

    def test_cursor_usage_expired_token_falls_back(self):
        import time
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._write_auth(home, self._make_jwt(time.time() - 3600))
            with patch("pathlib.Path.home", return_value=home):
                adapter = CursorAdapter()
                report = adapter._usage_quota()
        self.assertIsNone(report)

    def test_cursor_usage_falls_back_without_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with patch("pathlib.Path.home", return_value=home):
                adapter = CursorAdapter()
                report = adapter._usage_quota()
        self.assertIsNone(report)


class TestGeminiUsageQuota(unittest.TestCase):
    """Test Gemini (Antigravity) retrieveUserQuotaSummary probing."""

    def _make_response(self, data: dict):
        response = Mock()
        response.read.return_value = json.dumps(data).encode("utf-8")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def _write_token(self, home: Path) -> None:
        token_dir = home / ".gemini" / "antigravity-cli"
        token_dir.mkdir(parents=True)
        token_dir.joinpath("antigravity-oauth-token").write_text(
            json.dumps({"token": {"access_token": "antigravity-token$test"}}),
            encoding="utf-8",
        )

    def test_gemini_usage_parses_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._write_token(home)
            usage_response = {
                "groups": [
                    {"displayName": "Gemini Models", "buckets": [
                        {"bucketId": "gemini-5h", "window": "5h",
                         "resetTime": "2026-01-20T18:00:00Z", "remainingFraction": 0.85},
                        {"bucketId": "gemini-weekly", "window": "weekly",
                         "resetTime": "2026-01-27T00:00:00Z", "remainingFraction": 0.4},
                    ]},
                    {"displayName": "Claude and GPT models", "buckets": [
                        {"bucketId": "3p-5h", "window": "5h",
                         "resetTime": "2026-01-20T18:00:00Z", "remainingFraction": 0.6},
                        {"bucketId": "3p-weekly", "window": "weekly",
                         "resetTime": None, "remainingFraction": 0.3},
                    ]},
                ]
            }

            requests = []
            def urlopen(request, timeout=None):
                requests.append(request)
                return self._make_response(usage_response)

            with patch("pathlib.Path.home", return_value=home):
                with patch.dict("os.environ", {}, clear=True):
                    with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                        adapter = GeminiAdapter()
                        report = adapter.probe_quota()
        self.assertIsNotNone(report)
        # remainingFraction 0.3 -> 30% on the 3p pool; the adapter bar tracks
        # the Gemini pool only (min of its buckets = 40%).
        self.assertEqual(report.remaining_percent, 40.0)
        self.assertIn("Gemini Models 5h 85%", report.detail)
        self.assertIn("Gemini Models weekly 40%", report.detail)
        self.assertIn("Claude and GPT models weekly 30%", report.detail)
        self.assertIsNotNone(report.extra)
        assert report.extra is not None
        self.assertEqual(report.extra["dailyPercent"], 85.0)
        self.assertEqual(report.extra["weeklyPercent"], 40.0)
        self.assertEqual(report.extra["3p-weeklyPercent"], 30.0)
        meters = report.extra["meters"]
        self.assertIsInstance(meters, list)
        by_label = {m["label"]: {w["kind"]: w for w in m["windows"]}
                    for m in meters}
        self.assertEqual(set(by_label), {"Gemini Models", "Claude and GPT models"})
        self.assertEqual(by_label["Gemini Models"]["daily"]["percent"], 85.0)
        self.assertEqual(by_label["Gemini Models"]["weekly"]["percent"], 40.0)
        self.assertIn("2026-01-27", by_label["Gemini Models"]["weekly"]["resetAt"])
        self.assertEqual(by_label["Claude and GPT models"]["daily"]["percent"], 60.0)
        self.assertEqual(by_label["Claude and GPT models"]["weekly"]["percent"], 30.0)
        self.assertNotIn("resetAt", by_label["Claude and GPT models"]["weekly"])

        sent = requests[0]
        self.assertIn("retrieveUserQuotaSummary", sent.full_url)
        self.assertEqual(sent.get_header("Authorization"), "Bearer antigravity-token$test")
        self.assertEqual(sent.get_header("User-agent"), "antigravity")

    def _write_cli_oauth(self, home: Path, auth_type: str = "oauth-personal") -> None:
        import base64
        def b64(obj: dict) -> str:
            return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
        id_token = f"{b64({'alg': 'none'})}.{b64({'email': 'user@example.com', 'hd': 'example.com'})}.sig"
        gemini_dir = home / ".gemini"
        gemini_dir.mkdir(parents=True)
        gemini_dir.joinpath("settings.json").write_text(
            json.dumps({"security": {"auth": {"selectedType": auth_type}}}), encoding="utf-8")
        import time
        gemini_dir.joinpath("oauth_creds.json").write_text(
            json.dumps({
                "access_token": "gemini-cli-token$test",
                "expiry_date": int((time.time() + 3600) * 1000),
                "id_token": id_token,
            }), encoding="utf-8")

    def test_gemini_cli_surface_parses_quota(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._write_cli_oauth(home)
            responses = {
                "loadCodeAssist": {
                    "cloudaicompanionProject": "proj-123",
                    "currentTier": {"id": "free-tier"},
                },
                "retrieveUserQuota": {
                    "buckets": [
                        {"modelId": "gemini-2.5-pro", "remainingFraction": 0.7,
                         "resetTime": "2026-01-20T18:00:00Z"},
                        {"modelId": "gemini-2.5-flash", "remainingFraction": 0.9},
                    ]
                },
            }

            def urlopen(request, timeout=None):
                url = request.full_url
                for key, payload in responses.items():
                    if key in url:
                        return self._make_response(payload)
                raise AssertionError(f"unexpected URL {url}")

            with patch("pathlib.Path.home", return_value=home):
                with patch.dict("os.environ", {}, clear=True):
                    with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                        adapter = GeminiAdapter()
                        report = adapter.probe_quota()
        self.assertIsNotNone(report)
        self.assertEqual(report.remaining_percent, 70.0)
        self.assertIn("Gemini (gemini-cli):", report.detail)
        self.assertIn("gemini-2.5-pro 70%", report.detail)
        self.assertIsNotNone(report.extra)
        assert report.extra is not None
        self.assertEqual(report.extra["account"], "user@example.com")
        self.assertEqual(report.extra["tier"], "Workspace")  # free-tier + hd claim
        self.assertEqual(report.extra["cliProject"], "proj-123")
        self.assertEqual(report.extra["cli-gemini-2-5-proPercent"], 70.0)

    def test_gemini_cli_surface_skips_api_key_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._write_cli_oauth(home, auth_type="gemini-api-key")
            with patch("pathlib.Path.home", return_value=home):
                with patch.dict("os.environ", {}, clear=True):
                    adapter = GeminiAdapter()
                    self.assertIsNone(adapter._gemini_cli_quota())

    def test_gemini_usage_falls_back_on_license_wall(self):
        import email.message
        import urllib.error
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._write_token(home)

            def urlopen(request, timeout=None):
                raise urllib.error.HTTPError(
                    request.full_url, 403, "#3501: You do not have a valid license",
                    email.message.Message(), None)

            with patch("pathlib.Path.home", return_value=home):
                with patch.dict("os.environ", {}, clear=True):
                    with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                        adapter = GeminiAdapter()
                        report = adapter._usage_quota()
        self.assertIsNone(report)

    def test_gemini_usage_falls_back_without_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with patch("pathlib.Path.home", return_value=home):
                with patch.dict("os.environ", {}, clear=True):
                    with patch("router.adapters.subscriptions.credentials.get",
                               return_value=None):
                        adapter = GeminiAdapter()
                        report = adapter._usage_quota()
        self.assertIsNone(report)


class TestCodexUsageQuota(unittest.TestCase):
    """Test Codex wham/usage quota probing."""

    def _make_response(self, data: dict):
        response = Mock()
        response.read.return_value = json.dumps(data).encode("utf-8")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def _write_auth(self, home: Path) -> Path:
        codex_home = home / ".codex"
        codex_home.mkdir(parents=True)
        codex_home.joinpath("auth.json").write_text(
            json.dumps({"tokens": {
                "access_token": "codex-token$test",
                "account_id": "acct-123",
            }}), encoding="utf-8")
        return codex_home

    def test_codex_usage_parses_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            codex_home = self._write_auth(home)
            usage_response = {
                "plan_type": "plus",
                "rate_limit": {
                    "primary_window": {"used_percent": 1, "reset_at": 1781121633},
                    "secondary_window": {"used_percent": 28, "reset_at": 1781553834},
                },
                "additional_rate_limits": [
                    {"id": "codex-spark", "used_percent": 50, "reset_at": 1781121633},
                ],
            }

            requests = []
            def urlopen(request, timeout=None):
                requests.append(request)
                if "rate-limit-reset-credits" in request.full_url:
                    return self._make_response({"available": 2})
                return self._make_response(usage_response)

            with patch.dict("os.environ", {"CODEX_HOME": str(codex_home)}, clear=True):
                with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                    adapter = CodexAdapter()
                    report = adapter.probe_quota()
        self.assertIsNotNone(report)
        # used_percent -> remaining; bar uses tightest window (spark at 50% left).
        self.assertEqual(report.remaining_percent, 50.0)
        self.assertIn("plus", report.detail)
        self.assertIn("5h 99% remaining", report.detail)
        self.assertIn("weekly 72% remaining", report.detail)
        self.assertIn("codex-spark 50% remaining", report.detail)
        self.assertIsNotNone(report.extra)
        assert report.extra is not None
        self.assertEqual(report.extra["dailyPercent"], 99.0)
        self.assertEqual(report.extra["weeklyPercent"], 72.0)
        self.assertEqual(report.extra["resetCredits"], {"available": 2})

        sent = requests[0]
        self.assertIn("wham/usage", sent.full_url)
        self.assertEqual(sent.get_header("Chatgpt-account-id"), "acct-123")
        self.assertEqual(sent.get_header("Authorization"), "Bearer codex-token$test")

    def test_codex_usage_falls_back_on_401(self):
        import email.message
        import urllib.error
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            codex_home = self._write_auth(home)

            def urlopen(request, timeout=None):
                raise urllib.error.HTTPError(
                    request.full_url, 401, "Unauthorized", email.message.Message(), None)

            with patch.dict("os.environ", {"CODEX_HOME": str(codex_home)}, clear=True):
                with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                    adapter = CodexAdapter()
                    report = adapter._usage_quota()
        self.assertIsNone(report)

    def test_codex_usage_falls_back_without_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with patch.dict("os.environ", {"CODEX_HOME": str(home / ".codex")}, clear=True):
                with patch("pathlib.Path.home", return_value=home):
                    adapter = CodexAdapter()
                    report = adapter._usage_quota()
        self.assertIsNone(report)

    def test_codex_app_server_offline_fallback(self):
        """HTTP unreachable -> local JSON-RPC account/rateLimits/read."""
        import io
        import urllib.error
        rpc_lines = (
            json.dumps({"id": 0, "result": {}}) + "\n"
            + json.dumps({"id": 1, "result": {
                "rateLimits": {
                    "primaryWindow": {"usedPercent": 10, "resetAt": 1781121633},
                    "secondaryWindow": {"usedPercent": 70, "resetAt": 1781553834},
                },
                "planType": "pro",
            }}) + "\n"
        )
        fake_proc = Mock()
        fake_proc.stdin = io.StringIO()
        fake_proc.stdout = io.StringIO(rpc_lines)

        def urlopen(request, timeout=None):
            raise urllib.error.URLError("offline")

        def fake_select(r, w, x, timeout=None):
            return (r, [], [])

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            codex_home = self._write_auth(home)
            with patch.dict("os.environ", {"CODEX_HOME": str(codex_home)}, clear=True):
                with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                    with patch("router.adapters.subscriptions.subprocess.Popen", return_value=fake_proc):
                        with patch("router.adapters.subscriptions.select.select", side_effect=fake_select):
                            adapter = CodexAdapter()
                            adapter._cli_path = "/usr/bin/codex"
                            report = adapter._usage_quota()
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report.remaining_percent, 30.0)  # weekly is tightest
        self.assertIn("app-server", report.detail)
        self.assertIn("pro", report.detail)
        self.assertIn("5h 90% remaining", report.detail)
        self.assertIn("weekly 30% remaining", report.detail)


class TestCopilotUsageQuota(unittest.TestCase):
    """Test GitHub Copilot copilot_internal/user quota probing."""

    def _make_response(self, data: dict):
        response = Mock()
        response.read.return_value = json.dumps(data).encode("utf-8")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def _probe(self, payload: dict):
        requests = []
        def urlopen(request, timeout=None):
            requests.append(request)
            return self._make_response(payload)
        with patch.object(CopilotAdapter, "_github_token", return_value="gho_test$token"):
            with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                adapter = CopilotAdapter()
                return adapter._usage_quota(), requests

    def test_copilot_usage_parses_quota_snapshots(self):
        report, requests = self._probe({
            "copilot_plan": "business",
            "quota_reset_date_utc": "2026-09-01T00:00:00.000Z",
            "quota_snapshots": {
                "premium_interactions": {"entitlement": 3500, "remaining": 558,
                                         "percent_remaining": 15.9, "unlimited": False,
                                         "has_quota": True},
                "chat": {"unlimited": True, "entitlement": 0},
                "completions": {"unlimited": True, "entitlement": 0},
            },
        })
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report.remaining_percent, 16.0)
        self.assertIn("business", report.detail)
        self.assertIn("premium_interactions 16% (558/3500)", report.detail)
        self.assertNotIn("chat", report.detail)  # unlimited entries are skipped
        self.assertIsNotNone(report.reset_at)
        self.assertIsNotNone(report.extra)
        assert report.extra is not None
        self.assertEqual(report.extra["weeklyPercent"], 16.0)

        sent = requests[0]
        self.assertIn("copilot_internal/user", sent.full_url)
        self.assertEqual(sent.get_header("Authorization"), "token gho_test$token")
        self.assertEqual(sent.get_header("X-github-api-version"), "2025-04-01")

    def test_copilot_usage_free_tier_shape(self):
        report, _ = self._probe({
            "access_type_sku": "free_limited_copilot",
            "limited_user_reset_date": "2026-10-01T00:00:00.000Z",
            "monthly_quotas": {"chat": 50, "completions": 2000},
            "limited_user_quotas": {"chat": 10, "completions": 1500},
        })
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report.remaining_percent, 20.0)  # chat is tightest
        self.assertIn("chat 20% (10/50)", report.detail)
        self.assertIn("completions 75% (1500/2000)", report.detail)

    def test_copilot_usage_changed_shape_gives_no_reading(self):
        report, _ = self._probe({
            "copilot_plan": "pro",
            "quota_snapshots": {
                # percent_remaining missing: changed shape -> no reading, not a guess.
                "premium_interactions": {"entitlement": 300, "unlimited": False},
            },
        })
        self.assertIsNone(report)

    def test_copilot_usage_falls_back_without_token(self):
        with patch.object(CopilotAdapter, "_github_token", return_value=None):
            adapter = CopilotAdapter()
            self.assertIsNone(adapter._usage_quota())

    def _probe_routed(self, routes: dict):
        """urlopen mock that routes by URL substring; unmatched URLs 404."""
        import email.message
        import urllib.error
        requests = []
        def urlopen(request, timeout=None):
            url = request.full_url
            requests.append(request)
            # Longest key first: "/users/x/..." would otherwise match "user".
            for key in sorted(routes, key=len, reverse=True):
                if key in url:
                    return self._make_response(routes[key])
            raise urllib.error.HTTPError(url, 404, "Not Found", email.message.Message(), None)
        with patch.object(CopilotAdapter, "_github_token", return_value="gho_test$token"):
            with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                adapter = CopilotAdapter()
                return adapter._usage_quota(), requests

    def test_copilot_v2_token_fallback(self):
        report, requests = self._probe_routed({
            "copilot_internal/user": {"copilot_plan": "pro"},  # no snapshots
            "copilot_internal/v2/token": {
                "access_type_sku": "free_limited_copilot",
                "limited_user_reset_date": "2026-10-01T00:00:00.000Z",
                "monthly_quotas": {"chat": 50},
                "limited_user_quotas": {"chat": 25},
            },
        })
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report.remaining_percent, 50.0)
        self.assertIn("chat 50% (25/50)", report.detail)
        urls = [r.full_url for r in requests]
        self.assertTrue(any("v2/token" in u for u in urls))

    def test_copilot_personal_billing_usage(self):
        report, _ = self._probe_routed({
            "copilot_internal/user": {
                "copilot_plan": "pro",
                "quota_snapshots": {
                    "premium_interactions": {"entitlement": 300, "remaining": 150,
                                             "percent_remaining": 50.0, "unlimited": False},
                },
            },
            "api.github.com/user": {"login": "octocat"},
            "/users/octocat/settings/billing/premium_request/usage": {
                "usageItems": [{"grossAmount": 4.2}, {"grossAmount": 1.8}]},
            "/users/octocat/settings/billing/ai_credit/usage": {},
        })
        self.assertIsNotNone(report)
        assert report is not None
        assert report.extra is not None
        self.assertEqual(report.extra["premiumRequestUsageUsd"], 6.0)
        self.assertIn("$6.00 premium requests", report.detail)
        self.assertNotIn("org", report.detail)

    def test_copilot_org_paid_seat(self):
        report, _ = self._probe_routed({
            "copilot_internal/user": {
                "copilot_plan": "business",
                "quota_snapshots": {
                    "premium_interactions": {"entitlement": 3500, "remaining": 558,
                                             "percent_remaining": 15.9, "unlimited": False},
                },
            },
            "api.github.com/user/orgs": [{"login": "acme"}],
            "api.github.com/user": {"login": "octocat"},
            # personal endpoints empty -> org-paid seat
            "organizations/acme/settings/billing/premium_request/usage": {
                "totalAmount": 120.5},
        })
        self.assertIsNotNone(report)
        assert report is not None
        assert report.extra is not None
        self.assertEqual(report.extra["premiumRequestUsageUsd"], 120.5)
        self.assertEqual(report.extra["billedTo"], "acme")
        self.assertIn("(org acme)", report.detail)


class TestPerplexityUsageQuota(unittest.TestCase):
    """Test Perplexity web-session rate-limit probing."""

    def _make_response(self, data: dict):
        response = Mock()
        response.read.return_value = json.dumps(data).encode("utf-8")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def test_perplexity_usage_reports_exact_counts(self):
        usage_response = {
            "free_queries": {"available": True,
                             "remaining_detail": {"kind": "exact", "remaining": 10}},
            "remaining_pro": 2,
            "remaining_research": 0,
            "remaining_agentic_research": 0,
            "remaining_labs": 0,
            "model_specific_limits": {"sonar-reasoning-pro": 5},
        }

        requests = []
        def urlopen(request, timeout=None):
            requests.append(request)
            return self._make_response(usage_response)

        with patch.dict("os.environ", {"PERPLEXITY_SESSION_COOKIE": "sess$test"}, clear=True):
            with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                adapter = PerplexityAdapter()
                report = adapter._usage_quota()
        self.assertIsNotNone(report)
        assert report is not None
        # Remaining-only API: exact integers, never a synthesized percentage.
        self.assertIsNone(report.remaining_percent)
        self.assertIn("10 free", report.detail)
        self.assertIn("2 Pro", report.detail)
        self.assertIn("0 Research", report.detail)
        self.assertIn("5 sonar-reasoning-pro", report.detail)
        self.assertEqual(report.state, QuotaState.OK)

        sent = requests[0]
        self.assertIn("perplexity.ai/rest/rate-limit/all", sent.full_url)
        self.assertEqual(sent.get_header("Cookie"),
                         "__Secure-next-auth.session-token=sess$test")

    def test_perplexity_all_zero_is_depleted(self):
        usage_response = {
            "free_queries": {"available": True,
                             "remaining_detail": {"kind": "exact", "remaining": 0}},
            "remaining_pro": 0,
            "remaining_research": 0,
        }
        def urlopen(request, timeout=None):
            return self._make_response(usage_response)

        with patch.dict("os.environ", {"PERPLEXITY_SESSION_COOKIE": "sess$test"}, clear=True):
            with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                adapter = PerplexityAdapter()
                report = adapter._usage_quota()
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report.state, QuotaState.DEPLETED)

    def test_perplexity_pro_payload_model_limits(self):
        """Pro-shaped payloads: dynamic remaining_* keys, model limits with
        denominators become monthly meters, connector limits land in extra."""
        usage_response = {
            "free_queries": {"available": True,
                             "remaining_detail": {"kind": "exact", "remaining": 10}},
            "remaining_pro": 298,
            "remaining_research": 20,
            "remaining_agentic_research": 7,
            "remaining_labs": 4,
            "remaining_comet_plus": 12,
            "model_specific_limits": {
                "sonar-reasoning-pro": {"remaining": 40, "monthly_limit": 50},
                "gpt-5.2": 3,
            },
            "sources": {"source_to_limit": {
                "slack_direct": {"monthly_limit": 5, "remaining": 5},
                "web": {"monthly_limit": None, "remaining": None},
                "github_mcp_direct": {"monthly_limit": 0, "remaining": 0},
            }},
        }
        def urlopen(request, timeout=None):
            return self._make_response(usage_response)

        with patch.dict("os.environ", {"PERPLEXITY_SESSION_COOKIE": "sess$test"}, clear=True):
            with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                adapter = PerplexityAdapter()
                report = adapter._usage_quota()
        self.assertIsNotNone(report)
        assert report is not None
        self.assertIn("298 Pro", report.detail)
        self.assertIn("12 Comet Plus", report.detail)
        self.assertIn("40 sonar-reasoning-pro", report.detail)
        extra = report.extra
        assert extra is not None
        meters = {m["label"]: m for m in extra["meters"]}
        self.assertEqual(
            meters["sonar-reasoning-pro"]["windows"][0]["percent"], 80.0)
        self.assertEqual(
            meters["sonar-reasoning-pro"]["windows"][0]["kind"], "monthly")
        self.assertNotIn("gpt-5.2", meters)  # no denominator → count only
        self.assertEqual(extra["sourceLimits"]["slack_direct"]["remaining"], 5)
        self.assertNotIn("web", extra["sourceLimits"])

    def test_perplexity_401_prompts_for_fresh_cookie(self):
        import email.message
        import urllib.error
        def urlopen(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 401, "Unauthorized", email.message.Message(), None)

        with patch.dict("os.environ", {"PERPLEXITY_SESSION_COOKIE": "stale"}, clear=True):
            with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                adapter = PerplexityAdapter()
                report = adapter._usage_quota()
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report.state, QuotaState.UNKNOWN)
        self.assertIn("fresh cookie", report.detail)

    def test_perplexity_falls_back_without_cookie(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.toml"
            with patch.dict("os.environ", {"ROUTER_CONFIG": str(cfg)}, clear=True):
                adapter = PerplexityAdapter()
                self.assertIsNone(adapter._usage_quota())

    def test_perplexity_api_credits_extra(self):
        """console.perplexity.ai pplx-api RPCs attach an apiCredits extra."""
        usage_response = {"remaining_pro": 2}
        routes = {
            "rest/rate-limit/all": usage_response,
            "pplx-api/v2/groups": [{"id": "org-1", "name": "Acme"}],
            "pplx-api/v2/groups/org-1/usage": {"total": 14.5},
            "pplx-api/v2/groups/org-1": {"tier": "tier-2", "balance": 40.0},
        }
        def urlopen(request, timeout=None):
            for key in sorted(routes, key=len, reverse=True):
                if key in request.full_url:
                    return self._make_response(routes[key])
            raise OSError(f"unmatched {request.full_url}")

        with patch.dict("os.environ", {"PERPLEXITY_SESSION_COOKIE": "sess$test"}, clear=True):
            with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                adapter = PerplexityAdapter()
                report = adapter._usage_quota()
        self.assertIsNotNone(report)
        assert report is not None
        assert report.extra is not None
        credits = report.extra.get("apiCredits")
        self.assertIsNotNone(credits)
        self.assertEqual(credits["org"], "org-1")
        self.assertEqual(credits["tier"], "tier-2")
        self.assertEqual(credits["balanceUsd"], 40.0)
        self.assertEqual(credits["spendUsd"], 14.5)


class TestZedUsageQuota(unittest.TestCase):
    """Test Zed cloud billing usage probing."""

    def _make_response(self, data: dict):
        response = Mock()
        response.read.return_value = json.dumps(data).encode("utf-8")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def _probe(self, usage: dict, subscription: dict | None = None):
        requests = []
        def urlopen(request, timeout=None):
            requests.append(request)
            if "subscriptions/current" in request.full_url:
                if subscription is None:
                    raise OSError("subscription endpoint down")
                return self._make_response(subscription)
            return self._make_response(usage)

        with patch.dict("os.environ", {"ZED_SESSION_COOKIE": "zed-session$test"}, clear=True):
            with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                adapter = ZedAdapter()
                return adapter._usage_quota(), requests

    def test_zed_usage_parses_both_meters(self):
        report, requests = self._probe(
            {"token_spend": {"usage": 3.2, "limit": 5.0},
             "edit_predictions": {"usage": 1200, "limit": 2000}},
            {"plan": "trial", "period_end": "2026-10-10T00:00:00Z"},
        )
        self.assertIsNotNone(report)
        assert report is not None
        # Token Spend: (5-3.2)/5 = 36%; Edit Predictions: (2000-1200)/2000 = 40%.
        self.assertEqual(report.remaining_percent, 36.0)
        self.assertIn("[trial]", report.detail)
        self.assertIn("Token Spend 36%", report.detail)
        self.assertIn("Edit Predictions 40%", report.detail)
        self.assertIsNotNone(report.extra)
        assert report.extra is not None
        self.assertEqual(report.extra["tokenSpendPercent"], 36.0)
        self.assertEqual(report.extra["editPredictionsPercent"], 40.0)
        self.assertEqual(report.extra["weeklyResetAt"], "2026-10-10T00:00:00+00:00")
        self.assertEqual(requests[0].get_header("Cookie"), "zed.session=zed-session$test")

    def test_zed_unlimited_limit_renders_without_meter(self):
        report, _ = self._probe({
            "token_spend": {"usage": 0.0, "limit": None},
            "edit_predictions": {"usage": 1800, "limit": 2000},
        })
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report.remaining_percent, 10.0)
        self.assertIn("Token Spend unlimited", report.detail)
        self.assertIn("Edit Predictions 10%", report.detail)

    def test_zed_subscription_failure_does_not_hide_usage(self):
        report, _ = self._probe(
            {"edit_predictions": {"usage": 100, "limit": 2000}},
            subscription=None,
        )
        self.assertIsNotNone(report)
        assert report is not None
        self.assertIn("Edit Predictions 95%", report.detail)
        self.assertIsNone(report.reset_at)

    def test_zed_malformed_meter_omitted(self):
        report, _ = self._probe({
            "token_spend": {"usage": "lots", "limit": 5.0},
        })
        self.assertIsNone(report)

    def test_zed_falls_back_without_cookie(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.toml"
            with patch.dict("os.environ", {"ROUTER_CONFIG": str(cfg)}, clear=True):
                with patch.object(ZedAdapter, "_keychain_editor_auth", return_value=None):
                    adapter = ZedAdapter()
                    self.assertIsNone(adapter._usage_quota())

    def test_zed_editor_credential_fallback(self):
        """No session cookie -> /client/users/me with the editor credential."""
        requests = []
        def urlopen(request, timeout=None):
            requests.append(request)
            return self._make_response({
                "plan": "personal",
                "edit_predictions": {"used": 500, "limit": 2000},
            })

        with patch.dict("os.environ", {"ZED_EDITOR_TOKEN": "editor-tok$test"}, clear=True):
            with patch.object(ZedAdapter, "_session_cookie", return_value=None):
                with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                    adapter = ZedAdapter()
                    report = adapter._usage_quota()
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report.remaining_percent, 75.0)
        self.assertIn("[personal]", report.detail)
        self.assertIn("Edit Predictions 75%", report.detail)
        self.assertIn("token spend not exposed", report.detail)
        sent = requests[0]
        self.assertIn("/client/users/me", sent.full_url)
        self.assertEqual(sent.get_header("Authorization"), "Bearer editor-tok$test")


    def test_zed_run_hands_off_to_editor(self):
        """run() opens the prompt in Zed rather than crashing."""
        launched = []
        def popen(cmd, **kw):
            launched.append(cmd)
            return Mock()
        with patch.object(ZedAdapter, "is_available", return_value=True):
            adapter = ZedAdapter()
            adapter._cli_path = "/usr/local/bin/zed"
            with patch("router.adapters.subscriptions.subprocess.Popen", side_effect=popen):
                result = adapter.run(RunRequest(prompt="fix the bug"))
        self.assertIn("opened in Zed", result.output)
        self.assertTrue(launched[0][0].endswith("zed"))
        self.assertIn("fix the bug", Path(launched[0][1]).read_text(encoding="utf-8"))
        Path(launched[0][1]).unlink(missing_ok=True)

    def test_zed_cookie_prefers_keyring(self):
        """Keyring beats config.toml but env wins over both."""
        with patch.dict("os.environ", {}, clear=True):
            with patch("router.adapters.subscriptions.credentials.get",
                       return_value="ring-cookie"):
                with patch("router.adapters.subscriptions._router_config_value",
                           return_value="toml-cookie"):
                    adapter = ZedAdapter()
                    self.assertEqual(adapter._session_cookie(), "ring-cookie")
        with patch.dict("os.environ", {"ZED_SESSION_COOKIE": "env-cookie"}, clear=True):
            with patch("router.adapters.subscriptions.credentials.get",
                       return_value="ring-cookie"):
                adapter = ZedAdapter()
                self.assertEqual(adapter._session_cookie(), "env-cookie")

    def test_perplexity_cookie_normalization(self):
        """Bare tokens wrap in the NextAuth name; pplx_session appends."""
        with patch.dict("os.environ", {}, clear=True):
            with patch("router.adapters.subscriptions.credentials.get",
                       side_effect=lambda s, a: "tok123" if a == "session_cookie" else None):
                adapter = PerplexityAdapter()
                self.assertEqual(
                    adapter._session_cookie(),
                    "__Secure-next-auth.session-token=tok123")
        # A pasted name=value pair or full header passes through verbatim.
        with patch.dict(
                "os.environ",
                {"PERPLEXITY_SESSION_COOKIE": "a=b; c=d",
                 "PERPLEXITY_PPLX_SESSION": "__Secure-pplx.session.xyz=v2"},
                clear=True):
            adapter = PerplexityAdapter()
            self.assertEqual(
                adapter._session_cookie(),
                "a=b; c=d; __Secure-pplx.session.xyz=v2")


class TestZcodeUsageQuota(unittest.TestCase):
    """Test Z.ai ZCode monitor endpoint probing."""

    def _make_response(self, data: dict):
        response = Mock()
        response.read.return_value = json.dumps(data).encode("utf-8")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def test_zcode_usage_parses_rolling_window(self):
        responses = {
            "quota/limit": {"success": True, "data": {
                "percentage": 40.0, "subscription": "GLM Coding Plan Pro",
                "nextResetTime": "2026-10-01T00:00:00Z",
            }},
            "credit_grants": {"data": {"grants": [
                {"amount": 100, "expiry": "2026-11-01T00:00:00Z"},
                {"amount": 50},
            ]}},
            "model-usage": {"data": {"items": [
                {"model": "glm-4.6", "requests": 12, "tokens": 45000}]}},
            "tool-usage": {"data": {"items": [
                {"tool": "web_search", "invocations": 3}]}},
        }
        requests = []
        def urlopen(request, timeout=None):
            requests.append(request)
            for key in sorted(responses, key=len, reverse=True):
                if key in request.full_url:
                    return self._make_response(responses[key])
            raise AssertionError(f"unexpected URL {request.full_url}")

        with patch.dict("os.environ", {"ZAI_API_KEY": "zai-key$test"}, clear=True):
            with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                adapter = ZcodeAdapter()
                report = adapter._usage_quota()
        self.assertIsNotNone(report)
        assert report is not None
        # usage % arrives as used; the bar reports remaining.
        self.assertEqual(report.remaining_percent, 60.0)
        self.assertIn("5h rolling 60% remaining", report.detail)
        self.assertIn("GLM Coding Plan Pro", report.detail)
        self.assertIn("2 credit grants", report.detail)
        self.assertIsNotNone(report.extra)
        assert report.extra is not None
        self.assertEqual(report.extra["dailyPercent"], 60.0)
        self.assertEqual(report.extra["creditGrants"], 2)
        self.assertEqual(report.extra["earliestGrantExpiry"], "2026-11-01T00:00:00+00:00")
        # Analytics feeds land in extras, not in the detail line.
        self.assertEqual(len(report.extra["modelUsage"]), 1)
        self.assertEqual(len(report.extra["toolUsage"]), 1)
        self.assertEqual(
            requests[0].get_header("Authorization"), "Bearer zai-key$test")
        self.assertIn("api.z.ai/api/monitor/usage/quota/limit", requests[0].full_url)

    def test_zcode_missing_envelope_still_parses(self):
        def urlopen(request, timeout=None):
            if "quota/limit" in request.full_url:
                return self._make_response({"percentage": 100.0})
            raise OSError("credit grants unreachable")

        with patch.dict("os.environ", {"ZHIPUAI_API_KEY": "k"}, clear=True):
            with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                adapter = ZcodeAdapter()
                report = adapter._usage_quota()
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report.state, QuotaState.DEPLETED)
        self.assertEqual(report.remaining_percent, 0.0)

    def test_zcode_falls_back_without_key(self):
        with patch.dict("os.environ", {}, clear=True):
            adapter = ZcodeAdapter()
            self.assertIsNone(adapter._usage_quota())
            self.assertFalse(adapter.is_available())

    def test_zcode_run_uses_coding_api(self):
        """run() dispatches through the GLM coding plan API directly."""
        requests = []
        def urlopen(request, timeout=None):
            requests.append(request)
            return self._make_response({
                "model": "glm-4.6",
                "choices": [{"message": {"role": "assistant", "content": "hello back"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3},
            })

        with patch.dict("os.environ", {"ZAI_API_KEY": "zai-key$test"}, clear=True):
            with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                adapter = ZcodeAdapter()
                result = adapter.run(RunRequest(prompt="say hi", timeout_s=10))
        self.assertEqual(result.output, "hello back")
        self.assertEqual(result.input_tokens, 10)
        self.assertEqual(result.output_tokens, 3)
        self.assertEqual(result.cost_usd, 0.0)
        self.assertFalse(result.depleted_mid_run)
        sent = requests[0]
        self.assertIn("/api/coding/paas/v4/chat/completions", sent.full_url)
        self.assertEqual(sent.get_header("Authorization"), "Bearer zai-key$test")


class TestOpenRouterUsageQuota(unittest.TestCase):
    """Test OpenRouter /api/v1/key quota probing."""

    def _make_response(self, data: dict):
        response = Mock()
        response.read.return_value = json.dumps(data).encode("utf-8")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def test_openrouter_key_reports_remaining_limit(self):
        payload = {"data": {
            "limit_remaining": 8.5, "limit": 20.0, "limit_reset": "weekly",
            "usage": 42.0, "usage_daily": 1.5, "is_free_tier": False,
        }}

        requests = []
        def urlopen(request, timeout=None):
            requests.append(request)
            return self._make_response(payload)

        from router.adapters.openrouter import OpenRouterAdapter
        with patch("router.adapters.openrouter.urllib.request.urlopen", side_effect=urlopen):
            adapter = OpenRouterAdapter(api_key="or-key$test")
            report = adapter.probe_quota()
        self.assertEqual(report.state, QuotaState.OK)
        self.assertEqual(report.remaining_percent, 42.0)
        self.assertIn("$8.50 of $20.00 limit left", report.detail)
        self.assertIn("resets weekly", report.detail)
        self.assertIn("$1.50 today", report.detail)
        self.assertIsNotNone(report.extra)
        assert report.extra is not None
        self.assertEqual(report.extra["weeklyPercent"], 42.0)
        self.assertEqual(
            requests[0].get_header("Authorization"), "Bearer or-key$test")

    def test_openrouter_unlimited_key(self):
        payload = {"data": {"limit_remaining": None, "limit": None,
                            "limit_reset": None, "usage": 3.0}}
        def urlopen(request, timeout=None):
            return self._make_response(payload)

        from router.adapters.openrouter import OpenRouterAdapter
        with patch("router.adapters.openrouter.urllib.request.urlopen", side_effect=urlopen):
            adapter = OpenRouterAdapter(api_key="or-key$test")
            report = adapter.probe_quota()
        self.assertEqual(report.state, QuotaState.OK)
        self.assertIsNone(report.remaining_percent)
        self.assertIn("unlimited credits", report.detail)

    def test_openrouter_zero_remaining_is_depleted(self):
        payload = {"data": {"limit_remaining": 0, "limit": 10.0}}
        def urlopen(request, timeout=None):
            return self._make_response(payload)

        from router.adapters.openrouter import OpenRouterAdapter
        with patch("router.adapters.openrouter.urllib.request.urlopen", side_effect=urlopen):
            adapter = OpenRouterAdapter(api_key="or-key$test")
            report = adapter.probe_quota()
        self.assertEqual(report.state, QuotaState.DEPLETED)
        self.assertEqual(report.remaining_percent, 0.0)

    def test_openrouter_no_key_keeps_metered_detail(self):
        with patch.dict("os.environ", {}, clear=True):
            with patch("router.adapters.openrouter.get_secret", return_value=None):
                from router.adapters.openrouter import OpenRouterAdapter
                adapter = OpenRouterAdapter()
                report = adapter.probe_quota()
        self.assertEqual(report.state, QuotaState.OK)
        self.assertIn("pay-per-use", report.detail)


class TestDeepSeekUsageQuota(unittest.TestCase):
    """Test DeepSeek balance endpoint probing."""

    def _make_response(self, data: dict):
        response = Mock()
        response.read.return_value = json.dumps(data).encode("utf-8")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def test_deepseek_reports_balances(self):
        payload = {"balance_infos": [
            {"currency": "USD", "total_balance": "12.34",
             "granted_balance": "5.00", "topped_up_balance": "7.34"},
            {"currency": "CNY", "total_balance": "0.50"},
        ]}
        requests = []
        def urlopen(request, timeout=None):
            requests.append(request)
            return self._make_response(payload)

        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "ds-key$test"}, clear=True):
            with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                adapter = DeepSeekAdapter()
                report = adapter._usage_quota()
        self.assertIsNotNone(report)
        assert report is not None
        self.assertIsNone(report.remaining_percent)  # balance has no denominator
        self.assertIn("12.34 USD", report.detail)
        self.assertIn("0.50 CNY", report.detail)
        self.assertEqual(report.state, QuotaState.OK)
        assert report.extra is not None
        self.assertEqual(report.extra["balances"][0]["granted"], 5.0)
        self.assertEqual(
            requests[0].get_header("Authorization"), "Bearer ds-key$test")

    def test_deepseek_zero_balance_is_depleted(self):
        payload = {"balance_infos": [{"currency": "USD", "total_balance": "0.00"}]}
        def urlopen(request, timeout=None):
            return self._make_response(payload)

        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "ds-key$test"}, clear=True):
            with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                adapter = DeepSeekAdapter()
                report = adapter._usage_quota()
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report.state, QuotaState.DEPLETED)

    def test_deepseek_falls_back_without_key(self):
        with patch.dict("os.environ", {}, clear=True):
            adapter = DeepSeekAdapter()
            self.assertIsNone(adapter._usage_quota())
            self.assertFalse(adapter.is_available())


class TestXAIUsageQuota(unittest.TestCase):
    """Test xAI/Grok CLI-proxy quota probing."""

    def _make_response(self, data: dict):
        response = Mock()
        response.read.return_value = json.dumps(data).encode("utf-8")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def _write_auth(self, home: Path) -> None:
        import base64
        def b64(obj: dict) -> str:
            return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
        token = f"{b64({'alg': 'none'})}.{b64({'sub': 'user-42'})}.sig"
        grok_dir = home / ".grok"
        grok_dir.mkdir(parents=True)
        grok_dir.joinpath("auth.json").write_text(
            json.dumps({"tokens": {"https://auth.x.ai::app.key": {"access_token": token}}}),
            encoding="utf-8")

    def _probe(self, billing: dict, settings: dict | None = None):
        requests = []
        def urlopen(request, timeout=None):
            requests.append(request)
            if "/v1/settings" in request.full_url:
                if settings is None:
                    raise OSError("settings unreachable")
                return self._make_response(settings)
            return self._make_response(billing)
        return requests, urlopen

    def test_grok_parses_credit_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._write_auth(home)
            requests, urlopen = self._probe(
                {"config": {
                    "creditUsagePercent": 30.0,
                    "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY",
                                      "start": "2026-09-20T00:00:00Z",
                                      "end": "2026-09-27T00:00:00Z"},
                    "monthlyLimit": {"val": 50.0},
                    "used": {"val": 15.0},
                    "prepaid_balance": {"val": 8.0},
                }},
                {"subscription_tier_display": "SuperGrok"},
            )
            with patch("pathlib.Path.home", return_value=home):
                with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                    adapter = XAIAdapter()
                    report = adapter._usage_quota()
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report.remaining_percent, 70.0)  # used% -> remaining
        self.assertIn("weekly 70% remaining", report.detail)
        self.assertIn("[SuperGrok]", report.detail)
        self.assertIn("$15/$50 monthly", report.detail)
        self.assertIn("$8 prepaid", report.detail)
        assert report.extra is not None
        self.assertEqual(report.extra["weeklyPercent"], 70.0)
        self.assertEqual(report.extra["prepaidBalanceUsd"], 8.0)
        sent = requests[0]
        self.assertIn("billing?format=credits", sent.full_url)
        self.assertEqual(sent.get_header("X-xai-token-auth"), "xai-grok-cli")
        self.assertEqual(sent.get_header("X-userid"), "user-42")

    def test_grok_on_demand_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._write_auth(home)
            _, urlopen = self._probe({"config": {
                "onDemandUsed": {"val": 6.0}, "onDemandCap": {"val": 10.0},
            }})
            with patch("pathlib.Path.home", return_value=home):
                with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                    adapter = XAIAdapter()
                    report = adapter._usage_quota()
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report.remaining_percent, 40.0)

    def test_grok_absent_percent_stays_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._write_auth(home)
            _, urlopen = self._probe({"config": {"prepaid_balance": {"val": 3.0}}})
            with patch("pathlib.Path.home", return_value=home):
                with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                    adapter = XAIAdapter()
                    report = adapter._usage_quota()
        self.assertIsNotNone(report)
        assert report is not None
        self.assertIsNone(report.remaining_percent)
        self.assertIn("$3 prepaid", report.detail)

    def test_grok_falls_back_without_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with patch("pathlib.Path.home", return_value=home):
                with patch.dict("os.environ", {}, clear=True):
                    adapter = XAIAdapter()
                    self.assertIsNone(adapter._usage_quota())


class TestAmpUsageQuota(unittest.TestCase):
    """Test Amp `amp usage` parsing and API-key fallback."""

    def _make_response(self, data: dict):
        response = Mock()
        response.read.return_value = json.dumps(data).encode("utf-8")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def test_amp_cli_usage_amount_form(self):
        text = ("Amp usage\n"
                "  agent usage: $12.34 of $50.00 this month\n"
                "  credit balance: $5.00\n")
        with patch.object(AmpAdapter, "_cli_usage_text", return_value=text):
            with patch.dict("os.environ", {}, clear=True):
                adapter = AmpAdapter()
                report = adapter._usage_quota()
        self.assertIsNotNone(report)
        assert report is not None
        assert report.remaining_percent is not None
        self.assertAlmostEqual(report.remaining_percent, 25.0, places=2)
        self.assertIn("$12.34/$50", report.detail)
        self.assertIn("$5 credits", report.detail)
        assert report.extra is not None
        self.assertAlmostEqual(report.extra["agentPercent"], 25.0, places=2)

    def test_amp_api_key_fallback(self):
        payload = {"balances": [
            {"name": "Agent usage", "remaining": 30.0, "limit": 50.0},
            {"name": "Workspace credits", "balance": 7.5},
        ]}
        def urlopen(request, timeout=None):
            self.assertIn("userDisplayBalanceInfo", request.full_url)
            self.assertEqual(request.get_header("Authorization"), "Bearer amp-key$test")
            return self._make_response(payload)

        with patch.object(AmpAdapter, "_cli_usage_text", return_value=None):
            with patch.dict("os.environ", {"AMP_API_KEY": "amp-key$test"}, clear=True):
                with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                    adapter = AmpAdapter()
                    report = adapter._usage_quota()
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report.remaining_percent, 60.0)
        self.assertIn("Agent usage $30/$50 (60%)", report.detail)
        self.assertIn("Workspace credits $7.5", report.detail)  # balance-only: no %

    def test_amp_falls_back_without_credentials(self):
        with patch.object(AmpAdapter, "_cli_usage_text", return_value=None):
            with patch.dict("os.environ", {}, clear=True):
                with patch("router.adapters.subscriptions._router_config_value", return_value=None):
                    adapter = AmpAdapter()
                    self.assertIsNone(adapter._usage_quota())

    def test_amp_free_reset_synthesized(self):
        """A 'free' meter gets the documented 8PM America/New_York reset."""
        text = ("Amp usage\n"
                "  amp free usage: $3.00 of $10.00 today\n"
                "  agent usage: $12.34 of $50.00 this month\n")
        with patch.object(AmpAdapter, "_cli_usage_text", return_value=text):
            with patch.dict("os.environ", {}, clear=True):
                adapter = AmpAdapter()
                report = adapter._usage_quota()
        self.assertIsNotNone(report)
        assert report is not None
        self.assertIsNotNone(report.reset_at)
        assert report.reset_at is not None
        # Reset lands at 20:00 New York time.
        from zoneinfo import ZoneInfo
        self.assertEqual(report.reset_at.astimezone(ZoneInfo("America/New_York")).hour, 20)
        self.assertGreater(report.reset_at, report.observed_at)
        assert report.extra is not None
        self.assertIn("dailyResetAt", report.extra)

    def test_amp_settings_cookie_last_resort(self):
        """No CLI + no API key -> pasted session cookie hits the settings page."""
        page = "<html>agent usage $30.00 of $50.00</html>"
        requests = []
        response = Mock()
        response.read.return_value = page.encode("utf-8")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        def urlopen(request, timeout=None):
            requests.append(request)
            return response

        with patch.object(AmpAdapter, "_cli_usage_text", return_value=None):
            with patch.dict("os.environ", {"AMP_SESSION_COOKIE": "amp-sess$test"}, clear=True):
                with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                    adapter = AmpAdapter()
                    report = adapter._usage_quota()
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report.remaining_percent, 60.0)
        self.assertIn("settings page", report.detail)
        sent = requests[0]
        self.assertIn("ampcode.com/settings", sent.full_url)
        self.assertEqual(sent.get_header("Cookie"), "amp-sess$test")


class TestKimiUsageQuota(unittest.TestCase):
    """Test Kimi Code membership quota probing."""

    def _make_response(self, data: dict):
        response = Mock()
        response.read.return_value = json.dumps(data).encode("utf-8")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def _write_credentials(self, home: Path) -> None:
        creds_dir = home / ".kimi-code" / "credentials"
        creds_dir.mkdir(parents=True)
        creds_dir.joinpath("kimi-code.json").write_text(
            json.dumps({"access_token": "kimi-token$test"}), encoding="utf-8")

    def test_kimi_prefers_exact_limits_over_ratios(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._write_credentials(home)
            responses = {
                "/usages": {
                    "limits": [{"name": "5h", "used": 40, "limit": 100,
                                "reset_at": "2026-09-27T00:00:00Z"}],
                    "usages": {"limit_5h": 0.9, "limit_7d": 0.5},  # lagging ratio
                },
                "/me": {"email": "user@kimi.dev", "user_level_name": "Pro",
                        "goods_version": 1},
            }
            def urlopen(request, timeout=None):
                for key, payload in responses.items():
                    if key in request.full_url:
                        return self._make_response(payload)
                raise AssertionError(request.full_url)

            with patch("pathlib.Path.home", return_value=home):
                with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                    adapter = KimiAdapter()
                    report = adapter._usage_quota()
        self.assertIsNotNone(report)
        assert report is not None
        # Exact 5h count (60/100) preferred over the lagging 0.9 ratio.
        self.assertEqual(report.remaining_percent, 50.0)  # min(60%, 7d 50%)
        self.assertIn("5h 60/100 (60%)", report.detail)
        self.assertIn("7d 50% remaining", report.detail)
        self.assertIn("user@kimi.dev", report.detail)
        self.assertIn("[Pro]", report.detail)

    def test_kimi_ratio_only_and_v2_suppresses_weekly(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._write_credentials(home)
            responses = {
                "/usages": {"usages": {"limit_5h": 0.25, "limit_7d": 0.5}},
                "/me": {"email": "v2@kimi.dev", "goods_version": 2},
            }
            def urlopen(request, timeout=None):
                for key, payload in responses.items():
                    if key in request.full_url:
                        return self._make_response(payload)
                raise AssertionError(request.full_url)

            with patch("pathlib.Path.home", return_value=home):
                with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                    adapter = KimiAdapter()
                    report = adapter._usage_quota()
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report.remaining_percent, 75.0)
        self.assertIn("5h 75% remaining", report.detail)
        self.assertNotIn("7d", report.detail)  # weekly suppressed on V2

    def test_kimi_payg_balance(self):
        payload = {"data": {"available_balance": 12.5, "voucher_balance": 3.0,
                            "cash_balance": 9.5}}
        def urlopen(request, timeout=None):
            self.assertIn("api.moonshot.ai/v1/users/me/balance", request.full_url)
            return self._make_response(payload)

        with tempfile.TemporaryDirectory() as tmp:
            with patch("pathlib.Path.home", return_value=Path(tmp)):
                with patch.dict("os.environ", {"KIMI_API_KEY": "kimi-payg$test"}, clear=True):
                    with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                        adapter = KimiAdapter()
                        report = adapter._usage_quota()
        self.assertIsNotNone(report)
        assert report is not None
        self.assertIsNone(report.remaining_percent)
        self.assertIn("$12.5 available", report.detail)
        self.assertIn("$3 voucher", report.detail)

    def test_kimi_falls_back_without_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("pathlib.Path.home", return_value=Path(tmp)):
                with patch.dict("os.environ", {}, clear=True):
                    with patch("router.adapters.subscriptions._router_config_value", return_value=None):
                        adapter = KimiAdapter()
                        self.assertIsNone(adapter._usage_quota())


class TestMiniMaxUsageQuota(unittest.TestCase):
    """Test MiniMax token_plan/remains probing."""

    def _make_response(self, data: dict):
        response = Mock()
        response.read.return_value = json.dumps(data).encode("utf-8")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def _probe(self, payload: dict):
        requests = []
        def urlopen(request, timeout=None):
            requests.append(request)
            return self._make_response(payload)
        with patch.dict("os.environ", {"MINIMAX_API_KEY": "mmx-key$test"}, clear=True):
            with patch("router.adapters.subscriptions.urllib.request.urlopen", side_effect=urlopen):
                adapter = MiniMaxAdapter()
                return adapter._usage_quota(), requests

    def test_minimax_prefers_counts_over_percent(self):
        report, requests = self._probe({
            "base_resp": {"status_code": 0},
            "model_remains": [
                {"model_name": "general",
                 "current_interval_total_count": 100,
                 "current_interval_usage_count": 30,
                 "remaining_percent": 5,  # stale/wrong — counts win
                 "start_time": 1759000000000, "end_time": 1759018000000,
                 "weekly_total_count": 500, "weekly_usage_count": 100,
                 "weekly_end_time": 1759600000000},
                {"model_name": "video", "remaining_percent": 90},
            ],
        })
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report.remaining_percent, 70.0)  # 1 - 30/100
        self.assertIn("general interval 70%", report.detail)
        self.assertIn("general weekly 80%", report.detail)
        self.assertIn("video interval 90%", report.detail)  # percent fallback
        assert report.extra is not None
        self.assertEqual(report.extra["dailyPercent"], 70.0)
        self.assertEqual(report.extra["weeklyPercent"], 80.0)
        self.assertIsNotNone(report.reset_at)
        sent = requests[0]
        self.assertIn("token_plan/remains", sent.full_url)
        self.assertEqual(sent.get_header("Authorization"), "Bearer mmx-key$test")

    def test_minimax_nonzero_status_code_gives_no_reading(self):
        report, _ = self._probe({
            "base_resp": {"status_code": 1002, "status_msg": "invalid key"},
            "model_remains": [{"model_name": "general", "remaining_percent": 50}],
        })
        self.assertIsNone(report)

    def test_minimax_falls_back_without_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("pathlib.Path.home", return_value=Path(tmp)):
                with patch.dict("os.environ", {}, clear=True):
                    adapter = MiniMaxAdapter()
                    self.assertIsNone(adapter._usage_quota())


if __name__ == "__main__":
    unittest.main()