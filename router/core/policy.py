"""Policy engine: classify the task, pick the cheapest capable route.

Routing rule (v1): sunk cost first, metered second.
A healthy subscription adapter always wins ($0 marginal cost); otherwise the
cheapest metered model whose capability tier meets the task class wins.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Sequence

from .adapter import Adapter, QuotaState, RunRequest
from ..adapters.openrouter import OpenRouterAdapter

TRIVIAL_HINTS = ("typo", "rename", "format", "comment", "readme", "docstring", "lint", "spell")
HARD_HINTS = ("architect", "refactor", "design", "migrate", "race condition",
              "concurrency", "performance", "security", "deadlock")

TIER_RANK = {"cheap": 0, "mid": 1, "frontier": 2}
CLASS_MIN_TIER = {"trivial": "cheap", "standard": "mid", "hard": "frontier"}


class TaskClassifier:
    """Cheap-model task classifier using OpenRouter's cheapest model."""

    def __init__(self, api_key: str | None = None):
        self.adapter = OpenRouterAdapter(api_key)
        self.cache = {}  # Simple in-memory cache for prompt hash -> classification

    def classify(self, task: str) -> str:
        """Classify task using cheap model (with keyword fallback)."""
        # Check cache first
        if task in self.cache:
            return self.cache[task]

        # Try cheap model classification
        try:
            result = self._classify_with_model(task)
            self.cache[task] = result
            return result
        except Exception:
            # Fallback to keyword classification
            return self._classify_keywords(task)

    def _classify_with_model(self, task: str) -> str:
        """Use a cheap model to classify the task."""
        prompt = f"""Classify this coding task into one category: trivial, standard, or hard.

Trivial: simple changes, typos, formatting, comments, docs
Standard: normal coding tasks, bug fixes, small features
Hard: complex refactoring, architecture, performance, concurrency

Task: {task}

Respond with ONLY the category name (trivial, standard, or hard)."""

        req = RunRequest(prompt=prompt, model="google/gemini-flash-1.5")
        result = self.adapter.run(req)

        # Extract classification from response
        classification = result.output.strip().lower()
        if classification in ["trivial", "standard", "hard"]:
            return classification
        else:
            raise ValueError(f"Invalid classification: {classification}")

    def _classify_keywords(self, task: str) -> str:
        """Fallback keyword classifier."""
        t = task.lower()
        if any(h in t for h in HARD_HINTS):
            return "hard"
        if any(h in t for h in TRIVIAL_HINTS):
            return "trivial"
        return "standard"


def classify(task: str) -> str:
    """Cheap keyword classifier (M6 replaces this with a tiny model call)."""
    classifier = TaskClassifier()
    return classifier.classify(task)


@dataclass
class RoutingDecision:
    """Chosen adapter/model for a task, with rationale and estimated cost."""

    task: str
    task_class: str
    adapter: str
    model: str
    reason: str
    est_cost_usd: float
    dry_run: bool = False
    route_source: str = "policy"  # "policy", "forced", "routing_table"


@dataclass
class RoutingRule:
    """A routing rule that maps task types to adapter order."""
    types: list[str]
    adapter_order: list[str]
    complexity_filter: list[str] | None = None  # Only apply for specific complexities


def load_routing_table() -> list[RoutingRule]:
    """Load routing table from config file. Default routing table from design doc §7.2."""
    default_rules = [
        RoutingRule(
            types=["tests", "docs", "boilerplate"],
            adapter_order=["openrouter:deepseek-v3", "gemini", "cursor"]
        ),
        RoutingRule(
            types=["feature", "refactor"],
            adapter_order=["claude", "cursor", "openrouter:claude-sonnet"],
            complexity_filter=["low", "medium"]
        ),
        RoutingRule(
            types=["bugfix"],
            adapter_order=["claude", "cursor:claude", "openrouter:claude-opus"],
            complexity_filter=["high"]
        ),
        RoutingRule(
            types=["architecture"],
            adapter_order=["claude", "openrouter:claude-opus"]
        ),
        RoutingRule(
            types=["research"],
            adapter_order=["gemini", "openrouter:gemini-pro"]
        ),
    ]
    return default_rules


