"""RouterService — shared routing/execution core used by the CLI and app API.

Loads config, builds the adapter set, and turns a task into a routing
decision plus an executed run recorded in the ledger and capsule store.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .adapters.openrouter import OpenRouterAdapter
from .adapters.subscriptions import (ClaudeCodeAdapter, CodexAdapter, CopilotAdapter,
                                     CursorAdapter, DevinAdapter, GeminiAdapter,
                                     PerplexityAdapter, ZedAdapter, ZcodeAdapter,
                                     DeepSeekAdapter, XAIAdapter, AmpAdapter,
                                     KimiAdapter, MiniMaxAdapter)
from .core.adapter import RunRequest
from .core.capsule import resume_brief, write_capsule
from .core.ledger import log_run, today_spend
from .core.policy import RoutingDecision, classify, decide


def config_path() -> Path:
    """Router config path — $ROUTER_CONFIG or ~/.config/router/config.toml."""
    return Path(os.environ.get("ROUTER_CONFIG", Path.home() / ".config" / "router" / "config.toml"))


def load_config() -> dict:
    """Load the TOML config; empty dict when the file doesn't exist."""
    path = config_path()
    return tomllib.loads(path.read_text()) if path.exists() else {}


def build_adapters(api_key: str | None = None):
    """Instantiate every provider adapter; ``api_key`` feeds OpenRouter."""
    return [ClaudeCodeAdapter(), CursorAdapter(), GeminiAdapter(), DevinAdapter(),
            CodexAdapter(), CopilotAdapter(), PerplexityAdapter(), ZedAdapter(),
            ZcodeAdapter(), DeepSeekAdapter(), XAIAdapter(), AmpAdapter(),
            KimiAdapter(), MiniMaxAdapter(), OpenRouterAdapter(api_key)]


class RouterService:
    """Plans and executes prompts across the adapter pool."""

    def __init__(self, api_key: str | None = None) -> None:
        self.adapters = build_adapters(api_key)
        self.by_name = {adapter.name: adapter for adapter in self.adapters}

    def plan(self, task: str, adapter: str | None = None,
             model: str | None = None) -> RoutingDecision:
        """Pick an adapter/model for ``task``; explicit adapter forces the route."""
        if not task.strip():
            raise ValueError("task is required")
        if adapter:
            if adapter not in self.by_name:
                raise ValueError(f"unknown adapter {adapter!r}")
            return RoutingDecision(
                task, classify(task), adapter, model or "default",
                f"forced via application: {adapter}", 0.0,
                route_source="forced"
            )
        config = load_config()
        routes = config.get("routing", {}).get("routes") or None
        openrouter = self.by_name["openrouter"]
        decision = decide(task, self.adapters, openrouter.catalog(), routing_table=routes)
        if model:
            decision.model = model
            if decision.adapter == "openrouter":
                decision.est_cost_usd = openrouter.estimate_cost_usd(2000, 1000, model)
        return decision

    def execute(self, task: str, adapter: str | None = None,
                model: str | None = None, resume: str | None = None,
                workdir: str | None = None,
                on_output: Callable[[str, str], None] | None = None) -> dict:
        """Run ``task``: plan, enforce the metered spend cap, execute, log, capsule."""
        prompt = task
        if resume:
            prompt = f"{resume_brief(resume)}\n\nContinue with: {task}"
        decision = self.plan(prompt, adapter, model)
        selected = self.by_name[decision.adapter]
        if not selected.is_available():
            raise RuntimeError(f"adapter {decision.adapter} is not available")
        if decision.adapter == "openrouter":
            cap = float(load_config().get("policy", {}).get("daily_cap_usd", 5.0))
            if today_spend() >= cap:
                raise RuntimeError(f"daily metered spend cap of ${cap:.2f} reached")
        result = selected.run(RunRequest(
            prompt=prompt,
            model=decision.model,
            workdir=Path(workdir or ".").resolve(),
            on_output=on_output
        ))
        log_run(
            task=prompt,
            task_class=decision.task_class,
            adapter=decision.adapter,
            model=decision.model,
            dry_run=False,
            est_cost_usd=decision.est_cost_usd,
            actual_cost_usd=result.cost_usd
        )
        trigger = "depletion" if result.depleted_mid_run else "manual"
        notes = "Quota depleted during execution; resume with another adapter." if result.depleted_mid_run else ""
        capsule = write_capsule(prompt, decision, result.output,
                                handoff_notes=notes, triggered_by=trigger)
        return {
            "decision": asdict(decision),
            "result": asdict(result),
            "capsule": str(capsule)
        }
