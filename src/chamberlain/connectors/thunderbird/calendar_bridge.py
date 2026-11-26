"""Adapts the bridge client to the calendar connector's `CalendarBridge` protocol.

Every request checks that the privileged calendar helper is available on the same
connection, before sending the request. Failures keep the bridge's own words for `sent`
and `mutation`, which decide whether a write may have happened.
"""

from collections.abc import Mapping

from pydantic import JsonValue

from chamberlain.connectors.thunderbird.calendar import (
    BridgeHello,
    CalendarBridgeFailure,
    Json,
)
from chamberlain.connectors.thunderbird.client import BridgeClient, BridgeFailure
from chamberlain.connectors.thunderbird.protocol import Command, parse_args
from chamberlain.schema.errors import ChamberlainError


class ClientCalendarBridge:
    """`CalendarBridge` over a `BridgeClient`."""

    def __init__(self, client: BridgeClient) -> None:
        self._client = client

    def hello(self) -> BridgeHello:
        try:
            welcome = self._client.hello()
        except BridgeFailure as failure:
            raise _failure(failure) from None
        return BridgeHello(welcome.v, welcome.calendar_available)

    def request(self, cmd: str, args: Mapping[str, JsonValue], *, write: bool = False) -> Json:
        try:
            command = Command(cmd)
            parsed = parse_args(cmd, dict(args))
        except (ValueError, ChamberlainError) as error:
            raise CalendarBridgeFailure("bad_request", str(error)[:200], sent=False) from None
        try:
            reply = self._client.call(command, parsed, calendar=True)
        except BridgeFailure as failure:
            raise _failure(failure) from None
        return reply.result.model_dump(mode="json")


def _failure(failure: BridgeFailure) -> CalendarBridgeFailure:
    code = failure.bridge_code.value if failure.bridge_code is not None else "unavailable"
    return CalendarBridgeFailure(
        code, failure.detail[:200], sent=failure.sent, mutation=failure.mutation
    )
