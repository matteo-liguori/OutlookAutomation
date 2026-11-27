"""The command line: a thin face over `App.call`.

Contract: docs/contracts/02-tools.md (T-14, T-16). No business logic here.

    chamberlain tools [--json]          list the twelve tools
    chamberlain schema <tool>           print a tool's request and response schema
    chamberlain prompt                  print the standing prompt
    chamberlain tick [--quiet]          run one tick for the operating system scheduler
    chamberlain viewer [--port N] [--no-open | --print-url | --browser]
                                        serve the local viewer on 127.0.0.1 (never prints the token)
    chamberlain doctor [--metrics]      health checks, and the figures of 10-targets.md
    chamberlain config validate|show    check or print config/connections.yaml (no secrets)
    chamberlain config add-test-calendar <source>  enroll a configured calendar for live tests
    chamberlain import <old data root>  one-shot, read-only import of the old harness
    chamberlain recipients seed [--months N]
                                        learn known recipients from each Sent folder (read-only)
    chamberlain cleanup plan|grant|run   move old mail to Trash by the cleanup rules
    chamberlain export thread ID... [--file F] [--title T]
                                        one conversation as clean text + attachments (zip)
    chamberlain addon update [--restart] update the Thunderbird add-on by replacing its file
    chamberlain thunderbird close|start|status
                                        close it normally and wait (never a kill), or start it
    chamberlain schedule install|uninstall [--apply] [--every-minutes N]
                                        the hidden operating system tick (dry run without --apply)
    chamberlain schedule prompts        the agent-app automation text for each schedule
    chamberlain setup agent codex|claude [--apply] [--project DIR]
                                        connect the agent app to the MCP server
    chamberlain live-test [run|grant|sweep|report] [--only P] [--skip P] [--keep] [--dry-run]
                          [--retry RUN] [--workers N]
                                        exercise the harness against the real accounts
                                        (docs/live-test.md; off until live_test.enabled)
    chamberlain <tool> --json '{...}'   call a tool and print the envelope
"""

import argparse
import functools
import io
import json
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TextIO

from chamberlain import __version__
from chamberlain.app.wiring import run_tick
from chamberlain.cleanup.rules import only_source
from chamberlain.cleanup.run import propose_grants, run_cleanup
from chamberlain.connections import (
    build_runtime,
    load_connections,
    open_app,
    to_document,
)
from chamberlain.connections.load import add_test_calendar
from chamberlain.connections.setup import provider_setup
from chamberlain.connectors.caldav.setup import verify as icloud_verify
from chamberlain.connectors.thunderbird.update import (
    SystemDesktop,
    control,
    thunderbird_home,
    update_addon,
)
from chamberlain.dataroot import refuse_inside_checkout
from chamberlain.export.thread import export_thread, parse_ids
from chamberlain.importer.run import run_import
from chamberlain.livetest.commands import main as live_test_main
from chamberlain.livetest.runner import DEFAULT_WORKERS, RunOptions
from chamberlain.ports.clock import SystemClock
from chamberlain.runs.plan import due_passes
from chamberlain.schema.base import Model, ToolName
from chamberlain.schema.envelope import Reply, error_reply
from chamberlain.schema.errors import ChamberlainError, ErrorCategory, ErrorCode
from chamberlain.schema.registry import BY_NAME, STANDING_PROMPT, TOOLS, get_tool
from chamberlain.schema.tools import DoctorCheck
from chamberlain.surface.config_updates import config_migrate, updates
from chamberlain.surface.migration import migration_command, migration_parser
from chamberlain.surface.runner import DATA_ENV, configured_caller, data_root
from chamberlain.surface.schedule_install import (
    automation_prompts,
    default_target,
    install,
    uninstall,
)
from chamberlain.surface.viewer.server import launch
from chamberlain.writes.seed import DEFAULT_MONTHS, seed_all

SESSION_ENV = "CHAMBERLAIN_SESSION"
CLAUDE_CODE_ENV = "CLAUDECODE"
"""Set to 1 by Claude Code in the shells it starts."""

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INVALID = 2
EXIT_UNCERTAIN = 3

