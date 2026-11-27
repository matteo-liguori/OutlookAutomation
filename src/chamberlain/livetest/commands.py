"""The `chamberlain live-test` command: run, grant, sweep, report.

Contract: docs/live-test.md. This is the only place that builds the real environment. The
surface (`surface/cli.py`) calls `main` and prints what it returns.
"""

import json
import sys
import time
import urllib.request
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TextIO

from chamberlain.app.core import App
from chamberlain.connections import load_connections, open_app
from chamberlain.domain.testmark import carries_marker
from chamberlain.livetest.authority import start_standing_proposals
from chamberlain.livetest.config import LiveTestConfig
from chamberlain.livetest.env import (
    Environment,
    FileFaultInjector,
    PortProbe,
)
from chamberlain.livetest.model import RunReport
from chamberlain.livetest.report import read_reports, table, write_report
from chamberlain.livetest.runner import (
    NOT_RUN_YET,
    RunOptions,
    execute,
    make_run_id,
    open_lab,
    retry_ids,
)
from chamberlain.livetest.sweep import sweep
from chamberlain.schema.envelope import error_reply
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.surface.viewer import presence
from chamberlain.writes.repo import get_grant, owner_addresses

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_STOPPED = 2


def own_test_grant(app: App, grant: str) -> bool:
    """A held reply the live test itself proposed: a marked subject, every recipient one of the
    owner's own addresses. Only such a grant is confirmed for the owner (their standing
    self-test permission); anything else waits for their own click."""
    db = app.context.db
    found = get_grant(db, grant)
    if found is None:
        return False
    row = db.execute(
        "SELECT intent_json FROM operations WHERE source_id = ? AND intent_sha256 = ? "
        "ORDER BY rowid DESC LIMIT 1",
        (found.source_id, found.content_sha256),
    ).fetchone()
    if row is None:
        return False
    intent = json.loads(str(row[0]))
    recipients = [str(a).lower() for key in ("to", "cc", "bcc") for a in intent.get(key) or ()]
    own = owner_addresses(db)
    return (
        intent.get("kind") == "mail.send"
        and carries_marker(str(intent.get("subject", "")))
        and bool(recipients)
        and all(a in own for a in recipients)
    )


def viewer_confirms(app: App, data_root: Path) -> Callable[[str], None] | None:
    """Confirm the live test's own held reply through the running viewer's real endpoint, as
    the owner's click would. None when no viewer is running: the run then asks the owner."""
    run = data_root / "run"
    try:
        port = int(json.loads((run / presence.STATE_FILE).read_text("utf-8"))["port"])
        token = (run / presence.TOKEN_FILE).read_text("utf-8").strip()
    except (OSError, ValueError, KeyError, TypeError):
        return None
    origin = f"http://{presence.BIND_ADDRESS}:{port}"

    def confirm(grant: str) -> None:
        if not own_test_grant(app, grant):
            raise ChamberlainError(
                ErrorCode.REQUEST_INVALID, "Not the live test's own held reply: not confirmed."
            )
        headers = {"X-Chamberlain-Token": token}
        shown = urllib.request.Request(f"{origin}/api/grants/{grant}", headers=headers)
        with urllib.request.urlopen(shown, timeout=30) as reply:
            seen = json.loads(reply.read())
        body = json.dumps({"content_sha256": seen["content_sha256"]}).encode()
        click = urllib.request.Request(
            f"{origin}/api/grants/{grant}/confirm",
            data=body,
            method="POST",
            headers={**headers, "Origin": origin, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(click, timeout=120) as reply:
            reply.read()
        app.context.services.audit.append(
            actor="system:live-test",
            kind="grant.confirmed_for_owner",
            subject=grant,
            summary="The live test confirmed its own marked reply to the owner's own address "
            "under the owner's standing self-test permission.",
        )
        app.context.db.commit()

    return confirm


def real_environment(app: App, data_root: Path, bridge_dir: str | None) -> Environment:
    return Environment(
        mode="live",
        clock=app.context.clock,
        sleep=time.sleep,
        faults=FileFaultInjector(data_root, None if bridge_dir is None else Path(bridge_dir)),
        probe=PortProbe(app.context),
        owner_confirms=viewer_confirms(app, data_root),
        notify=lambda text: print(text, file=sys.stderr, flush=True),
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
        app = open_app(data_root, include_test=True)
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
    if options.retry_from is not None:
        earlier = _find_report(data_root, options.retry_from)
        if earlier is None:
            out.write(f"No live-test report {options.retry_from} to retry.\n")
            return EXIT_FAILED
        ids = retry_ids(earlier)
        if not ids:
            out.write(
                f"Nothing to retry: every scenario of {earlier.run_id} passed or was skipped.\n"
            )
            return EXIT_OK
        out.write(f"Retrying {len(ids)} scenario(s) of {earlier.run_id}.\n")
        doctor_operations = tuple(
            handle
            for result in earlier.results
            if result.id == "harness.doctor_clean" and result.id in ids
            for handle in result.handles
            if handle.startswith("op_")
        )
        options = replace(options, retry_ids=ids, doctor_operations=doctor_operations)
    if not options.dry_run:
        options = replace(
            options,
            progress=_progress_writer(data_root, env, out),
            open_app=lambda: open_app(data_root, include_test=True),
        )
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


def _find_report(data_root: Path, run_id: str) -> RunReport | None:
    reports = read_reports(data_root, 50)
    if run_id == "latest":
        return reports[0] if reports else None
    return next((r for r in reports if r.run_id == run_id), None)


def _progress_writer(data_root: Path, env: Environment, out: TextIO) -> Callable[[RunReport], None]:
    """After every scenario: rewrite the report files and print the new result lines."""
    shown: set[str] = set()

    def write(report: RunReport) -> None:
        write_report(data_root, report, env.clock.now())
        for result in report.results:
            if result.id in shown or result.reason == NOT_RUN_YET:
                continue
            shown.add(result.id)
            out.write(
                f"  {result.status.value:<5} {result.id:<55} {result.seconds:6.1f}s  "
                f"{result.reason[:90]}\n"
            )
            out.flush()

    return write


def _report(data_root: Path, run_id: str | None, out: TextIO, as_json: bool) -> int:
    reports = read_reports(data_root, 50)
    chosen = [r for r in reports if run_id is None or r.run_id == run_id][:1]
    if not chosen:
        out.write("No live-test report found.\n")
        return EXIT_STOPPED
    report = chosen[0]
    out.write((report.model_dump_json(indent=2) if as_json else table(report)) + "\n")
    return EXIT_OK
