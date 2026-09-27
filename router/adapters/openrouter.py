"""OpenRouter: the metered adapter. One key, 400+ models, real prices.

/models is public (no key needed); /chat/completions needs OPENROUTER_API_KEY.
Model list is cached for 24h under ~/.cache/router/.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from ..core.adapter import (AdapterHealth, ModelInfo, QuotaReport, QuotaState,
                             RunRequest, RunResult)
from ..core.secrets import get_secret

API = "https://openrouter.ai/api/v1"


def _cache_file() -> Path:
    p = Path.home() / ".cache" / "router"
    p.mkdir(parents=True, exist_ok=True)
    return p / "openrouter_models.json"


class OpenRouterAdapter:
    """Metered fallback: OpenRouter chat completions billed per token."""

    name = "openrouter"
    kind = "metered"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or get_secret("OPENROUTER_API_KEY")

    def _fetch_models(self) -> dict:
        cp = _cache_file()
        if cp.exists() and time.time() - cp.stat().st_mtime < 86400:
            return json.loads(cp.read_text())
        req = urllib.request.Request(API + "/models",
                                     headers={"User-Agent": "tollkeeper/0.1"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
        cp.write_text(json.dumps(data))
        return data

    def catalog(self) -> list[dict]:
        """Text-in/text-out models, tiered by price (per 1M prompt tokens).

        Tiering is deliberately crude for the spike: price is a rough proxy
        for capability. Guardrails: OpenRouter's own router ids
        (openrouter/*) are not real models, negative prices are data errors,
        and models older than ~2 years can't claim frontier (stale list
        prices, e.g. gpt-3.5-turbo-16k still priced like it's 2023).
        """
        out = []
        now = time.time()
        for m in self._fetch_models().get("data", []):
            if m.get("architecture", {}).get("modality") != "text->text":
                continue
            mid = m.get("id", "")
            if "embed" in mid or mid.startswith("openrouter/"):
                continue
            pricing = m.get("pricing", {})
            try:
                pin = float(pricing.get("prompt", 0))
                pout = float(pricing.get("completion", 0))
            except (TypeError, ValueError):
                continue
            if pin < 0 or pout < 0:
                continue  # bogus pricing data
            per_m = pin * 1_000_000
            tier = "cheap" if per_m < 0.5 else "mid" if per_m < 3.0 else "frontier"
            age_days = (now - m.get("created", now)) / 86400
            if tier == "frontier" and age_days > 730:
                tier = "mid"
            out.append({"id": mid, "tier": tier, "prompt": pin,
                        "completion": pout, "context": m.get("context_length", 0)})
        return out

    def models(self) -> list[ModelInfo]:
        """Catalog entries as typed ModelInfo for the policy engine."""
        catalog = self.catalog()
        out = []
        for m in catalog:
            tier_map = {"cheap": 3, "mid": 2, "frontier": 1}
            out.append(ModelInfo(
                id=m["id"],
                provider="openrouter",
                input_per_1m=m["prompt"] * 1_000_000,
                output_per_1m=m["completion"] * 1_000_000,
                quality_tier=tier_map.get(m["tier"], 2),
                context_window=m["context"],
                billed="metered"
            ))
        return out

    # -- Adapter protocol -------------------------------------------------
    def is_available(self) -> bool:
        """/models is public, so health checks always work; runs need a key."""
        return True

    def _key_info(self) -> dict | None:
        """GET /api/v1/key — credit limit, usage windows, BYOK spend."""
        if not self.api_key:
            return None
        req = urllib.request.Request(
            API + "/key",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "User-Agent": "tollkeeper/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode())
        except (urllib.error.HTTPError, urllib.error.URLError, OSError,
                json.JSONDecodeError, TimeoutError):
            return None
        inner = data.get("data")
        return inner if isinstance(inner, dict) else None

    def probe_quota(self, force: bool = False) -> QuotaReport:
        """Key credit limit/usage; the only adapter with a dollar-spend view."""
        info = self._key_info()
        if info is None:
            return QuotaReport(
                state=QuotaState.OK if not self.api_key else QuotaState.UNKNOWN,
                detail=("metered: no quota, pure pay-per-use" if not self.api_key
                        else "openrouter: /key endpoint unreachable or key rejected"),
                observed_at=datetime.now(timezone.utc)
            )

        limit_remaining = info.get("limit_remaining")
        limit = info.get("limit")
        limit_reset = info.get("limit_reset")  # cadence string or null = never resets
        is_free = info.get("is_free_tier")

        parts: list[str] = []
        remaining_percent = None
        if isinstance(limit_remaining, (int, float)):
            if isinstance(limit, (int, float)) and limit > 0:
                parts.append(f"${limit_remaining:.2f} of ${limit:.2f} limit left")
                remaining_percent = float(round(max(0.0, min(100.0, limit_remaining / limit * 100.0))))
            else:
                parts.append(f"${limit_remaining:.2f} credits left")
            if limit_reset:
                parts.append(f"resets {limit_reset}")
        elif limit is None:
            parts.append("unlimited credits")
        if is_free:
            parts.append("free tier")
        for key, label in (("usage_daily", "today"), ("usage_weekly", "this week"),
                           ("usage_monthly", "this month"), ("usage", "all-time")):
            value = info.get(key)
            if isinstance(value, (int, float)) and value:
                parts.append(f"${value:.2f} {label}")
        byok = info.get("byok_usage") or info.get("byok_usage_daily")
        if isinstance(byok, (int, float)) and byok:
            parts.append(f"${byok:.2f} BYOK")

        extra: dict[str, object] = {}
        if isinstance(limit_remaining, (int, float)):
            extra["limitRemainingUsd"] = float(limit_remaining)
        if isinstance(limit, (int, float)) and limit > 0:
            extra["limitUsd"] = float(limit)
        if limit_reset:
            extra["limitReset"] = str(limit_reset)
        for key in ("usage_daily", "usage_weekly", "usage_monthly", "usage"):
            value = info.get(key)
            if isinstance(value, (int, float)):
                extra[f"{key}Usd"] = float(value)
        if is_free is not None:
            extra["freeTier"] = bool(is_free)
        # Surface the key's credit limit in the matching UI window when the
        # reset cadence tells us which window it governs.
        if remaining_percent is not None:
            slot = {"daily": "dailyPercent", "weekly": "weeklyPercent"}.get(str(limit_reset))
            if slot:
                extra[slot] = remaining_percent

        state = QuotaState.DEPLETED if (isinstance(limit_remaining, (int, float))
                                        and limit_remaining <= 0) else QuotaState.OK
        return QuotaReport(
            state=state,
            detail="openrouter: " + ", ".join(parts),
            observed_at=datetime.now(timezone.utc),
            remaining_percent=remaining_percent,
            extra=extra,
        )

    def health(self, force_probe: bool = False) -> AdapterHealth:
        """Reachable when the public /models catalog responds."""
        try:
            n = len(self._fetch_models().get("data", []))
            return AdapterHealth(self.name, True,
                                 QuotaReport(
                                     state=QuotaState.OK,
                                     detail=f"{n} models listed",
                                     observed_at=datetime.now(timezone.utc)
                                 ),
                                 "metered via OpenRouter")
        except Exception as e:  # network down etc.
            return AdapterHealth(self.name, False,
                                 QuotaReport(
                                     state=QuotaState.UNKNOWN,
                                     detail=str(e),
                                     observed_at=datetime.now(timezone.utc)
                                 ))

    def estimate_cost_usd(self, in_tokens: int, out_tokens: int, model: str) -> float:
        """Token-cost estimate from the catalog's per-token prices."""
        for m in self.catalog():
            if m["id"] == model:
                return in_tokens * m["prompt"] + out_tokens * m["completion"]
        raise ValueError(f"unknown model {model!r}")

    def run(self, req: RunRequest) -> RunResult:
        """POST /chat/completions with retry/backoff on 429 and 5xx."""
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not set")
        model = req.model or "anthropic/claude-3.5-sonnet"

        # Retry logic for transient failures
        max_retries = 3
        base_delay = 1.0  # seconds

        for attempt in range(max_retries):
            try:
                body = json.dumps({"model": model,
                                   "messages": [{"role": "user", "content": req.prompt}]}).encode()
                http_req = urllib.request.Request(
                    API + "/chat/completions", data=body,
                    headers={"Authorization": f"Bearer {self.api_key}",
                             "Content-Type": "application/json",
                             "User-Agent": "tollkeeper/0.1"})
                with urllib.request.urlopen(http_req, timeout=req.timeout_s) as r:
                    data = json.loads(r.read().decode())

                text = data["choices"][0]["message"]["content"]
                if req.on_output:
                    req.on_output("stdout", text)
                usage = data.get("usage", {})
                in_tok = usage.get("prompt_tokens", 0)
                out_tok = usage.get("completion_tokens", 0)
                cost = self.estimate_cost_usd(in_tok, out_tok, model)
                return RunResult(
                    output=text,
                    model_used=model,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    cost_usd=cost,
                    depleted_mid_run=False
                )
            except urllib.error.HTTPError as e:
                # Handle specific HTTP errors
                if e.code == 429:  # Rate limited
                    if attempt < max_retries - 1:
                        delay = base_delay * (2 ** attempt)  # Exponential backoff
                        print(f"Rate limited. Retrying in {delay}s... (attempt {attempt + 1}/{max_retries})", file=sys.stderr)
                        time.sleep(delay)
                        continue
                    else:
                        raise RuntimeError(f"OpenRouter rate limit exceeded after {max_retries} attempts")
                elif e.code in [500, 502, 503, 504]:  # Server errors
                    if attempt < max_retries - 1:
                        delay = base_delay * (2 ** attempt)
                        print(f"Server error {e.code}. Retrying in {delay}s... (attempt {attempt + 1}/{max_retries})", file=sys.stderr)
                        time.sleep(delay)
                        continue
                    else:
                        raise RuntimeError(f"OpenRouter server error {e.code} after {max_retries} attempts")
                else:
                    raise  # Re-raise other HTTP errors
            except Exception as e:
                # Other errors (network, timeout, etc.)
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    print(f"Request failed: {e}. Retrying in {delay}s... (attempt {attempt + 1}/{max_retries})", file=sys.stderr)
                    time.sleep(delay)
                    continue
                else:
                    raise RuntimeError(f"OpenRouter request failed after {max_retries} attempts: {e}")

        raise RuntimeError("Max retries exceeded")
