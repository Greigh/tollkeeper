"""Adapter interface.

Every backend -- a subscription CLI you already pay for, or a metered API --
implements this contract. Adapters never touch vendor auth or billing; they
shell out to (or call) whatever the user already has configured.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Literal, Protocol


class QuotaState(Enum):
    """Vendor-reported quota state for a provider account."""

    OK = "ok"
    DEPLETED = "depleted"
    UNKNOWN = "unknown"


@dataclass
class QuotaReport:
    """One quota observation. ``remaining_percent`` is remaining (not used);
    ``extra`` carries provider-specific fields (meters, plan, resets)."""

    state: QuotaState
    detail: str
    reset_at: datetime | None = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    remaining_percent: float | None = None
    extra: dict | None = None


@dataclass
class ModelInfo:
    """A routable model with pricing, quality, and billing-pool metadata."""

    id: str
    provider: str
    input_per_1m: float
    output_per_1m: float
    quality_tier: int
    context_window: int
    billed: Literal["subscription", "metered", "free"]


@dataclass
class RunRequest:
    """A prompt execution request; ``on_output`` streams (stream, text) chunks."""

    prompt: str
    model: str | None = None
    workdir: Path = field(default_factory=lambda: Path("."))
    timeout_s: int = 120
    on_output: Callable[[str, str], None] | None = None


@dataclass
class RunResult:
    """Result of a run; ``depleted_mid_run`` flags mid-run quota exhaustion."""

    output: str
    model_used: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    depleted_mid_run: bool = False


@dataclass
class AdapterHealth:
    """Combined availability + quota snapshot for the dashboard."""

    name: str
    reachable: bool
    quota: QuotaReport
    note: str = ""


class Adapter(Protocol):
    """Provider adapter contract — read-only credentials, no token refresh."""

    name: str
    kind: str  # "subscription" | "metered"

    def is_available(self) -> bool:
        """True when the provider's CLI/app/credentials are usable locally."""

    def probe_quota(self) -> QuotaReport:
        """Fetch the vendor-reported quota state (never fabricates numbers)."""

    def models(self) -> list[ModelInfo]:
        """Models this adapter can route to."""

    def health(self) -> AdapterHealth:
        """Availability + quota snapshot for status displays."""

    def estimate_cost_usd(self, in_tokens: int, out_tokens: int, model: str) -> float:
        """Estimated dollar cost for a run on ``model``."""

    def run(self, req: RunRequest) -> RunResult:
        """Execute the prompt through the provider's own tooling."""
