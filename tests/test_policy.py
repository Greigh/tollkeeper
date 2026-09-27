"""Tests for the policy engine."""
import unittest
from router.core.policy import classify, RoutingRule, decide


class TestKeywordClassifier(unittest.TestCase):
    """Test the keyword-based fallback classifier."""

    def test_trivial_tasks(self):
        self.assertEqual(classify("fix a typo"), "trivial")
        self.assertEqual(classify("update the README"), "trivial")
        self.assertEqual(classify("add a comment"), "trivial")

    def test_standard_tasks(self):
        self.assertEqual(classify("add a new feature"), "standard")
        self.assertEqual(classify("fix this bug"), "standard")

    def test_hard_tasks(self):
        self.assertEqual(classify("architect a new system"), "hard")
        self.assertEqual(classify("refactor for performance"), "hard")
        self.assertEqual(classify("design a concurrency model"), "hard")


class TestRoutingRules(unittest.TestCase):
    """Test routing table functionality."""

    def test_routing_rule_creation(self):
        rule = RoutingRule(
            types=["trivial", "standard"],
            adapter_order=["cursor", "gemini"]
        )
        self.assertEqual(rule.types, ["trivial", "standard"])
        self.assertEqual(rule.adapter_order, ["cursor", "gemini"])
        self.assertIsNone(rule.complexity_filter)

    def test_routing_rule_with_complexity(self):
        rule = RoutingRule(
            types=["feature"],
            adapter_order=["claude", "openrouter"],
            complexity_filter=["high"]
        )
        self.assertEqual(rule.complexity_filter, ["high"])


if __name__ == "__main__":
    unittest.main()