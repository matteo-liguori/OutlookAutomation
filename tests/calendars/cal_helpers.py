"""Shared test setup for provider invitation responses."""

from typing import Literal

from chamberlain.domain.handles import HandleKind, ref_handle
from chamberlain.domain.intents import CalendarRespond
from chamberlain.domain.operations import Prepared
from chamberlain.ports.calendar import CalendarPort


def prepare_answer(
    port: CalendarPort,
    source: str,
    locator: str,
    organiser: str,
    response: Literal["accepted", "tentative", "declined"],
    *,
    expected_etag: str | None = None,
) -> Prepared:
    handle = ref_handle(HandleKind.CALENDAR_EVENT, source, locator)
    intent = CalendarRespond(
        source=source,
        event_ref=handle,
        expected_etag=expected_etag
        if expected_etag is not None
        else port.read(source, locator).etag,
        response=response,
        organiser=organiser,
    )
    return port.prepare(intent, {handle: locator})
