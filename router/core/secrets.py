"""Persistent, local-only secret storage for application-managed API keys.

Secrets are stored in a JSON file under the user's config directory with
restricted filesystem permissions (0600). They are loaded into environment
variables at startup so adapters can consume them the same way they consume
secrets supplied directly via the shell.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


_SECRET_FILE: Path | None = None


def _secrets_file() -> Path:
    global _SECRET_FILE
    if _SECRET_FILE is None:
        base = Path.home() / ".config" / "router"
        base.mkdir(parents=True, exist_ok=True)
        _SECRET_FILE = base / "secrets.json"
    return _SECRET_FILE


def load_secrets() -> dict[str, str]:
    """Load persisted secrets into os.environ and return them."""
    path = _secrets_file()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}
    if isinstance(data, dict):
        for name, value in data.items():
            if isinstance(name, str) and isinstance(value, str):
                os.environ[name] = value
        return data
    return {}


def get_secret(name: str) -> str | None:
    """Return a secret, preferring the environment, then the persisted store."""
    env_value = os.environ.get(name)
    if env_value:
        return env_value
    path = _secrets_file()
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None
    return data.get(name) if isinstance(data, dict) else None


def set_secret(name: str, value: str) -> None:
    """Persist a secret. The file is created with 0600 permissions."""
    path = _secrets_file()
    data: dict[str, str] = {}
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                data = {k: v for k, v in loaded.items() if isinstance(k, str) and isinstance(v, str)}
        except (json.JSONDecodeError, OSError):
            data = {}
    data[name] = value
    os.environ[name] = value
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.chmod(path, 0o600)


def delete_secret(name: str) -> None:
    """Remove a persisted secret."""
    path = _secrets_file()
    if not path.exists():
        return
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return
    if isinstance(data, dict) and name in data:
        del data[name]
        os.environ.pop(name, None)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)


def list_secrets() -> dict[str, str]:
    """Return all persisted secrets with values masked for display."""
    path = _secrets_file()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}

    def mask(value: str) -> str:
        """Show only the first/last 4 chars; fully mask short values."""
        if len(value) <= 8:
            return "•" * len(value)
        return value[:4] + "•" * 8 + value[-4:]

    return {name: mask(value) for name, value in data.items() if isinstance(name, str) and isinstance(value, str)}
