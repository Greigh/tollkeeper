import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from router.app.api import app
from router.core.ledger import log_run
from router.core.policy import RoutingDecision


class TestApplicationApi(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_home = os.environ.get("ROUTER_HOME")
        self.previous_config = os.environ.get("ROUTER_CONFIG")
        os.environ["ROUTER_HOME"] = self.temp_dir.name
        os.environ["ROUTER_CONFIG"] = os.path.join(self.temp_dir.name, "config.toml")
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.temp_dir.cleanup()
        if self.previous_home is None:
            os.environ.pop("ROUTER_HOME", None)
        else:
            os.environ["ROUTER_HOME"] = self.previous_home
        if self.previous_config is None:
            os.environ.pop("ROUTER_CONFIG", None)
        else:
            os.environ["ROUTER_CONFIG"] = self.previous_config

    def test_health_and_overview(self):
        log_run(task="test", task_class="standard", adapter="claude", model="default",
                dry_run=False, est_cost_usd=0.0, actual_cost_usd=0.0)
        self.assertEqual(self.client.get("/api/health").json()["status"], "ok")
        overview = self.client.get("/api/overview").json()
        self.assertEqual(overview["metrics"]["runs"], 1)
        self.assertEqual(overview["recentRuns"][0]["adapter"], "claude")

    def test_plan_endpoint(self):
        decision = RoutingDecision("task", "standard", "claude", "default", "healthy", 0.0)
        with patch("router.app.api.RouterService.plan", return_value=decision):
            response = self.client.post("/api/plan", json={"task": "task"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["adapter"], "claude")

    def test_config_round_trip_and_validation(self):
        content = "[policy]\ndaily_cap_usd = 3.0\n"
        response = self.client.put("/api/config", json={"content": content})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/config").json()["content"], content)
        invalid = self.client.put("/api/config", json={"content": "[broken"})
        self.assertEqual(invalid.status_code, 400)

    def test_probe_adapter_endpoint(self):
        response = self.client.post("/api/adapters/devin/probe")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["name"], "devin")
        self.assertIn("quota", data)

    def test_probe_unknown_adapter_returns_404(self):
        response = self.client.post("/api/adapters/nonexistent/probe")
        self.assertEqual(response.status_code, 404)

    def test_secrets_round_trip(self):
        response = self.client.put("/api/secrets", json={"name": "DEVIN_API_KEY", "value": "cog-test"})
        self.assertEqual(response.status_code, 200)
        data = self.client.get("/api/secrets").json()
        self.assertIn("DEVIN_API_KEY", data["secrets"])
        self.client.delete("/api/secrets/DEVIN_API_KEY")
        data = self.client.get("/api/secrets").json()
        self.assertNotIn("DEVIN_API_KEY", data["secrets"])


if __name__ == "__main__":
    unittest.main()