_Run = Callable[[Path, argparse.Namespace, TextIO], int]
_Command = Callable[[argparse.Namespace, TextIO], int]


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
    setup = commands.add_parser("setup", help="configure a calendar or Thunderbird connection")
    setup.add_argument(
        "provider",
        nargs="?",
        choices=["select", "status", "agent", "thunderbird", "google", "icloud"],
    )
    setup.add_argument("options", nargs=argparse.REMAINDER, help="provider setup options")
    config = commands.add_parser("config", help="check or manage the connection profile")
    config_actions = config.add_subparsers(dest="action", required=True)
    config_actions.add_parser("validate", help="check the connection profile")
    config_actions.add_parser("show", help="print the connection profile")
    migration = config_actions.add_parser(
        "migrate", help="preview or apply the local store upgrade"
    )
    migration.add_argument("--apply", action="store_true")
    commands.add_parser("status", help="list configured sources and connector capabilities")
    notes = commands.add_parser("updates", help="inspect local release information")
    tags = notes.add_subparsers(dest="action", required=True)
    tags.add_parser("notes", help="read an annotated release tag").add_argument("tag")
    migration_parser(commands.add_parser("migrate", help="move a v1 installation to v2"))
    enroll = config_actions.add_parser(
        "add-test-calendar", help="enroll an existing calendar for live tests"
    )
    enroll.add_argument("source", help="configured calendar source ID, such as calendar:work")
    cleanup = commands.add_parser(
        "cleanup", help="move old mail to Trash by the cleanup rules (plan, grant or run)"
    )
    cleanup.add_argument("action", choices=["plan", "grant", "run"])
    cleanup.add_argument("--source", help="apply the configured rules only to this mailbox")
    export = commands.add_parser(
        "export",
        help="a conversation as clean text plus attachments, zipped (exports/, kept a week)",
    )
    export.add_argument("what", choices=["thread"])
    export.add_argument("ids", nargs="*", help="Message-IDs or message handles (msg_...)")
    export.add_argument("--file", type=Path, help="a text file of Message-IDs, one per line")
    export.add_argument("--title", default="", help="the heading (default: the first subject)")
    export.add_argument(
        "--keep-quotes", action="store_true", help="keep quoted history in each message"
    )
    addon = commands.add_parser(
        "addon",
        help="update the Thunderbird add-on by replacing its file in the profile (not the UI)",
    )
    addon.add_argument("action", choices=["update"])
    addon.add_argument(
        "--restart",
        action="store_true",
        help="close Thunderbird normally first and start it again after",
    )
    importing = commands.add_parser(
        "import", help="import the old harness's data (the old folder is only read)"
    )
    importing.add_argument("old_root", help="the old data root (use a copy)")
    recipients = commands.add_parser(
        "recipients", help="known recipients: seed them from each mail source's Sent folder"
    )
    recipients.add_argument("action", choices=["seed"])
    recipients.add_argument(
        "--months", type=int, default=DEFAULT_MONTHS, help="how far back to read (default 24)"
    )
    live = commands.add_parser(
        "live-test", help="exercise the harness against the real accounts (docs/live-test.md)"
    )
    live.add_argument(
        "action", nargs="?", choices=["run", "grant", "sweep", "report"], default="run"
    )
    live.add_argument("--only", action="append", default=[], help="only scenarios matching this")
    live.add_argument("--skip", action="append", default=[], help="leave out scenarios matching")
    live.add_argument("--keep", action="store_true", help="leave the test items in place")
    live.add_argument(
        "--retry",
        dest="retry_from",
        metavar="RUN",
        help="run again only what failed or never ran in RUN (a run id, or latest)",
    )
    live.add_argument("--dry-run", action="store_true", help="show the plan and touch nothing")
    live.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"scenarios run side by side (default {DEFAULT_WORKERS}; 1 runs them one by one)",
    )
    live.add_argument(
        "--approve-calendar-edits",
        action="store_true",
        help="for this run, approve changing and deleting the calendar events it makes",
    )
    live.add_argument("--json", action="store_true", dest="as_json", help="also print the report")
    live.add_argument("--run", dest="report_run", help="report: the run id (default: the latest)")
    tick = commands.add_parser(
        "tick", help="run one tick (exit status 10: events wait and no session is active)"
    )
    tick.add_argument(
        "--quiet", action="store_true", help="print nothing (for pythonw, which has no console)"
    )
    box = commands.add_parser("thunderbird", help="close, start or check Thunderbird")
    box.add_argument("action", choices=["close", "start", "status"])
    sched = commands.add_parser(
        "schedule", help="install the operating system tick, or print the agent automations"
    )
    sched.add_argument("action", choices=["install", "uninstall", "prompts"])
    sched.add_argument("--apply", action="store_true", help="do it (default: show the plan)")
    sched.add_argument(
        "--every-minutes",
        type=int,
        default=None,
        help="tick interval in minutes (default: the profile's tick.every_minutes, 60)",
    )
    viewer = commands.add_parser(
        "viewer", help="serve the local viewer on 127.0.0.1 and open it in the browser"
    )
    viewer.add_argument("--port", type=int, default=0, help="port (default: a random free one)")
    viewer.add_argument("--no-open", action="store_true", help="do not open the browser")
    viewer.add_argument(
        "--print-url",
        action="store_true",
        help="do not open the system browser; print a ready-to-open address (the default under "
        "Claude Code, so the Claude app's built-in browser pane can open it)",
    )
    viewer.add_argument(
        "--browser",
        action="store_true",
        help="open the system browser even under Claude Code",
    )
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


