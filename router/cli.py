"""Thin CLI client over router_core. No business logic lives here."""
from __future__ import annotations

import argparse
import tomllib
from pathlib import Path

from . import __version__
from .core.adapter import RunRequest
from .core.policy import RoutingDecision, classify, decide
from .core.ledger import log_run, spend_summary, today_spend
from .core import credentials
from .core.capsule import list_capsules, resume_brief, write_capsule
from .adapters.openrouter import OpenRouterAdapter
from .adapters.subscriptions import (ClaudeCodeAdapter, CopilotAdapter, CursorAdapter,
                                     GeminiAdapter, DevinAdapter, CodexAdapter,
                                     PerplexityAdapter, ZedAdapter, ZcodeAdapter,
                                     DeepSeekAdapter, XAIAdapter, AmpAdapter,
                                     KimiAdapter, MiniMaxAdapter)
from .dashboard import start_dashboard


def load_config() -> dict:
    """CLI config loader (legacy path; RouterService has its own)."""
    p = Path.home() / ".config" / "router" / "config.toml"
    return tomllib.loads(p.read_text()) if p.exists() else {}


def load_routing_table() -> list | None:
    """Load routing table from config file."""
    cfg = load_config()
    routes = cfg.get("routing", {}).get("routes", [])
    return routes if routes else None


def build_adapters(api_key: str | None = None):
    """Instantiate every provider adapter; ``api_key`` feeds OpenRouter."""
    return [ClaudeCodeAdapter(), CursorAdapter(), GeminiAdapter(), DevinAdapter(),
            CodexAdapter(), CopilotAdapter(), PerplexityAdapter(), ZedAdapter(),
            ZcodeAdapter(), DeepSeekAdapter(), XAIAdapter(), AmpAdapter(),
            KimiAdapter(), MiniMaxAdapter(), OpenRouterAdapter(api_key)]


# -- status ---------------------------------------------------------------
def cmd_status(_args) -> None:
    """`router status` — print reachability + quota detail per adapter."""
    print(f"{'ADAPTER':<14}{'KIND':<13}{'REACHABLE':<11}DETAIL")
    for a in build_adapters():
        h = a.health()
        quota_detail = h.quota.detail
        if h.quota.remaining_percent is not None:
            quota_detail += f" ({h.quota.remaining_percent:g}% remaining, vendor reported)"
        if h.quota.reset_at:
            reset_str = h.quota.reset_at if isinstance(h.quota.reset_at, str) else h.quota.reset_at.isoformat()
            quota_detail += f" (resets {reset_str})"
        if h.note:  # Show version warnings
            quota_detail += f" [{h.note}]"
        print(f"{h.name:<14}{a.kind:<13}{str(h.reachable):<11}{quota_detail}")


# -- run ------------------------------------------------------------------
def _forced_decision(args, adapters, task: str) -> RoutingDecision:
    by_name = {a.name: a for a in adapters}
    if args.adapter not in by_name:
        raise SystemExit(f"unknown adapter {args.adapter!r} "
                         f"(have: {sorted(by_name)})")
    return RoutingDecision(task, classify(task), args.adapter,
                           args.model or "default",
                           f"forced via --adapter {args.adapter}", 0.0,
                           dry_run=args.dry_run)


