"""Compatibility entry point for the native macOS Outlook adapter."""
from monitor.adapters.macos.native import SCRIPT, execute, request_for, main

if __name__ == "__main__":
    raise SystemExit(main())