def _load_routing_rules_from_config() -> list[RoutingRule]:
    """Load routing rules from config file format."""
    import tomllib
    from pathlib import Path

    config_path = Path.home() / ".config" / "router" / "config.toml"
    if not config_path.exists():
        return []

    try:
        config = tomllib.loads(config_path.read_text())
        routes = config.get("routing", {}).get("routes", [])

        rules = []
        for route in routes:
            rules.append(RoutingRule(
                types=route.get("types", []),
                adapter_order=route.get("adapter_order", []),
                complexity_filter=route.get("complexity_filter")
            ))
        return rules
    except Exception:
        return []


def decide(task: str, adapters: Sequence[Adapter], catalog: Sequence[dict],
           est_in: int = 2000, est_out: int = 1000,
           routing_table: list[RoutingRule] | None = None) -> RoutingDecision:
    """Make routing decision with optional routing table support."""
    task_class = classify(task)
    min_tier = TIER_RANK[CLASS_MIN_TIER[task_class]]

    # Use routing table if provided, otherwise use default logic
    if routing_table:
        # Convert dict config format to RoutingRule objects if necessary
        rules = [
            RoutingRule(
                types=route.get("types", []),
                adapter_order=route.get("adapter_order", []),
                complexity_filter=route.get("complexity_filter")
            ) if isinstance(route, dict) else route
            for route in routing_table
        ]
    else:
        rules = _load_routing_rules_from_config() or load_routing_table()

    # Check routing table first
    for rule in rules:
        if task_class in rule.types:
            # Check complexity filter if specified
            if rule.complexity_filter and task_class not in rule.complexity_filter:
                continue

            # Try each adapter in order
            for adapter_spec in rule.adapter_order:
                # Parse adapter spec: "adapter" or "adapter:model"
                if ":" in adapter_spec:
                    adapter_name, model = adapter_spec.split(":", 1)
                else:
                    adapter_name, model = adapter_spec, "default"

                # Find adapter
                for a in adapters:
                    if a.name == adapter_name:
                        # Check if adapter is healthy
                        if a.kind == "subscription" and a.health().quota.state == QuotaState.OK:
                            return RoutingDecision(
                                task, task_class, a.name, model,
                                f"routing table: {adapter_spec} for {task_class}", 0.0,
                                route_source="routing_table")
                        elif a.kind == "metered":
                            # Check if model exists in catalog
                            for m in catalog:
                                if m["id"] == model:
                                    cost = a.estimate_cost_usd(est_in, est_out, model)
                                    return RoutingDecision(
                                        task, task_class, a.name, model,
                                        f"routing table: {adapter_spec} for {task_class}", cost,
                                        route_source="routing_table")
                        break

    # Fallback to default logic: sunk cost first, metered second
    for a in adapters:
        if a.kind == "subscription" and a.health().quota.state == QuotaState.OK:
            return RoutingDecision(
                task, task_class, a.name, "default",
                f"sunk cost first: {a.name} healthy, $0 marginal", 0.0)

    # Metered second: cheapest model capable of the task class.
    options = []
    for a in adapters:
        if a.kind != "metered":
            continue
        for m in catalog:
            if TIER_RANK.get(m.get("tier", "frontier"), 2) >= min_tier:
                options.append((a.estimate_cost_usd(est_in, est_out, m["id"]), a, m))
    if not options:
        raise RuntimeError("no capable model available from any metered adapter")
    cost, adapter, model = min(options, key=lambda o: o[0])
    return RoutingDecision(
        task, task_class, adapter.name, model["id"],
        f"cheapest {CLASS_MIN_TIER[task_class]}-tier-or-better model in catalog", cost)
