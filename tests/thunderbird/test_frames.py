"""Frame reader and writer: limits and fuzzing (bridge contract B-7, old test intent)."""

import contextlib
import io
import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from chamberlain.connectors.thunderbird.frames import (
    FROM_ADDON_LIMIT,
    TO_ADDON_LIMIT,
    encode_frame,
    read_frame,
    write_frame,
)
from chamberlain.connectors.thunderbird.protocol import parse_frame
from chamberlain.schema.errors import ChamberlainError, ErrorCode

NO_DB = settings(database=None, max_examples=200)


class Trickle:
    """A stream that returns at most `step` bytes per read, as pipes do."""

    def __init__(self, data: bytes, step: int) -> None:
        self._data = io.BytesIO(data)
        self._step = step

    def read(self, size: int, /) -> bytes:
        return self._data.read(min(size, self._step))


def test_b7_frames_roundtrip_even_when_split_across_reads() -> None:
    body = b'{"a":1}' * 50
    raw = encode_frame(body, 10_000)
    assert read_frame(Trickle(raw, 1), 10_000) == body
    assert read_frame(Trickle(raw, 5), 10_000) == body


def test_b7_truncated_zero_and_oversize_frames_are_refused() -> None:
    for raw in [
        b"\x01\x00",  # header cut short
        b"\x05\x00\x00\x00ab",  # body cut short
        b"\x00\x00\x00\x00",  # zero length
        b"\xff\xff\xff\xff",  # absurd length
    ]:
        with pytest.raises(ChamberlainError) as raised:
            read_frame(io.BytesIO(raw), 1_000)
        assert raised.value.code is ErrorCode.SOURCE_UNAVAILABLE, raw


def test_b7_the_limit_differs_by_direction() -> None:
    body = b"x" * (TO_ADDON_LIMIT + 1)
    with pytest.raises(ChamberlainError) as raised:
        encode_frame(body, TO_ADDON_LIMIT)
    assert raised.value.code is ErrorCode.REQUEST_TOO_LARGE
    assert len(encode_frame(body, FROM_ADDON_LIMIT)) == len(body) + 4


def test_b7_a_failed_write_is_source_unavailable() -> None:
    class Broken:
        def write(self, data: bytes | memoryview, /) -> int:
            raise OSError("pipe closed")

        def flush(self) -> None:
            return None

    with pytest.raises(ChamberlainError) as raised:
        write_frame(Broken(), b"{}", 100)
    assert raised.value.code is ErrorCode.SOURCE_UNAVAILABLE


@NO_DB
@given(st.binary(max_size=300), st.integers(min_value=1, max_value=7))
def test_b7_fuzz_the_reader_only_ever_raises_chamberlain_errors(raw: bytes, step: int) -> None:
    with contextlib.suppress(ChamberlainError):
        read_frame(Trickle(raw, step), 200)


@NO_DB
@given(st.binary(max_size=400))
def test_b7_fuzz_message_parsing_only_ever_raises_chamberlain_errors(body: bytes) -> None:
    with contextlib.suppress(ChamberlainError):
        parse_frame(body)


@NO_DB
@given(
    st.dictionaries(st.text(max_size=8), st.one_of(st.none(), st.integers(), st.text(max_size=8)))
)
def test_b7_fuzz_json_objects_are_refused_cleanly(value: dict[str, object]) -> None:
    with contextlib.suppress(ChamberlainError):
        parse_frame(json.dumps(value).encode())