def cmd_run(args) -> None:
    """`router run` — route the task, enforce the metered cap, execute, log, capsule."""
    cfg = load_config()
    adapters = build_adapters(args.api_key)
    by_name = {a.name: a for a in adapters}
    or_adapter = by_name["openrouter"]

    # Handle resume from capsule
    task = args.task
    if args.resume:
        brief = resume_brief(args.resume)
        task = f"{brief}\n\nContinue with: {args.task}"
        print(f"[Resuming from capsule: {args.resume}]")

    if args.adapter:
        d = _forced_decision(args, adapters, task)
    else:
        # Load routing table for policy-based routing
        routing_table = load_routing_table()
        d = decide(task, adapters, or_adapter.catalog(), routing_table=routing_table)
        d.dry_run = args.dry_run
        if args.model:  # --model overrides the policy's pick
            d.model = args.model
            d.est_cost_usd = or_adapter.estimate_cost_usd(2000, 1000, args.model)
            d.reason += f" (model overridden to {args.model})"

    print(f"task class : {d.task_class}")
    print(f"route      : {d.adapter} / {d.model}")
    if d.route_source == "routing_table":
        print(f"route via  : routing table")
    print(f"reason     : {d.reason}")
    print(f"est. cost  : ${d.est_cost_usd:.4f}")

    if d.dry_run:
        log_run(task=task, task_class=d.task_class, adapter=d.adapter,
                model=d.model, dry_run=True, est_cost_usd=d.est_cost_usd)
        print("(dry run -- logged, nothing executed)")
        return

    adapter = by_name[d.adapter]
    if not adapter.is_available():
        print(f"adapter {d.adapter} is not available")
        return

    if d.adapter == "openrouter":
        cap = float(cfg.get("policy", {}).get("daily_cap_usd", 5.0))
        spent = today_spend()
        if spent >= cap:
            print(f"daily cap ${cap:.2f} reached (spent ${spent:.4f}) -- refusing.")
            return

    print("executing...\n")
    req = RunRequest(prompt=task, model=d.model)
    result = adapter.run(req)
    print(result.output)

    if d.adapter == "openrouter":
        cap = float(cfg.get("policy", {}).get("daily_cap_usd", 5.0))
        spent = today_spend()
        print(f"\nactual cost: ${result.cost_usd:.4f}  (today ${spent + result.cost_usd:.4f} / cap ${cap:.2f})")
        log_run(task=task, task_class=d.task_class, adapter=d.adapter,
                model=d.model, dry_run=False, est_cost_usd=d.est_cost_usd,
                actual_cost_usd=result.cost_usd)
    else:
        if result.depleted_mid_run:
            print(f"\n[quota depleted mid-run - creating capsule for potential re-route]")
            # Create capsule with depletion trigger
            capsule_path = write_capsule(
                task, d, result.output,
                handoff_notes="Quota was depleted during execution. Resume with next available adapter.",
                triggered_by="depletion"
            )
            print(f"capsule: {capsule_path}")
            log_run(task=task, task_class=d.task_class, adapter=d.adapter,
                    model=d.model, dry_run=False, est_cost_usd=d.est_cost_usd,
                    actual_cost_usd=0.0)
            return
        print(f"\ncost: $0.00 (subscription)")
        log_run(task=task, task_class=d.task_class, adapter=d.adapter,
                model=d.model, dry_run=False, est_cost_usd=d.est_cost_usd,
                actual_cost_usd=0.0)

    print(f"capsule: {write_capsule(task, d, result.output)}")


# -- spend ----------------------------------------------------------------
def cmd_spend(args) -> None:
    """`router spend` — print per-adapter spend totals from the ledger."""
    rows = spend_summary(args.days)
    if not rows:
        print("no runs logged yet.")
        return
    print(f"{'ADAPTER':<14}{'MODEL':<42}{'RUNS':<6}{'ACTUAL $':<10}EST $")
    tot_a = tot_e = 0.0
    for adapter, model, n, actual, est in rows:
        actual, est = actual or 0.0, est or 0.0
        tot_a += actual
        tot_e += est
        print(f"{adapter:<14}{model[:40]:<42}{n:<6}{actual:<10.4f}{est:.4f}")
    print(f"\n{args.days}d totals: actual ${tot_a:.4f} / estimated ${tot_e:.4f}")


# -- cred -------------------------------------------------------------------
def _mask(value: str) -> str:
    if len(value) <= 8:
        return "•" * len(value)
    return value[:4] + "•" * (len(value) - 8) + value[-4:]


def cmd_cred(args) -> None:
    """`router cred` — manage OS-keychain secrets (masked reads, env override hint)."""
    if args.action == "backend":
        print(credentials.backend_name())
        return
    if args.action in ("get", "set", "delete", "env") and not (args.service and args.account):
        raise SystemExit(f"cred {args.action} needs <service> <account>")
    if args.action == "env":
        print(f"export {credentials._env_name(args.service, args.account)}=<secret>")
        return
    if args.action == "get":
        value = credentials.get(args.service, args.account)
        if value is None:
            print("not stored")
            return
        print(value if args.show else _mask(value))
        return
    if args.action == "set":
        secret = args.secret
        if secret is None:
            import getpass
            import sys
            if sys.stdin.isatty():
                secret = getpass.getpass(f"{args.service}/{args.account} secret: ")
            else:
                secret = sys.stdin.read().strip()
        if not secret:
            raise SystemExit("no secret provided")
        if credentials.set(args.service, args.account, secret):
            print(f"stored {args.service}/{args.account} in {credentials.backend_name()}")
        else:
            print("store failed — use the ROUTER_CRED_* env override instead")
        return
    if args.action == "delete":
        if credentials.delete(args.service, args.account):
            print(f"deleted {args.service}/{args.account}")
        else:
            print("delete failed")
        return


