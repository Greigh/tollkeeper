"""Tests for router.core.credentials (OS keychain backends)."""
import sys
import unittest
from unittest.mock import patch

from router.core import credentials


class TestCredentials(unittest.TestCase):
    def test_env_override_wins(self):
        with patch.dict("os.environ", {"ROUTER_CRED_GEMINI_ANTIGRAVITY": "env-tok"}, clear=True):
            self.assertEqual(credentials.get("gemini", "antigravity"), "env-tok")

    def test_env_name_sanitizes(self):
        self.assertEqual(
            credentials._env_name("my-svc", "acct/x"),
            "ROUTER_CRED_MY_SVC_ACCT_X")

    def test_backend_name(self):
        expected = {
            "darwin": "macos-keychain",
            "win32": "windows-credential-manager",
        }.get(sys.platform, "linux-secret-service")
        self.assertEqual(credentials.backend_name(), expected)

    def test_get_returns_none_on_backend_failure(self):
        with patch.dict("os.environ", {}, clear=True):
            with patch("subprocess.run", side_effect=OSError("no backend")):
                # macOS/Linux both go through subprocess; a failure must
                # degrade to None rather than raise.
                if sys.platform == "win32":
                    self.skipTest("windows backend uses ctypes, not subprocess")
                self.assertIsNone(credentials.get("svc", "acct"))


if __name__ == "__main__":
    unittest.main()