def _fail(out: TextIO, error: ChamberlainError) -> int:
    _print(out, error_reply(error).model_dump(mode="json", exclude_none=True))
    return EXIT_FAILED


def _plain_fail(out: TextIO, error: ChamberlainError) -> int:
    out.write(f"{error.message} {error.fix}".rstrip() + "\n")
    return EXIT_FAILED


def _rooted(*, plain: bool = False, missing: int = EXIT_FAILED) -> Callable[[_Run], _Command]:
    """Resolve the data root once; no root exits `missing` (T-14), a raised error is JSON."""

    def wrap(run: _Run) -> _Command:
        @functools.wraps(run)
        def command(args: argparse.Namespace, out: TextIO) -> int:
            try:
                root = data_root(args.data)
            except ChamberlainError as error:
                if plain:
                    out.write(f"Set {DATA_ENV} or pass --data before the command.\n")
                else:
                    _fail(out, error)
                return missing
            try:
                return run(root, args, out)
            except ChamberlainError as error:
                return _fail(out, error)

        return command

    return wrap


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
    try:
        reply = configured_caller(data_root(args.data))(args.command, request)
    except ChamberlainError as error:
        reply = error_reply(error)
    _print(out, reply.model_dump(mode="json", exclude_none=True))
    return exit_code(reply)


@_rooted()
def _tick(root: Path, args: argparse.Namespace, out: TextIO) -> int:
    app = open_app(root)
    try:
        ran, wake = run_tick(app)
        due = due_passes(app.context)
    finally:
        app.close()
    if not args.quiet:
        _print(out, {"ran": ran, "wake": wake != 0, "due": due})
    return wake


def _thunderbird(args: argparse.Namespace, out: TextIO) -> int:
    status, line = control(args.action, SystemDesktop())
    out.write(line + "\n")
    return status


@_rooted(plain=True)
def _schedule(root: Path, args: argparse.Namespace, out: TextIO) -> int:
    try:
        if args.action == "prompts":
            lines = automation_prompts(root)
        elif args.action == "install":
            lines = install(
                default_target(root), every_minutes=args.every_minutes, apply=args.apply
            )
        else:
            lines = uninstall(default_target(root), apply=args.apply)
    except ChamberlainError as error:
        return _plain_fail(out, error)
    out.write(("\n\n" if args.action == "prompts" else "\n").join(lines) + "\n")
    return EXIT_OK


class _NoSecrets:
    """A credential store that holds nothing, so `config validate` never touches the real one."""

    def get(self, service: str, name: str) -> str | None:
        return None

    def set(self, service: str, name: str, value: str) -> None:
        raise ChamberlainError(ErrorCode.CONFIG_INVALID, "Validation does not save secrets.")

    def delete(self, service: str, name: str) -> bool:
        return False


