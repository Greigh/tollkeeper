import json
import os
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

from router.core.ledger import log_quota_observation, log_run
from router.dashboard import create_dashboard_server


class TestDashboard(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_home = os.environ.get("ROUTER_HOME")
        os.environ["ROUTER_HOME"] = self.temp_dir.name
        self.server = create_dashboard_server(0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp_dir.cleanup()
        if self.previous_home is None:
            os.environ.pop("ROUTER_HOME", None)
        else:
            os.environ["ROUTER_HOME"] = self.previous_home

    def get_json(self, path):
        with urlopen(f"{self.base_url}{path}", timeout=2) as response:
            return response, json.load(response)

    def test_dashboard_and_api_security_headers(self):
        with urlopen(self.base_url, timeout=2) as response:
            body = response.read().decode()
            self.assertIn("Coding Router Dashboard", body)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
            self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])

    def test_unknown_paths_do_not_serve_local_files(self):
        with self.assertRaises(HTTPError) as raised:
            urlopen(f"{self.base_url}/pyproject.toml", timeout=2)
        error = raised.exception
        try:
            self.assertEqual(error.code, 404)
            self.assertEqual(json.load(error), {"error": "not found"})
        finally:
            error.close()

    def test_metrics_exclude_dry_runs_and_use_latest_quota_state(self):
        log_run(task="real", task_class="standard", adapter="openrouter", model="m",
                dry_run=False, est_cost_usd=0.5, actual_cost_usd=0.25)
        log_run(task="dry", task_class="standard", adapter="claude", model="default",
                dry_run=True, est_cost_usd=0.0, actual_cost_usd=0.0)
        log_quota_observation("claude", "ok", "available", remaining_percent=42.5)
        log_quota_observation("claude", "depleted", "limit reached", remaining_percent=3.0)

        _, data = self.get_json("/api/data")
        self.assertEqual(data["totalRuns"], 1)
        self.assertEqual(data["totalSpend"], 0.25)
        self.assertEqual(data["avoidedCost"], 0.25)
        self.assertEqual(data["activeAdapters"], 0)

        _, quota = self.get_json("/api/quota")
        self.assertEqual(len(quota["adapters"]), 1)
        self.assertEqual(quota["adapters"][0]["state"], "depleted")
        self.assertEqual(quota["adapters"][0]["remainingPercent"], 3.0)
        self.assertNotIn("localEstimate", quota["adapters"][0])

    def test_spend_endpoint_returns_recent_runs(self):
        log_run(task="<script>alert(1)</script>", task_class="standard",
                adapter="openrouter", model="safe", dry_run=False,
                est_cost_usd=0.1, actual_cost_usd=0.05)
        response, data = self.get_json("/api/spend")
        self.assertEqual(response.headers.get_content_type(), "application/json")
        self.assertEqual(data["spend"][0]["total"], 0.05)
        self.assertEqual(data["recentRuns"][0]["task"], "<script>alert(1)</script>")


if __name__ == "__main__":
    unittest.main()
