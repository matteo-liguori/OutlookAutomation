"""The command line: a thin face over `App.call`.

Contract: docs/contracts/02-tools.md (T-14, T-16). No business logic here.

    chamberlain tools [--json]          list the twelve tools
    chamberlain schema <tool>           print a tool's request and response schema
    chamberlain prompt                  print the standing prompt
    chamberlain tick                    run one tick for the operating system scheduler
    chamberlain viewer [--port N] [--no-open]
                                        serve the local viewer on 127.0.0.1 (never prints the token)
    chamberlain doctor [--metrics]      health checks, and the figures of 10-targets.md
    chamberlain <tool> --json '{...}'   call a tool and print the envelope
"""

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from chamberlain import __version__
from chamberlain.app.wiring import build_app, run_tick
from chamberlain.runs.plan import due_passes
from chamberlain.schema.base import Model, ToolName
from chamberlain.schema.envelope import Reply, error_reply
from chamberlain.schema.errors import ChamberlainError, ErrorCategory, ErrorCode
from chamberlain.schema.registry import BY_NAME, STANDING_PROMPT, TOOLS, get_tool
from chamberlain.schema.tools import DoctorCheck
from chamberlain.surface.viewer.server import launch

DATA_ENV = "CHAMBERLAIN_DATA"
SESSION_ENV = "CHAMBERLAIN_SESSION"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INVALID = 2
EXIT_UNCERTAIN = 3


def exit_code(reply: Reply[Model]) -> int:
    """0 ok, 2 invalid request, 3 uncertain, 1 otherwise (T-14)."""
    if reply.error is None:
        return EXIT_OK
    if reply.error.category is ErrorCategory.INVALID_REQUEST:
        return EXIT_INVALID
    if reply.error.category is ErrorCategory.UNCERTAIN:
        return EXIT_UNCERTAIN
    return EXIT_FAILED


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chamberlain", description="Chamberlain command line.")
    parser.add_argument("--version", action="version", version=f"chamberlain {__version__}")
    parser.add_argument("--data", help=f"data root (default: ${DATA_ENV})")
    commands = parser.add_subparsers(dest="command", required=True)
    tools = commands.add_parser("tools", help="list the tools")
    tools.add_argument("--json", action="store_true", dest="as_json")
    schema = commands.add_parser("schema", help="print a tool's schema")
    schema.add_argument("tool", choices=sorted(BY_NAME))
    commands.add_parser("prompt", help="print the standing prompt")
    commands.add_parser(
        "tick", help="run one tick (exit status 10: events wait and no session is active)"
    )
    viewer = commands.add_parser(
        "viewer", help="serve the local viewer on 127.0.0.1 and open it in the browser"
    )
    viewer.add_argument("--port", type=int, default=0, help="port (default: a random free one)")
    viewer.add_argument("--no-open", action="store_true", help="do not open the browser")
    for spec in TOOLS:
        tool = commands.add_parser(spec.name.value, help=spec.description)
        tool.add_argument("--json", dest="request", default="{}", help="the request as JSON")
        tool.add_argument("--session", help=f"session handle (default: ${SESSION_ENV})")
        if spec.name is ToolName.DOCTOR:
            tool.add_argument("--metrics", action="store_true", help="include the target figures")
            tool.add_argument("--check", choices=[c.value for c in DoctorCheck])
    return parser


def _print(out: TextIO, value: Any) -> None:
    out.write(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def _call_tool(args: argparse.Namespace, out: TextIO) -> int:
    try:
        raw: Any = json.loads(args.request)
        if not isinstance(raw, dict):
            raise ValueError("not an object")
    except ValueError:
        reply = error_reply(
            ChamberlainError(
                ErrorCode.REQUEST_INVALID,
                "The request is not a JSON object.",
                fix="Pass --json with one JSON object.",
            )
        )
        _print(out, reply.model_dump(mode="json", exclude_none=True))
        return exit_code(reply)
    request: dict[str, Any] = dict(raw)  # pyright: ignore[reportUnknownArgumentType]
    if getattr(args, "metrics", False):
        request["metrics"] = True
    if getattr(args, "check", None):
        request["check"] = args.check
    session = args.session or os.environ.get(SESSION_ENV)
    if session and "session" not in request:
        request["session"] = session
    data = args.data or os.environ.get(DATA_ENV)
    if not data:
        reply = error_reply(
            ChamberlainError(
                ErrorCode.CONFIG_INVALID,
                "No data root is set.",
                fix=f"Set {DATA_ENV} or pass --data before the tool name.",
            )
        )
    else:
        app = build_app(Path(data))
        try:
            reply = app.call(args.command, request)
        finally:
            app.close()
    _print(out, reply.model_dump(mode="json", exclude_none=True))
    return exit_code(reply)


def _tick(args: argparse.Namespace, out: TextIO) -> int:
    data = args.data or os.environ.get(DATA_ENV)
    if not data:
        reply = error_reply(
            ChamberlainError(
                ErrorCode.CONFIG_INVALID,
                "No data root is set.",
                fix=f"Set {DATA_ENV} or pass --data before the tool name.",
            )
        )
        _print(out, reply.model_dump(mode="json", exclude_none=True))
        return EXIT_FAILED
    app = build_app(Path(data))
    try:
        ran, wake = run_tick(app)
        due = due_passes(app.context)
    finally:
        app.close()
    _print(out, {"ran": ran, "wake": wake != 0, "due": due})
    return wake


def _viewer(args: argparse.Namespace, out: TextIO) -> int:
    """Serve the viewer. The token goes to the browser only, never to this output (G-18)."""
    data = args.data or os.environ.get(DATA_ENV)
    if not data:
        reply = error_reply(
            ChamberlainError(
                ErrorCode.CONFIG_INVALID,
                "No data root is set.",
                fix=f"Set {DATA_ENV} or pass --data before the tool name.",
            )
        )
        _print(out, reply.model_dump(mode="json", exclude_none=True))
        return EXIT_FAILED
    viewer = launch(Path(data), port=args.port, open_browser=not args.no_open, out=out)
    try:
        viewer.serve_forever()
    except KeyboardInterrupt:
        out.write("Viewer stopped." + chr(10))
    finally:
        viewer.close()
    return EXIT_OK


def main(argv: Sequence[str] | None = None, out: TextIO | None = None) -> int:
    stream = out or sys.stdout
    args = _parser().parse_args(argv)
    if args.command == "tools":
        if args.as_json:
            _print(stream, [{"name": s.name.value, "description": s.description} for s in TOOLS])
        else:
            for spec in TOOLS:
                stream.write(f"{spec.name.value:<10} {spec.description}\n")
        return EXIT_OK
    if args.command == "schema":
        spec = get_tool(args.tool)
        _print(stream, {"request": spec.input_schema(), "response": spec.output_schema()})
        return EXIT_OK
    if args.command == "prompt":
        stream.write(STANDING_PROMPT + "\n")
        return EXIT_OK
    if args.command == "tick":
        return _tick(args, stream)
    if args.command == "viewer":
        return _viewer(args, stream)
    return _call_tool(args, stream)


if __name__ == "__main__":
    raise SystemExit(main())