@_rooted()
def _config(root: Path, args: argparse.Namespace, out: TextIO) -> int:
    if args.action == "migrate":
        return config_migrate(root, apply=args.apply, out=out)
    if args.action == "add-test-calendar":
        backup = add_test_calendar(root, args.source)
        result = {"ok": True, "source": args.source, "changed": backup is not None}
        if backup is not None:
            result["backup"] = str(backup)
        _print(out, result)
        return EXIT_OK
    connections = load_connections(root)
    if args.action == "show":
        _print(out, to_document(connections))
        return EXIT_OK
    runtime = build_runtime(connections, root, SystemClock(), secrets=_NoSecrets())
    _print(
        out,
        {
            "ok": True,
            "sources": {
                source_id: {"kind": source.kind, "enabled": source.enabled}
                for source_id, source in sorted(connections.sources.items())
            },
            "connectors": runtime.sources.source_ids(),
        },
    )
    return EXIT_OK


@_rooted()
def _import(root: Path, args: argparse.Namespace, out: TextIO) -> int:
    _print(out, run_import(Path(args.old_root), root, caldav_check=icloud_verify).summary())
    return EXIT_OK


@_rooted(plain=True)
def _cleanup(root: Path, args: argparse.Namespace, out: TextIO) -> int:
    """`plan` counts, `grant` proposes the owner's grant, `run` moves to Trash (docs/cleanup.md)."""
    app = None
    try:
        connections = load_connections(root)
        config = connections.cleanup
        if args.source:
            source = connections.sources.get(args.source)
            if not args.source.startswith("mail:") or source is None or not source.enabled:
                out.write("Choose a configured, enabled mailbox.\n")
                return EXIT_FAILED
            config = only_source(config, args.source)
        if not config.rules:
            if args.source:
                out.write(f"No cleanup rules apply to {args.source}.\n")
            else:
                out.write("No cleanup rules in connections.yaml (see docs/cleanup.md).\n")
            return EXIT_OK
        app = open_app(root)
        if args.action == "grant":
            for source, state, handle in propose_grants(app.context, config):
                out.write(f"{source}: {state} {handle}\n")
            out.write("Confirm each waiting grant in the viewer (chamberlain viewer).\n")
            return EXIT_OK
        report = run_cleanup(app.context, config, dry_run=args.action == "plan")
    except ChamberlainError as error:
        return _plain_fail(out, error)
    finally:
        if app is not None:
            app.close()
    out.write("\n".join(report.lines(dry_run=args.action == "plan")) + "\n")
    return EXIT_FAILED if report.failed else EXIT_OK


@_rooted(plain=True)
def _export(root: Path, args: argparse.Namespace, out: TextIO) -> int:
    """An owner utility: one conversation as a zip under exports/ (read-only on the mail)."""
    app = None
    try:
        raw = list(args.ids)
        if args.file is not None:
            raw += args.file.read_text(encoding="utf-8").splitlines()
        ids = parse_ids(raw)
        app = open_app(root)
        result = export_thread(
            app.context, ids, title=args.title, keep_quotes=args.keep_quotes, zone=None
        )
    except OSError as error:
        out.write(f"Cannot read {args.file}: {error.strerror or type(error).__name__}.\n")
        return EXIT_FAILED
    except ChamberlainError as error:
        return _plain_fail(out, error)
    finally:
        if app is not None:
            app.close()
    out.write("\n".join(result.lines()) + "\n")
    return EXIT_OK


@_rooted(plain=True)
def _addon(root: Path, args: argparse.Namespace, out: TextIO) -> int:
    """Build the add-on and put it in place of the installed file in every Thunderbird profile."""
    try:
        report = update_addon(
            root / "bridge", home=thunderbird_home(), desktop=SystemDesktop(), restart=args.restart
        )
    except ChamberlainError as error:
        return _plain_fail(out, error)
    out.write("\n".join(report.lines()) + "\n")
    return EXIT_OK


