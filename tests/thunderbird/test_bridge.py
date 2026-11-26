"""Native host and client behaviour over scripted pipes (bridge contract B-1 to B-10, P-8, P-17).

No Thunderbird and no Node here: a scripted add-on speaks native-messaging frames through
OS pipes, so each rule about the host and the client is tested in isolation.
"""

import base64
import contextlib
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from chamberlain.connectors.thunderbird import frames, protocol
from chamberlain.connectors.thunderbird.client import (
    BridgeClient,
    BridgeFailure,
    RuntimeConnector,
    split_chunks,
)
from chamberlain.connectors.thunderbird.native_host import (
    Deadlines,
    FdReader,
    FdWriter,
    NativeHost,
    private_directory,
    write_runtime,
)
from chamberlain.connectors.thunderbird.protocol import (
    PROTOCOL_VERSION,
    AddonHello,
    BridgeError,
    BridgeErrorCode,
    ChunkFrame,
    ClientHello,
    Command,
    Refusal,
    RefusalCode,
    RequestFrame,
    ResponseFrame,
)
from chamberlain.domain.canonical import sha256_bytes
from chamberlain.schema.base import Model
from chamberlain.schema.errors import ChamberlainError, ErrorCode

TOKEN = "k" * 64
FAST = Deadlines(
    addon_read=0.4,
    addon_write=0.4,
    host_read=0.8,
    host_write=0.8,
    client_read=1.5,
    client_write=1.5,
)
ID = "req_0123456789"

Handler = Callable[[RequestFrame | ChunkFrame, "Addon"], None]


