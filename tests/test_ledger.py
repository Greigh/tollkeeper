"""Tests for the spend ledger."""
import unittest
import os
import tempfile
from pathlib import Path
from router.core.ledger import log_run, today_spend, spend_summary, log_quota_observation, latest_quota_observation


class TestLedger(unittest.TestCase):
    """Test the sqlite ledger functionality."""

    def setUp(self):
        """Create a temporary ledger for testing."""
        self.temp_dir = tempfile.mkdtemp()
        os.environ["ROUTER_HOME"] = self.temp_dir

    def tearDown(self):
        """Clean up temporary directory."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        if "ROUTER_HOME" in os.environ:
            del os.environ["ROUTER_HOME"]

    def test_log_run(self):
        """Test logging a run."""
        log_run(
            task="test task",
            task_class="standard",
            adapter="claude",
            model="default",
            dry_run=False,
            est_cost_usd=0.0,
            actual_cost_usd=0.0
        )

        # Verify the run was logged
        import sqlite3
        from router.core.ledger import db_path
        conn = sqlite3.connect(db_path())
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM runs WHERE task='test task'")
        result = cursor.fetchone()
        conn.close()

        self.assertIsNotNone(result)
        self.assertEqual(result[4], "claude")  # adapter is column 4 (0-indexed)

    def test_today_spend(self):
        """Test today's spend calculation."""
        log_run(
            task="test task",
            task_class="standard",
            adapter="claude",
            model="default",
            dry_run=False,
            est_cost_usd=0.0,
            actual_cost_usd=1.50
        )

        spend = today_spend()
        self.assertEqual(spend, 1.50)

    def test_quota_observation(self):
        """Test quota observation logging and retrieval."""
        log_quota_observation(
            adapter="claude",
            state="depleted",
            detail="Test depletion",
            reset_at="2026-09-26T20:00:00",
            remaining_percent=4.5
        )

        result = latest_quota_observation("claude")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "depleted")  # state
        self.assertEqual(result[1], "Test depletion")  # detail
        self.assertEqual(result[4], 4.5)  # vendor-reported remaining percentage


if __name__ == "__main__":
    unittest.main()