@_rooted()
def _recipients(root: Path, args: argparse.Namespace, out: TextIO) -> int:
    """Read each mail source's Sent folder and record its recipients as known (G-15)."""
    if not 1 <= args.months <= 120:
        raise ChamberlainError(
            ErrorCode.REQUEST_INVALID, "--months is between 1 and 120.", field="months"
        )
    app = open_app(root)
    try:
        reports = seed_all(
            app.context.db,
            dict(app.context.sources.mail),
            app.context.clock,
            months=args.months,
        )
    finally:
        app.close()
    _print(out, {"sources": [r.summary() for r in reports], "months": args.months})
    return EXIT_OK if reports and all(r.complete for r in reports) else EXIT_FAILED


@_rooted()
def _live_test(root: Path, args: argparse.Namespace, out: TextIO) -> int:
    options = RunOptions(
        only=tuple(args.only),
        skip=tuple(args.skip),
        keep=args.keep,
        dry_run=args.dry_run,
        approve_calendar_edits=args.approve_calendar_edits,
        retry_from=args.retry_from,
        workers=max(1, args.workers),
    )
    return live_test_main(
        args.action, root, options, out, as_json=args.as_json, run_id=args.report_run
    )


@_rooted()
def _viewer(root: Path, args: argparse.Namespace, out: TextIO) -> int:
    """Serve the viewer. The token goes to the browser only, never to this output (G-18)."""
    print_url = args.print_url or (
        os.environ.get(CLAUDE_CODE_ENV) == "1" and not args.browser and not args.no_open
    )
    viewer = launch(
        root,
        port=args.port,
        open_browser=not (args.no_open or print_url),
        print_url=print_url,
        out=out,
    )
    try:
        viewer.serve_forever()
    except KeyboardInterrupt:
        out.write("Viewer stopped." + chr(10))
    finally:
        viewer.close()
    return EXIT_OK


@_rooted(plain=True, missing=EXIT_INVALID)
def _status(root: Path, args: argparse.Namespace, out: TextIO) -> int:
    config = load_connections(root)
    ports = build_runtime(config, root, SystemClock()).sources
    bindings = {**ports.mail, **ports.calendar, **ports.contacts}
    sources: dict[str, Any] = {}
    for name, source in sorted(config.sources.items()):
        row: dict[str, Any] = {
            "enabled": source.enabled,
            "kind": source.kind,
            "capabilities": [],
        }
        if source.enabled and name in bindings:
            try:
                row["capabilities"] = (
                    bindings[name].capabilities(name).model_dump(mode="json")["capabilities"]
                )
            except ChamberlainError as error:
                row["error"] = error.code.value
        sources[name] = row
    _print(out, {"sources": sources})
    return EXIT_OK


@_rooted(missing=EXIT_INVALID)
def _setup(root: Path, args: argparse.Namespace, out: TextIO) -> int:
    return provider_setup(args.provider, args.options, root, out)


_COMMANDS = {
    "migrate": migration_command,
    "status": _status,
    "updates": updates,
    "setup": _setup,
    "tick": _tick,
    "thunderbird": _thunderbird,
    "schedule": _schedule,
    "config": _config,
    "import": _import,
    "recipients": _recipients,
    "addon": _addon,
    "export": _export,
    "cleanup": _cleanup,
    "viewer": _viewer,
    "live-test": _live_test,
}


def main(argv: Sequence[str] | None = None, out: TextIO | None = None) -> int:
    console: TextIO = sys.stdout if sys.stdout is not None else io.StringIO()  # pythonw: none
    reconfigure: Any = getattr(console, "reconfigure", None)
    if out is None and callable(reconfigure):
        # Windows consoles default to a legacy code page; mail subjects carry emoji.
        reconfigure(encoding="utf-8")
    stream: TextIO = out or console
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
    data = args.data or os.environ.get(DATA_ENV)
    if data:
        try:
            refuse_inside_checkout(data)
        except ChamberlainError as error:
            return _fail(stream, error)
    command = _COMMANDS.get(args.command, _call_tool)
    if args.command == "tick" and args.quiet:
        stream = io.StringIO()
    return command(args, stream)


if __name__ == "__main__":
    raise SystemExit(main())
