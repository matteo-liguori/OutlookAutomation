"""The one gate every builder must pass before reporting.

    python -m chamberlain.dev check     format check, lint, types, the add-on's types, tests
    python -m chamberlain.dev fix       apply the formatter and safe lint fixes

Contract: docs/contracts/10-targets.md (Engineering).
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ADDON_DIR = ROOT / "src" / "chamberlain" / "connectors" / "thunderbird" / "addon"
ADDON_STEP = "@addon"
"""Marks the Node step: `npm ci` when needed, then the add-on's TypeScript check."""

CHECK_STEPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("format", ("ruff", "format", "--check", ".")),
    ("lint", ("ruff", "check", ".")),
    ("types", ("pyright",)),
    ("addon", (ADDON_STEP,)),
    ("tests", ("pytest",)),
)

FIX_STEPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("format", ("ruff", "format", ".")),
    ("lint", ("ruff", "check", "--fix", ".")),
)


def run_addon_step() -> int:
    """Install the add-on's pinned toolchain if absent and type-check it (Node is required)."""
    npm = shutil.which("npm")
    if npm is None:
        if os.environ.get("CHAMBERLAIN_ALLOW_NO_NODE") == "1":
            print("npm is missing; skipped (CHAMBERLAIN_ALLOW_NO_NODE=1)", flush=True)
            return 0
        print("Node.js and npm are required for the Thunderbird add-on", flush=True)
        return 1
    if not (ADDON_DIR / "node_modules").exists():
        installed = subprocess.run(
            [npm, "ci", "--no-audit", "--no-fund"], cwd=ADDON_DIR, check=False
        )
        if installed.returncode != 0:
            return installed.returncode
    return subprocess.run(
        [npm, "run", "typecheck"],
        cwd=ADDON_DIR,
        env={**os.environ, "CHAMBERLAIN_PYTHON": sys.executable},
        check=False,
    ).returncode


def run_steps(steps: Sequence[tuple[str, tuple[str, ...]]], keep_going: bool) -> int:
    """Run each step as `python -m <tool> ...` from the repository root."""
    failed: list[str] = []
    for name, command in steps:
        started = time.monotonic()
        print(f"== {name}: {' '.join(command)}", flush=True)
        if command == (ADDON_STEP,):
            code = run_addon_step()
        else:
            code = subprocess.run(
                [sys.executable, "-m", *command], cwd=ROOT, check=False
            ).returncode
        seconds = time.monotonic() - started
        status = "ok" if code == 0 else "FAILED"
        print(f"== {name}: {status} ({seconds:.1f}s)", flush=True)
        if code != 0:
            failed.append(name)
            if not keep_going:
                break
    if failed:
        print(f"GATE FAILED: {', '.join(failed)}", flush=True)
        return 1
    print("GATE OK", flush=True)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m chamberlain.dev")
    parser.add_argument("command", choices=("check", "fix"))
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="run every step even after one fails",
    )
    args = parser.parse_args(argv)
    steps = CHECK_STEPS if args.command == "check" else FIX_STEPS
    return run_steps(steps, keep_going=args.keep_going)


if __name__ == "__main__":
    raise SystemExit(main())
