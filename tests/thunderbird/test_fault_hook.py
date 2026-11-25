"""The live-test fault hook of the native host: lose one response, safely.

It fires only for a `send` whose subject carries the file's run id, once, within ten minutes.
The write still reaches the add-on; the client gets no answer and reports an uncertain write.
"""

import contextlib
import json
import os
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from test_bridge import (
    ALL_CAPS,
    FAST,
    ID,
    TOKEN,
    Addon,
    FdReader,
    FdWriter,
    hear,
    hello_frame,
    say,
)

from chamberlain.connectors.thunderbird import native_host, protocol
from chamberlain.connectors.thunderbird.native_host import (
    FAULT_DROP_RESPONSE,
    LiveFault,
    NativeHost,
    write_runtime,
)
from chamberlain.connectors.thunderbird.protocol import (
    ChunkFrame,
    RequestFrame,
    Welcome,
)

RUN = "20261124T153000-ab12"
SUBJECT = f"CHAMBERLAIN LIVE TEST {RUN} self_send"


@dataclass
class Env:
    addon: Addon
    host: NativeHost
    fault_file: Path

    def raw(self) -> socket.socket:
        connection = socket.create_connection(("127.0.0.1", self.host.port), timeout=3)
        connection.settimeout(3)
        return connection


def echo(frame: RequestFrame | ChunkFrame, addon: Addon) -> None:
    """The add-on does the write and answers."""
    if isinstance(frame, RequestFrame):
        addon.ok(
            frame,
            protocol.StatusResult(
                addon_version="2.0.0",
                browser_version="159.0",
                write_busy=False,
                capabilities=ALL_CAPS,
            ),
        )


@contextmanager
def hosted(tmp_path: Path) -> Iterator[Env]:
    addon = Addon(echo)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    fault_file = tmp_path / "run" / native_host.FAULT_FILE
    fault_file.parent.mkdir()
    host = NativeHost(
        FdReader(addon.to_host_r),
        FdWriter(addon.from_host_w),
        listener,
        token=TOKEN,
        deadlines=FAST,
        fault_file=fault_file,
    )
    host.start()
    addon.start()
    write_runtime(tmp_path, port=host.port, token=TOKEN, pid=0)
    addon.hello()
    for _ in range(200):
        if host.addon_connected:
            break
        threading.Event().wait(0.01)
    try:
        yield Env(addon, host, fault_file)
    finally:
        addon.close()
        host.stop()


def arm(env: Env, run_id: str = RUN, fault: str = FAULT_DROP_RESPONSE) -> None:
    env.fault_file.write_text(json.dumps({"run_id": run_id, "fault": fault}), encoding="utf-8")


def send_request(subject: str, cmd: str = "send", request_id: str = ID) -> RequestFrame:
    return RequestFrame(
        v=2,
        id=request_id,
        cmd=cmd,
        args={"subject": subject, "account_id": "a"},
    )


def ask(env: Env, request: RequestFrame) -> object:
    """Send one request on its own connection; the answer, or None if the line closed."""
    with env.raw() as connection:
        say(connection, hello_frame())
        assert isinstance(hear(connection), Welcome)
        say(connection, request)
        with contextlib.suppress(OSError):
            return hear(connection)
    return None


def test_a_marked_send_loses_its_response_once_but_still_runs(tmp_path: Path) -> None:
    with hosted(tmp_path) as env:
        arm(env)
        assert ask(env, send_request(SUBJECT)) is None
        assert [f.cmd for f in env.addon.received if isinstance(f, RequestFrame)] == ["send"]
        assert list(env.fault_file.parent.iterdir()) == []  # consumed, nothing left behind
        second = ask(env, send_request(SUBJECT, request_id="req_second0001"))
        assert isinstance(second, protocol.ResponseFrame)


def test_nothing_else_is_ever_touched(tmp_path: Path) -> None:
    cases = ["other_subject", "other_run", "not_a_send", "unknown_fault", "stale", "broken"]
    for case in [*cases, "no_file"]:
        (tmp_path / case).mkdir()
        _nothing_else_is_touched(tmp_path / case, case)


def _nothing_else_is_touched(tmp_path: Path, case: str) -> None:
    subject, cmd = SUBJECT, "send"
    with hosted(tmp_path) as env:
        if case == "broken":
            env.fault_file.write_text("not json", encoding="utf-8")
        elif case == "unknown_fault":
            arm(env, fault="explode")
        elif case != "no_file":
            arm(env)
        if case == "other_subject":
            subject = "An ordinary subject"
        elif case == "other_run":
            subject = "CHAMBERLAIN LIVE TEST 20200101T000000-0000 self_send"
        elif case == "not_a_send":
            cmd = "status"
        elif case == "stale":
            old = env.fault_file.stat().st_mtime - 3600
            os.utime(env.fault_file, (old, old))
        assert isinstance(ask(env, send_request(subject, cmd=cmd)), protocol.ResponseFrame)
        if case in {"other_subject", "other_run", "not_a_send"}:
            assert env.fault_file.exists()  # a request it must not touch does not use it up


def test_one_claim_wins_and_the_host_looks_only_in_the_default_layout(tmp_path: Path) -> None:
    path = tmp_path / native_host.FAULT_FILE
    path.write_text(json.dumps({"run_id": RUN, "fault": FAULT_DROP_RESPONSE}), encoding="utf-8")
    hook = LiveFault(path)
    request = send_request(SUBJECT)
    assert (hook.claim(request), hook.claim(request)) == (True, False)
    default = native_host.fault_file_for(Path("data/bridge"))
    assert default is not None
    assert default.as_posix() == "data/run/livetest-fault.json"
    assert native_host.fault_file_for(Path("elsewhere/other")) is None