# -- dashboard --------------------------------------------------------------
def cmd_dashboard(args) -> None:
    """`router dashboard` — serve the legacy dashboard."""
    port = args.port if args.port else 8080
    start_dashboard(port)


def cmd_app(args) -> None:
    """`router app` — serve the React application UI."""
    from .app.api import start_app
    start_app(port=args.port, open_browser=not args.no_open)


# -- capsule --------------------------------------------------------------
def cmd_capsule(args) -> None:
    """`router capsule` — list/write/show session capsules."""
    if args.list:
        capsules = list_capsules()
        if not capsules:
            print("no capsules found.")
            return
        print(f"{'TASK':<40}{'ADAPTER':<12}{'TRIGGERED BY':<12}{'CREATED AT'}")
        for c in capsules:
            print(f"{c['task'][:40]:<40}{c['adapter']:<12}{c['triggered_by']:<12}{c['created_at']}")
    elif args.show:
        print(resume_brief(args.show))
    elif args.task:
        print(f"capsule: {write_capsule(args.task)}")
    else:
        raise SystemExit("capsule needs --list, --task, or --show FILE")


def main(argv=None) -> None:
    """Parse argv and dispatch to the subcommand handler."""
    p = argparse.ArgumentParser(prog="router",
                                description="cost-aware coding router (M0/M1 spike)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="probe adapter health")
    s.set_defaults(fn=cmd_status)

    r = sub.add_parser("run", help="route (and optionally execute) a task")
    r.add_argument("--task", required=True, help="the task in plain words")
    r.add_argument("--dry-run", action="store_true",
                   help="print the routing decision, log it, execute nothing")
    r.add_argument("--adapter", help="force an adapter, skipping policy")
    r.add_argument("--model", help="force an OpenRouter model id")
    r.add_argument("--api-key", help="OpenRouter key (else OPENROUTER_API_KEY)")
    r.add_argument("--resume", help="resume from a capsule file path")
    r.set_defaults(fn=cmd_run)

    sp = sub.add_parser("spend", help="spend summary from the local ledger")
    sp.add_argument("--days", type=int, default=30)
    sp.set_defaults(fn=cmd_spend)

    d = sub.add_parser("dashboard", help="start legacy web dashboard")
    d.add_argument("--port", type=int, default=8080, help="port to serve on (default: 8080)")
    d.set_defaults(fn=cmd_dashboard)

    application = sub.add_parser("app", help="start the Tollkeeper application")
    application.add_argument("--port", type=int, default=8080, help="port to serve on (default: 8080)")
    application.add_argument("--no-open", action="store_true", help="do not open a browser")
    application.set_defaults(fn=cmd_app)

    c = sub.add_parser("capsule", help="session capsules")
    c.add_argument("--list", action="store_true", help="list all capsules")
    c.add_argument("--task", help="write a blank capsule for a task")
    c.add_argument("--show", help="print the resume brief for a capsule file")
    c.set_defaults(fn=cmd_capsule)

    cr = sub.add_parser("cred", aliases=["secrets"],
                        help="OS keychain secrets (cookies, tokens)")
    cr.add_argument("action",
                    choices=["get", "set", "delete", "backend", "env"])
    cr.add_argument("service", nargs="?", default="",
                    help="e.g. perplexity, zed, amp, gemini")
    cr.add_argument("account", nargs="?", default="",
                    help="e.g. session_cookie, editor_token")
    cr.add_argument("--secret", help="secret value (else prompt/stdin)")
    cr.add_argument("--show", action="store_true",
                    help="print the raw secret on get (default: masked)")
    cr.set_defaults(fn=cmd_cred)

    args = p.parse_args(argv)
    args.fn(args)
