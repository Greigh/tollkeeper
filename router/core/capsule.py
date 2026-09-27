"""Session capsules: freeze a run's context so it can resume after a quota hop."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def capsule_dir() -> Path:
    """Capsule storage directory under $ROUTER_HOME (created if missing)."""
    root = Path(os.environ.get("ROUTER_HOME", Path.home() / ".local" / "share" / "router"))
    path = root / "capsules"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _get_git_info() -> dict[str, str]:
    """Get current git branch and status if in a git repo."""
    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True
        ).strip()
        status = subprocess.check_output(
            ["git", "status", "--short"],
            stderr=subprocess.DEVNULL,
            text=True
        ).strip()
        return {"branch": branch, "status": status}
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {"branch": "unknown", "status": ""}


def write_capsule(
    task: str,
    decision=None,
    output: str = "",
    notes: str = "",
    plan: dict[str, Any] | None = None,
    decisions: list[dict[str, str]] | None = None,
    code_state: dict[str, Any] | None = None,
    conversation_digest: str = "",
    handoff_notes: str = "",
    triggered_by: str = "manual"
) -> Path:
    """Write a session capsule with full context for resumption."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    path = capsule_dir() / f"capsule-{ts}.json"

    git_info = _get_git_info()

    data = {
        "capsule_version": 1,
        "task": {
            "summary": task,
            "type": getattr(decision, "task_class", "unknown"),
            "complexity": getattr(decision, "task_class", "unknown"),
        },
        "plan": plan or {
            "goal": "",
            "todos": [],
            "current_step": 0,
        },
        "decisions": decisions or [],
        "code_state": code_state or {
            "branch": git_info["branch"],
            "files_touched": [],
            "diff_summary": git_info["status"] or "no changes",
            "tests_status": "not run",
        },
        "route": {
            "adapter": getattr(decision, "adapter", None),
            "model": getattr(decision, "model", None),
        },
        "reason": getattr(decision, "reason", ""),
        "est_cost_usd": getattr(decision, "est_cost_usd", None),
        "output_head": (output or "")[:2000],
        "full_output": output,
        "conversation_digest": conversation_digest,
        "handoff_notes": handoff_notes,
        "created_by": getattr(decision, "adapter", "unknown"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "triggered_by": triggered_by,  # "manual", "depletion", "downgrade"
    }

    path.write_text(json.dumps(data, indent=2))
    return path


def resume_brief(path: str | Path) -> str:
    """Generate a human-readable resume brief from a capsule."""
    d = json.loads(Path(path).read_text())

    # Handle both old format (task as string) and new format (task as dict)
    task_data = d.get("task", {})
    if isinstance(task_data, str):
        task_summary = task_data
        task_type = d.get("task_class", "unknown")
    else:
        task_summary = task_data.get("summary", "unknown")
        task_type = task_data.get("type", "unknown")

    plan = d.get("plan", {})
    decisions = d.get("decisions", [])
    code_state = d.get("code_state", {})
    route = d.get("route", {})
    created_by = d.get("created_by", route.get("adapter", "unknown"))
    created_at = d.get("created_at", d.get("created", ""))

    # Build todos display
    todos = plan.get("todos", [])
    current_step = plan.get("current_step", 0)
    done_todos = [t["text"] for i, t in enumerate(todos) if i < current_step]
    active_todo = todos[current_step]["text"] if current_step < len(todos) else "none"
    pending_todos = [t["text"] for i, t in enumerate(todos) if i > current_step]

    brief = f"""## Resumed session (capsule from {created_by}, {created_at})
**Task:** {task_summary}  |  **Type:** {task_type}  |  **Step {current_step}/{len(todos)}**
**Goal:** {plan.get('goal', 'no goal specified')}
**Done:** {done_todos if done_todos else 'none'}   **Now:** {active_todo}   **Next:** {pending_todos if pending_todos else 'none'}
**Key decisions:** {', '.join([f"{d['decision']} ({d['reason']})" for d in decisions]) if decisions else 'none'}
**Code state:** branch {code_state.get('branch', 'unknown')}; touched {', '.join(code_state.get('files_touched', [])) or 'none'}; {code_state.get('diff_summary', 'no changes')}; tests: {code_state.get('tests_status', 'unknown')}
**Digest:** {d.get('conversation_digest', 'no digest available')}
**Instructions:** {d.get('handoff_notes', 'Continue from where the previous adapter left off. Do not redo completed steps.')}
"""

    return brief


def list_capsules() -> list[dict[str, Any]]:
    """List all available capsules with metadata."""
    capsules = []
    for path in capsule_dir().glob("capsule-*.json"):
        try:
            data = json.loads(path.read_text())
            # Handle both old format (task as string) and new format (task as dict)
            task_data = data.get("task", {})
            if isinstance(task_data, str):
                task_summary = task_data
            else:
                task_summary = task_data.get("summary", "unknown")

            capsules.append({
                "path": str(path),
                "task": task_summary,
                "adapter": data.get("created_by", data.get("route", {}).get("adapter", "unknown")),
                "created_at": data.get("created_at", data.get("created", "")),
                "triggered_by": data.get("triggered_by", "manual"),
            })
        except (json.JSONDecodeError, IOError):
            continue
    return sorted(capsules, key=lambda x: x["created_at"], reverse=True)
