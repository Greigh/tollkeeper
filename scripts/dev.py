#!/usr/bin/env python3
"""Start the Coding Router backend and Vite dev frontend together.

The backend runs on 127.0.0.1:8080. The Vite dev server proxies /api to it
and serves the React app on 127.0.0.1:5173. Press Ctrl+C once to stop both.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def stream_output(prefix: str, pipe) -> None:
    for line in pipe:
        print(f"[{prefix}] {line}", end="", flush=True)


def main() -> None:
    backend_cmd = [
        sys.executable, "-m", "uvicorn",
        "router.app.api:app",
        "--host", "127.0.0.1", "--port", "8080",
        # Reload backend code on change; frontend HMR is handled by Vite.
        "--reload", "--reload-dir", os.path.join(ROOT, "router"),
    ]
    frontend_cmd = ["npm", "run", "dev"]

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    backend = subprocess.Popen(
        backend_cmd,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )
    frontend = subprocess.Popen(
        frontend_cmd,
        cwd=os.path.join(ROOT, "frontend"),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )

    def stop(*_):
        backend.terminate()
        frontend.terminate()
        try:
            backend.wait(timeout=5)
            frontend.wait(timeout=5)
        except subprocess.TimeoutExpired:
            backend.kill()
            frontend.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    threading.Thread(target=stream_output, args=("backend", backend.stdout), daemon=True).start()
    threading.Thread(target=stream_output, args=("frontend", frontend.stdout), daemon=True).start()

    print("Coding Router dev runner started.")
    print("  Backend:  http://127.0.0.1:8080")
    print("  Frontend: http://127.0.0.1:5173")
    print("  Press Ctrl+C to stop both.\n", flush=True)

    backend.wait()
    frontend.wait()


if __name__ == "__main__":
    main()
