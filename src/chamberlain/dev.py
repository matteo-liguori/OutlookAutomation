"""The one gate every builder must pass before reporting.

    python -m chamberlain.dev check     format check, lint, types, tests
    python -m chamberlain.dev fix       apply the formatter and safe lint fixes

Contract: docs/contracts/10-targets.md (Engineering).
"""

import argparse
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

CHECK_STEPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("format", ("ruff", "format", "--check", ".")),
    ("lint", ("ruff", "check", ".")),
    ("types", ("pyright",)),
    ("tests", ("pytest",)),
)

FIX_STEPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("format", ("ruff", "format", ".")),
    ("lint", ("ruff", "check", "--fix", ".")),
)


def run_steps(steps: Sequence[tuple[str, tuple[str, ...]]], keep_going: bool) -> int:
    """Run each step as `python -m <tool> ...` from the repository root."""
    failed: list[str] = []
    for name, command in steps:
        started = time.monotonic()
        print(f"== {name}: {' '.join(command)}", flush=True)
        result = subprocess.run([sys.executable, "-m", *command], cwd=ROOT, check=False)
        seconds = time.monotonic() - started
        status = "ok" if result.returncode == 0 else "FAILED"
        print(f"== {name}: {status} ({seconds:.1f}s)", flush=True)
        if result.returncode != 0:
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
