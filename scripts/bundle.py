#!/usr/bin/env python3
"""Build a standalone executable for the Coding Router application.

The resulting binary can be double-clicked or run from a terminal without a
pre-installed Python interpreter:

    ./dist/coding-router/coding-router          (onedir)
    ./dist/coding-router.app                    (macOS .app, if built with --windowed)

Run this script from the project root after building the frontend assets.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"


def main() -> None:
    if not (ROOT / "router" / "app" / "static" / "index.html").exists():
        print("Frontend assets not found. Build them first with:")
        print("  cd frontend && npm run build")
        raise SystemExit(1)

    pyinstaller = shutil.which("pyinstaller") or (
        Path(sys.executable).parent / "pyinstaller"
    )
    if not pyinstaller or not Path(pyinstaller).exists():
        print("PyInstaller not found. Install it with:")
        print(f"  {sys.executable} -m pip install pyinstaller")
        raise SystemExit(1)

    # Ensure the dist directory exists.
    DIST.mkdir(exist_ok=True)

    name = "coding-router"
    # Onedir build is generally faster and easier to inspect.
    cmd = [
        pyinstaller,
        "--noconfirm",
        "--onedir",
        "--name", name,
        "--add-data", f"{ROOT / 'router' / 'app' / 'static'}{os.pathsep}router/app/static",
        "--add-data", f"{ROOT / 'config.example.toml'}{os.pathsep}.",
        "--hidden-import", "uvicorn.logging",
        "--hidden-import", "uvicorn.loops",
        "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.protocols",
        "--hidden-import", "uvicorn.protocols.http",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.lifespan",
        "--hidden-import", "uvicorn.lifespan.on",
        str(ROOT / "scripts" / "bundle_entry.py"),
    ]

    print("Running:", " ".join(str(c) for c in cmd))
    subprocess.run([str(c) for c in cmd], cwd=ROOT, check=True)

    print(f"\nBundle created at: {DIST / name}")
    print(f"Run it with: ./{DIST / name / name}")


if __name__ == "__main__":
    main()
