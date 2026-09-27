#!/usr/bin/env python3
"""Entry point for the PyInstaller standalone bundle.

This file is not imported during normal development; it only exists so
PyInstaller has a single executable entry point that starts the application.
"""
from router.app.api import main_app

if __name__ == "__main__":
    main_app()