class Addon:
    """The add-on's side of the native pipes, scripted by a handler."""

    def __init__(self, handler: Handler | None) -> None:
        self.to_host_r, self.to_host_w = os.pipe()
        self.from_host_r, self.from_host_w = os.pipe()
        self.received: list[RequestFrame | ChunkFrame] = []
        self.calendar_available = False
        self._handler = handler
        self._write_lock = threading.Lock()
        self._thread = threading.Thread(target=self._pump, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _pump(self) -> None:
        reader = FdReader(self.from_host_r)
        while True:
            try:
                raw = frames.read_frame(reader, frames.TO_ADDON_LIMIT)
            except (ChamberlainError, OSError):
                return
            if raw is None:
                return
            frame = protocol.parse_frame(raw)
            if isinstance(frame, RequestFrame | ChunkFrame):
                self.received.append(frame)
                if self._handler is not None:
                    self._handler(frame, self)

    def say(self, frame: Model) -> None:
        with self._write_lock:
            frames.write_frame(
                FdWriter(self.to_host_w), protocol.dump_frame(frame), frames.FROM_ADDON_LIMIT
            )

    def hello(self, *, v: int = PROTOCOL_VERSION, calendar_available: bool = False) -> None:
        self.calendar_available = calendar_available
        self.say(
            AddonHello(
                v=v,
                id="hello-00000001",
                addon_version="2.0.0",
                calendar_available=calendar_available,
            )
        )

    def ok(self, request: RequestFrame, result: Model, chunks: int = 0) -> None:
        self.say(
            ResponseFrame(
                v=PROTOCOL_VERSION,
                id=request.id,
                cmd=request.cmd,
                ok=True,
                result=result.model_dump(mode="json", exclude_none=True),
                chunks=chunks,
            )
        )

    def error(self, request: RequestFrame, code: BridgeErrorCode, mutation: str = "none") -> None:
        failure = BridgeError(code=code, mutation="possible" if mutation == "possible" else "none")
        self.say(
            ResponseFrame(
                v=PROTOCOL_VERSION, id=request.id, cmd=request.cmd, ok=False, error=failure
            )
        )

    def close(self) -> None:
        for fd in (self.to_host_w, self.from_host_w):
            with contextlib.suppress(OSError):
                os.close(fd)
        self._thread.join(timeout=3)
        for fd in (self.to_host_r, self.from_host_r):
            with contextlib.suppress(OSError):
                os.close(fd)


@dataclass
class Env:
    addon: Addon
    host: NativeHost
    client: BridgeClient
    directory: Path

    def raw(self) -> socket.socket:
        connection = socket.create_connection(("127.0.0.1", self.host.port), timeout=3)
        connection.settimeout(3)
        return connection


class _Sock:
    def __init__(self, connection: socket.socket) -> None:
        self.connection = connection

    def read(self, size: int, /) -> bytes:
        return self.connection.recv(size)

    def write(self, data: bytes | memoryview, /) -> int:
        self.connection.sendall(data)
        return len(data)

    def flush(self) -> None:
        return None


def say(connection: socket.socket, frame: Model) -> None:
    frames.write_frame(_Sock(connection), protocol.dump_frame(frame), frames.LOCAL_LIMIT)


def hear(connection: socket.socket) -> object:
    raw = frames.read_frame(_Sock(connection), frames.LOCAL_LIMIT)
    return None if raw is None else protocol.parse_frame(raw)


def hello_frame(token: str = TOKEN, v: int = PROTOCOL_VERSION) -> ClientHello:
    return ClientHello(v=v, id="client-hello-1", token=token)


@contextmanager
def bridge(
    tmp_path: Path,
    handler: Handler | None = None,
    *,
    addon_hello: bool = True,
    calendar_available: bool = False,
    addon_version: int = PROTOCOL_VERSION,
    deadlines: Deadlines = FAST,
) -> Iterator[Env]:
    addon = Addon(handler)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    host = NativeHost(
        FdReader(addon.to_host_r),
        FdWriter(addon.from_host_w),
        listener,
        token=TOKEN,
        deadlines=deadlines,
    )
    host.start()
    addon.start()
    write_runtime(tmp_path, port=host.port, token=TOKEN, pid=0)
    if addon_hello:
        addon.hello(v=addon_version, calendar_available=calendar_available)
        for _ in range(200):
            if host.addon_connected:
                break
            threading.Event().wait(0.01)
    try:
        yield Env(addon, host, BridgeClient(RuntimeConnector(tmp_path), deadlines), tmp_path)
    finally:
        addon.close()
        host.stop()


def status_handler(frame: RequestFrame | ChunkFrame, addon: Addon) -> None:
    if isinstance(frame, RequestFrame):
        result = protocol.StatusResult(
            addon_version="2.0.0",
            write_busy=False,
            calendar_available=addon.calendar_available,
        )
        addon.ok(frame, result)


def a_status_call(env: Env) -> object:
    return env.client.call(Command.STATUS, protocol.StatusArgs()).result


# --- authentication, handshake, closed list ------------------------------------------------


def test_b9_an_unauthenticated_client_is_refused_and_nothing_is_relayed(tmp_path: Path) -> None:
    with bridge(tmp_path, status_handler) as env, env.raw() as connection:
        say(connection, hello_frame(token="wrong-token"))
        answer = hear(connection)
        assert isinstance(answer, Refusal)
        assert answer.code is RefusalCode.UNAUTHENTICATED
        with contextlib.suppress(ChamberlainError, OSError):
            say(connection, RequestFrame(v=PROTOCOL_VERSION, id=ID, cmd="status", args={}))
        assert env.addon.received == []


def test_b9_a_version_mismatch_refuses_and_names_the_addon_update(tmp_path: Path) -> None:
    with bridge(tmp_path, status_handler, addon_version=2) as env:
        with pytest.raises(BridgeFailure) as raised:
            a_status_call(env)
        assert raised.value.code is ErrorCode.SOURCE_UNAVAILABLE
        assert not raised.value.sent
        assert "add-on" in raised.value.fix.lower()
        assert env.addon.received == []


def test_b9_no_addon_connected_is_unavailable_and_not_sent(tmp_path: Path) -> None:
    with bridge(tmp_path, status_handler, addon_hello=False) as env:
        with pytest.raises(BridgeFailure) as raised:
            a_status_call(env)
        assert raised.value.code is ErrorCode.SOURCE_UNAVAILABLE
        assert raised.value.proven_not_applied


def test_b2_a_command_outside_the_list_is_refused_by_the_host(tmp_path: Path) -> None:
    with bridge(tmp_path, status_handler) as env, env.raw() as connection:
        say(connection, hello_frame())
        assert isinstance(hear(connection), protocol.Welcome)
        say(connection, RequestFrame(v=PROTOCOL_VERSION, id=ID, cmd="eval", args={"code": "1"}))
        answer = hear(connection)
        assert isinstance(answer, ResponseFrame)
        assert answer.error is not None
        assert answer.error.code is BridgeErrorCode.UNKNOWN_COMMAND
        assert env.addon.received == []


def test_b2_malformed_write_args_are_refused_before_relay(tmp_path: Path) -> None:
    with bridge(tmp_path, status_handler) as env, env.raw() as connection:
        say(connection, hello_frame())
        assert isinstance(hear(connection), protocol.Welcome)
        say(
            connection,
            RequestFrame(v=PROTOCOL_VERSION, id=ID, cmd="compose", args={"mode": "send"}),
        )
        answer = hear(connection)
        assert isinstance(answer, ResponseFrame)
        assert answer.error is not None
        assert answer.error.code is BridgeErrorCode.BAD_REQUEST
        assert env.addon.received == []


def test_nested_required_nullable_field_survives_the_client_frame(tmp_path: Path) -> None:
    def answer(frame: RequestFrame | ChunkFrame, addon: Addon) -> None:
        if isinstance(frame, RequestFrame):
            addon.ok(frame, protocol.CopiesResult(scopes=(), refresh_verified=True))

    args = protocol.CopiesArgs(
        account_id="account1",
        mailbox="owner@example.org",
        where=(protocol.CopyScope(folder_id=None, subject="subject", since_ms=0),),
    )
    with bridge(tmp_path, answer) as env:
        env.client.call(Command.COPIES, args)
        request = env.addon.received[0]
        assert isinstance(request, RequestFrame)
        scopes = request.args["where"]
        assert isinstance(scopes, list)
        assert isinstance(scopes[0], dict)
        assert scopes[0]["folder_id"] is None


def test_b5_a_request_id_already_in_flight_is_refused(tmp_path: Path) -> None:
    # The first request must still be in flight when the second arrives. With the fast
    # deadlines a busy machine let the first one time out first (the old flake), so this
    # test gives the host a long read deadline and releases the add-on itself.
    slow = Deadlines(
        addon_read=5, addon_write=5, host_read=30, host_write=30, client_read=60, client_write=60
    )
    gate = threading.Event()
    arrived = threading.Event()

    def hold(frame: RequestFrame | ChunkFrame, addon: Addon) -> None:
        if isinstance(frame, RequestFrame):
            arrived.set()
            gate.wait(10)
            status_handler(frame, addon)

    with bridge(tmp_path, hold, deadlines=slow) as env:
        first = env.raw()
        second = env.raw()
        try:
            for connection in (first, second):
                say(connection, hello_frame())
                assert isinstance(hear(connection), protocol.Welcome)
            say(first, RequestFrame(v=PROTOCOL_VERSION, id=ID, cmd="status", args={}))
            assert arrived.wait(10), "the first request never reached the add-on"
            say(second, RequestFrame(v=PROTOCOL_VERSION, id=ID, cmd="status", args={}))
            answer = hear(second)
            assert isinstance(answer, Refusal)
            assert answer.code is RefusalCode.DUPLICATE_ID
        finally:
            gate.set()
            first.close()
            second.close()


# --- a dropped client never crashes the host -----------------------------------------------


def test_b7_dropped_and_malformed_clients_never_crash_the_host(tmp_path: Path) -> None:
    with bridge(tmp_path, status_handler) as env:
        with env.raw():
            pass  # connects and leaves
        with env.raw() as connection:
            connection.sendall(b"\x05\x00\x00\x00abcde")  # a frame that is not JSON
        with env.raw() as connection:
            connection.sendall(b"\xff\xff\xff\xff")  # an absurd length
        with env.raw() as connection:
            say(connection, hello_frame())
            hear(connection)
            connection.sendall(b"\x20\x00\x00\x00{")  # truncated mid-request
        with env.raw() as connection:
            say(connection, hello_frame())
            hear(connection)
            say(connection, RequestFrame(v=PROTOCOL_VERSION, id=ID, cmd="status", args={}))
        assert isinstance(a_status_call(env), protocol.StatusResult)
        assert env.host.addon_connected


# --- relaying, chunks and timeouts -----------------------------------------------------------


def test_b4_a_request_round_trips_with_its_id(tmp_path: Path) -> None:
    with bridge(tmp_path, status_handler) as env:
        reply = env.client.call(Command.STATUS, protocol.StatusArgs())
        assert isinstance(reply.result, protocol.StatusResult)
        assert isinstance(env.addon.received[0], RequestFrame)


def test_b8_uploaded_attachments_travel_as_hashed_chunks_after_the_request(tmp_path: Path) -> None:
    data = bytes(range(256)) * 2000  # 512,000 bytes: three chunks
    chunks = split_chunks(data)
    spec = protocol.AttachmentSpec(
        key="a1", name="a.bin", size=len(data), sha256=sha256_bytes(data), chunks=len(chunks)
    )

    def collect(frame: RequestFrame | ChunkFrame, addon: Addon) -> None:
        sent_chunks = [f for f in addon.received if isinstance(f, ChunkFrame)]
        if isinstance(frame, ChunkFrame) and len(sent_chunks) == spec.chunks:
            addon.ok(
                next(f for f in addon.received if isinstance(f, RequestFrame)),
                protocol.ComposeResult(attachment_bytes_verified=True),
            )

    args = protocol.ComposeArgs(
        account_id="account1",
        mailbox="owner@example.org",
        mode="send",
        to=("a@example.org",),
        subject="s",
        body="b",
        sent_folder_id="f1",
        draft_folder_id="f2",
        token="<t@x>",
        attachments=(spec,),
    )
    with bridge(tmp_path, collect) as env:
        reply = env.client.call(Command.COMPOSE, args, payloads={"a1": data})
        assert isinstance(reply.result, protocol.ComposeResult)
        first = env.addon.received[0]
        assert isinstance(first, RequestFrame)
        got = [f for f in env.addon.received if isinstance(f, ChunkFrame)]
        assert [f.index for f in got] == [0, 1, 2]
        assert {f.id for f in got} == {first.id}
        assert b"".join(base64.b64decode(f.data) for f in got) == data
        assert all(sha256_bytes(base64.b64decode(f.data)) == f.sha256 for f in got)


def test_p8_a_download_is_assembled_from_chunks_and_verified(tmp_path: Path) -> None:
    data = bytes(range(256)) * 3000
    pieces = split_chunks(data)

    def serve(frame: RequestFrame | ChunkFrame, addon: Addon) -> None:
        if not isinstance(frame, RequestFrame):
            return
        for index, piece in enumerate(pieces):
            addon.say(
                ChunkFrame(
                    v=PROTOCOL_VERSION,
                    id=frame.id,
                    name="1.2",
                    index=index,
                    data=base64.b64encode(piece).decode(),
                    sha256=sha256_bytes(piece),
                )
            )
        result = protocol.AttachmentResult(
            name="a.bin",
            media_type="application/octet-stream",
            size=len(data),
            sha256=sha256_bytes(data),
            chunks=len(pieces),
        )
        addon.ok(frame, result, chunks=len(pieces))

    args = protocol.AttachmentArgs(
        account_id="account1",
        mailbox="owner@example.org",
        folder_id="f",
        internet_message_id="m@x",
        part_name="1.2",
        max_bytes=10_000_000,
    )
    with bridge(tmp_path, serve) as env:
        reply = env.client.call(Command.ATTACHMENT, args)
        assert reply.assembled("1.2", expected=len(pieces)) == data


def test_p8_a_chunk_that_does_not_match_its_hash_yields_nothing(tmp_path: Path) -> None:
    def serve(frame: RequestFrame | ChunkFrame, addon: Addon) -> None:
        if not isinstance(frame, RequestFrame):
            return
        addon.say(
            ChunkFrame(
                v=PROTOCOL_VERSION,
                id=frame.id,
                name="1.2",
                index=0,
                data=base64.b64encode(b"bad").decode(),
                sha256="0" * 64,
            )
        )
        result = protocol.AttachmentResult(
            name="a",
            media_type="application/octet-stream",
            size=3,
            sha256="0" * 64,
            chunks=1,
        )
        addon.ok(frame, result, chunks=1)

    args = protocol.AttachmentArgs(
        account_id="account1",
        mailbox="owner@example.org",
        folder_id="f",
        internet_message_id="m@x",
        part_name="1.2",
        max_bytes=100,
    )
    with bridge(tmp_path, serve) as env:
        reply = env.client.call(Command.ATTACHMENT, args)
        with pytest.raises(ChamberlainError) as raised:
            reply.assembled("1.2", expected=1)
        assert raised.value.code is ErrorCode.ARTIFACT_HASH_MISMATCH


def test_p8_a_response_that_lost_a_chunk_is_unavailable(tmp_path: Path) -> None:
    def serve(frame: RequestFrame | ChunkFrame, addon: Addon) -> None:
        if isinstance(frame, RequestFrame):
            result = protocol.AttachmentResult(
                name="a",
                media_type="application/octet-stream",
                size=3,
                sha256="0" * 64,
                chunks=2,
            )
            addon.ok(frame, result, chunks=2)

    args = protocol.AttachmentArgs(
        account_id="account1",
        mailbox="owner@example.org",
        folder_id="f",
        internet_message_id="m@x",
        part_name="1.2",
        max_bytes=100,
    )
    with bridge(tmp_path, serve) as env, pytest.raises(BridgeFailure) as raised:
        env.client.call(Command.ATTACHMENT, args)
    assert raised.value.code is ErrorCode.SOURCE_UNAVAILABLE


def move_args() -> protocol.ChangeArgs:
    return protocol.ChangeArgs(
        account_id="account1",
        mailbox="owner@example.org",
        items=(protocol.ChangeGroup(folder_id="f1", ids=("m@x",)),),
        move_to="f2",
    )


def test_p17_a_write_that_never_answers_is_possible_not_proven_absent(tmp_path: Path) -> None:
    with bridge(tmp_path, None) as env, pytest.raises(BridgeFailure) as raised:
        env.client.call(Command.CHANGE, move_args())
    assert raised.value.sent
    assert raised.value.mutation == "possible"
    assert not raised.value.proven_not_applied


def test_p17_a_read_that_never_answers_is_source_unavailable_not_a_hang(tmp_path: Path) -> None:
    with bridge(tmp_path, None) as env, pytest.raises(BridgeFailure) as raised:
        env.client.call(Command.STATUS, protocol.StatusArgs())
    assert raised.value.code is ErrorCode.SOURCE_UNAVAILABLE


def test_p10_an_addon_error_proves_not_applied_only_when_it_says_so(tmp_path: Path) -> None:
    def refuse(frame: RequestFrame | ChunkFrame, addon: Addon) -> None:
        if isinstance(frame, RequestFrame):
            addon.error(frame, BridgeErrorCode.BUSY, "none")

    with bridge(tmp_path, refuse) as env, pytest.raises(BridgeFailure) as raised:
        env.client.call(Command.CHANGE, move_args())
    assert raised.value.proven_not_applied
    assert raised.value.code is ErrorCode.SOURCE_BUSY

    def possible(frame: RequestFrame | ChunkFrame, addon: Addon) -> None:
        if isinstance(frame, RequestFrame):
            addon.error(frame, BridgeErrorCode.NOT_VERIFIED, "possible")

    with bridge(tmp_path, possible) as env, pytest.raises(BridgeFailure) as raised:
        env.client.call(Command.CHANGE, move_args())
    assert not raised.value.proven_not_applied


def test_p10_the_addon_disappearing_mid_request_is_possible(tmp_path: Path) -> None:
    def vanish(frame: RequestFrame | ChunkFrame, addon: Addon) -> None:
        if isinstance(frame, RequestFrame):
            os.close(addon.to_host_w)

    with bridge(tmp_path, vanish) as env, pytest.raises(BridgeFailure) as raised:
        env.client.call(Command.CHANGE, move_args())
    assert not raised.value.proven_not_applied


def test_b7_an_outgoing_frame_over_the_host_limit_is_refused_before_sending(tmp_path: Path) -> None:
    args = protocol.ComposeArgs(
        account_id="account1",
        mailbox="owner@example.org",
        mode="send",
        to=("a@example.org",),
        subject="s",
        body="\x00" * 190_000,  # each escapes to six bytes: over 900,000
        sent_folder_id="f1",
        draft_folder_id="f2",
        token="<t@x>",
    )
    with bridge(tmp_path, status_handler) as env, pytest.raises(ChamberlainError) as raised:
        env.client.call(Command.COMPOSE, args)
    assert raised.value.code is ErrorCode.REQUEST_TOO_LARGE
    assert env.addon.received == []


def test_b10_with_no_runtime_file_the_bridge_is_unavailable_and_nothing_was_sent(
    tmp_path: Path,
) -> None:
    client = BridgeClient(RuntimeConnector(tmp_path / "nowhere"), FAST)
    with pytest.raises(BridgeFailure) as raised:
        client.call(Command.STATUS, protocol.StatusArgs())
    assert raised.value.proven_not_applied
    assert raised.value.code is ErrorCode.SOURCE_UNAVAILABLE


def test_b10_deadlines_nest_addon_then_host_then_client() -> None:
    default = Deadlines()
    assert default.addon_write < default.host_write < default.client_write
    assert default.addon_read < default.host_read < default.client_read
    with pytest.raises(ChamberlainError) as raised:
        Deadlines(addon_write=60.0)
    assert raised.value.code is ErrorCode.CONFIG_INVALID


def test_b10_the_addon_deadlines_in_the_typescript_match_the_contract() -> None:
    from chamberlain.connectors.thunderbird.typegen import render

    source = render()
    default = Deadlines()
    assert f"READ_DEADLINE_MS = {int(default.addon_read * 1000):_}" in source
    assert f"WRITE_DEADLINE_MS = {int(default.addon_write * 1000):_}" in source


def test_b10_the_runtime_file_names_the_port_and_token(tmp_path: Path) -> None:
    target = write_runtime(tmp_path, port=4321, token="abc", pid=7)
    assert target.name == "runtime.json"
    text = target.read_text(encoding="utf-8")
    assert '"port": 4321' in text
    assert '"token": "abc"' in text
    assert not list(tmp_path.glob("runtime-*.json"))


def test_b10_the_bridge_directory_is_restricted_to_the_current_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("USERNAME", "tester")
    monkeypatch.setenv("USERDOMAIN", "LAB")
    private_directory(tmp_path / "bridge")
    assert calls[0][0] == "icacls"
    assert "/inheritance:r" in calls[0]
    assert "LAB\\tester:(OI)(CI)F" in calls[0]


def test_b10_calendar_availability_does_not_disturb_mail_handshake(tmp_path: Path) -> None:
    for available in (False, True):
        with bridge(tmp_path, status_handler, calendar_available=available) as env:
            assert env.client.hello().calendar_available is available
            assert isinstance(a_status_call(env), protocol.StatusResult)


def test_b10_the_calendar_adapter_needs_the_capability_and_sends_nothing_without_it(
    tmp_path: Path,
) -> None:
    with bridge(tmp_path, status_handler) as env:
        adapter = env.client
        assert not adapter.hello().calendar_available
        with pytest.raises(BridgeFailure) as raised:
            adapter.call(Command.CALENDAR_LIST, protocol.StatusArgs(), calendar=True)
        assert not raised.value.sent
        assert env.addon.received == []
