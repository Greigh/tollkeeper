"""Detect locally installed desktop applications without executing them.

Used as a fallback when a CLI tool is not on PATH. Each adapter can declare
common install locations for its GUI counterpart; this module only checks
file existence, so it is safe and side-effect free.
"""
from __future__ import annotations

import os
import platform
from pathlib import Path


SYSTEM = platform.system()


def _home() -> Path:
    return Path.home()


def _app_dirs() -> list[Path]:
    """Return common application install directories for the current platform."""
    if SYSTEM == "Darwin":
        return [Path("/Applications"), _home() / "Applications"]
    if SYSTEM == "Windows":
        program_files = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")]
        return [Path(p) for p in program_files if p]
    # Linux and other Unix-likes
    return [
        Path("/usr/share/applications"),
        Path("/usr/local/share/applications"),
        _home() / ".local" / "share" / "applications",
    ]


def _macos_app(name: str) -> Path | None:
    for apps in _app_dirs():
        path = apps / f"{name}.app"
        if path.is_dir():
            return path
    return None


def _windows_exe(names: tuple[str, ...]) -> Path | None:
    for apps in _app_dirs():
        for name in names:
            for ext in (".exe", ".bat", ".cmd"):
                path = apps / f"{name}{ext}"
                if path.is_file():
                    return path
    return None


def _linux_desktop(names: tuple[str, ...]) -> Path | None:
    for apps in _app_dirs():
        for name in names:
            path = apps / f"{name}.desktop"
            if path.is_file():
                return path
    return None


_DESKTOP_LOCATIONS: dict[str, tuple[tuple[str, ...], ...]] = {
    # macOS app bundle names, Windows base names, Linux desktop entry names
    "claude": (("Claude",), ("Claude",), ("claude",)),
    "cursor": (("Cursor", "Cursor Nightly"), ("Cursor",), ("cursor", "cursor-nightly")),
    "gemini": (("Gemini",), ("Gemini",), ("gemini",)),
    "devin": (("Devin Desktop", "Devin"), ("Devin Desktop", "Devin"), ("devin-desktop", "devin")),
    "codex": (("Codex",), ("Codex",), ("codex",)),
    "perplexity": (("Perplexity",), ("Perplexity",), ("perplexity", "pplx")),
    "zed": (("Zed",), ("Zed",), ("zed",)),
    "grok": (("Grok",), ("Grok",), ("grok",)),
}


def find_desktop_app(adapter_name: str) -> Path | None:
    """Return the path to an installed desktop app for an adapter, if found."""
    names = _DESKTOP_LOCATIONS.get(adapter_name)
    if not names:
        return None
    macos, windows, linux = names
    if SYSTEM == "Darwin":
        for app_name in macos:
            found = _macos_app(app_name)
            if found:
                return found
    elif SYSTEM == "Windows":
        return _windows_exe(windows)
    else:
        return _linux_desktop(linux)
    return None
