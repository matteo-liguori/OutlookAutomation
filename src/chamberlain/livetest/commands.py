"""The `chamberlain live-test` command: run, grant, sweep, report.

Contract: docs/live-test.md. This is the only place that builds the real environment. The
surface (`surface/cli.py`) calls `main` and prints what it returns.
"""

import json
from pathlib import Path
from typing import TextIO

from chamberlain.app.core import App
from chamberlain.connections import load_connections, open_app
from chamberlain.livetest.authority import start_standing_proposals
from chamberlain.livetest.config import LiveTestConfig
from chamberlain.livetest.env import (
    Environment,
    FileFaultInjector,
    PortProbe,
    RealWaiter,
)
from chamberlain.livetest.report import read_reports, table, write_report
from chamberlain.livetest.runner import RunOptions, execute, make_run_id, open_lab
from chamberlain.livetest.sweep import sweep
from chamberlain.schema.envelope import error_reply
from chamberlain.schema.errors import ChamberlainError, ErrorCode

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_STOPPED = 2


def real_environment(app: App, data_root: Path, bridge_dir: str | None) -> Environment:
    return Environment(
        mode="live",
        clock=app.context.clock,
        waiter=RealWaiter(),
        faults=FileFaultInjector(data_root, None if bridge_dir is None else Path(bridge_dir)),
        probe=PortProbe(app.context),
    )


def _disabled() -> ChamberlainError:
    return ChamberlainError(
        ErrorCode.CONFIG_INVALID,
        "Live test is switched off.",
        fix="Set live_test.enabled: true and list the sources in config/connections.yaml "
        "(see docs/configuration.md), then run `chamberlain live-test grant`.",
    )


def _fail(out: TextIO, error: ChamberlainError) -> int:
    out.write(json.dumps(error_reply(error).model_dump(mode="json", exclude_none=True)) + "\n")
    return EXIT_STOPPED


def main(
    action: str,
    data_root: Path,
    options: RunOptions,
    out: TextIO,
    *,
    as_json: bool = False,
    run_id: str | None = None,
) -> int:
    """Run one live-test action against the configured installation. Returns the exit code."""
    try:
        connections = load_connections(data_root)
        config = connections.live_test
        if action == "report":
            return _report(data_root, run_id, out, as_json)
        if not config.enabled:
            raise _disabled()
        app = open_app(data_root)
    except ChamberlainError as error:
        return _fail(out, error)
    try:
        env = real_environment(app, data_root, connections.bridge_dir)
        return _dispatch(action, app, env, config, data_root, options, out, as_json)
    except ChamberlainError as error:
        return _fail(out, error)
    finally:
        app.close()


def _dispatch(
    action: str,
    app: App,
    env: Environment,
    config: LiveTestConfig,
    data_root: Path,
    options: RunOptions,
    out: TextIO,
    as_json: bool,
) -> int:
    if action == "grant":
        return _grant(app, env, config, options, out)
    if action == "sweep":
        result = sweep(app, env, config, options)
        out.write("\n".join(result.lines()) + "\n")
        if result.stopped:
            return EXIT_STOPPED
        return EXIT_FAILED if result.problems else EXIT_OK
    report = execute(app, env, config, options)
    if not options.dry_run and not report.stopped:
        json_path, md_path = write_report(data_root, report, env.clock.now())
        out.write(f"Report: {md_path}\n        {json_path}\n\n")
    out.write(table(report) + "\n")
    if as_json:
        out.write(report.model_dump_json(indent=2) + "\n")
    if report.stopped:
        return EXIT_STOPPED
    return EXIT_FAILED if report.score.failed else EXIT_OK


def _grant(
    app: App, env: Environment, config: LiveTestConfig, options: RunOptions, out: TextIO
) -> int:
    lab = open_lab(app, env, config, make_run_id(env, options), options)
    states = start_standing_proposals(lab, list(config.mail))
    waiting = 0
    for state in states:
        if state.state == "active":
            out.write(f"{state.source}: already confirmed.\n")
        else:
            waiting += 1
            out.write(f"{state.source}: waiting for you ({state.handle}).\n")
    if waiting:
        out.write(
            "\nOpen the viewer (`chamberlain viewer`), read each live-test grant and confirm it "
            "once. This command never confirms anything itself.\n"
        )
    return EXIT_OK


def _report(data_root: Path, run_id: str | None, out: TextIO, as_json: bool) -> int:
    reports = read_reports(data_root, 50)
    chosen = [r for r in reports if run_id is None or r.run_id == run_id][:1]
    if not chosen:
        out.write("No live-test report found.\n")
        return EXIT_STOPPED
    report = chosen[0]
    out.write((report.model_dump_json(indent=2) if as_json else table(report)) + "\n")
    return EXIT_OK
