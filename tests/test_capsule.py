"""Tests for session capsules."""
import unittest
import json
import tempfile
import shutil
from pathlib import Path
from router.core.capsule import write_capsule, resume_brief, list_capsules, capsule_dir


class TestCapsules(unittest.TestCase):
    """Test capsule functionality."""

    def setUp(self):
        """Create a temporary capsule directory for testing."""
        self.temp_dir = tempfile.mkdtemp()
        import os
        os.environ["ROUTER_HOME"] = self.temp_dir

    def tearDown(self):
        """Clean up temporary directory."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        import os
        if "ROUTER_HOME" in os.environ:
            del os.environ["ROUTER_HOME"]

    def test_write_capsule(self):
        """Test writing a capsule."""
        path = write_capsule(
            task="test task",
            notes="Test notes"
        )
        self.assertTrue(path.exists())
        self.assertEqual(path.suffix, ".json")

        # Verify capsule content
        data = json.loads(path.read_text())
        self.assertEqual(data["task"]["summary"], "test task")
        self.assertEqual(data["capsule_version"], 1)
        self.assertEqual(data["created_by"], "unknown")

    def test_resume_brief(self):
        """Test generating a resume brief."""
        path = write_capsule(
            task="test task",
            notes="Test notes"
        )
        brief = resume_brief(path)

        self.assertIn("test task", brief)
        self.assertIn("Resumed session", brief)
        self.assertIn("Task:", brief)

    def test_list_capsules(self):
        """Test listing capsules."""
        # Create test capsules with unique names to avoid conflicts
        path1 = write_capsule(task="test_capsule_task_1")
        path2 = write_capsule(task="test_capsule_task_2")

        # Verify the files exist
        self.assertTrue(path1.exists())
        self.assertTrue(path2.exists())

        # List capsules
        capsules = list_capsules()

        # Should have at least our 2 test capsules
        self.assertGreaterEqual(len(capsules), 2)

        # Check that our test capsules are in the list by path
        capsule_paths = [c["path"] for c in capsules]
        self.assertIn(str(path1), capsule_paths)
        self.assertIn(str(path2), capsule_paths)


if __name__ == "__main__":
    unittest.main()