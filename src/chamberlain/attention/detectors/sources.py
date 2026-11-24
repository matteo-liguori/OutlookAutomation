"""Sources that are out of date, unreachable or need the user to sign in (P-5)."""

from collections.abc import Iterable

from chamberlain.attention.models import DetectorInput, EventAttrs, EventDraft
from chamberlain.attention.tuning import PRIORITY
from chamberlain.domain.capabilities import HealthState
from chamberlain.domain.events import EventKind


def _span(stamp: str) -> str:
    return stamp.replace("T", " ").removesuffix("Z")[:16]


OLDER = "older_mail"
"""`attrs.reason` of the event that asks about, or reports, mail held back as too old (A-23)."""


def _held_events(data: DetectorInput) -> Iterable[EventDraft]:
    for held in data.mail_held:
        span = f"{_span(held.start)} to {_span(held.end)} UTC"
        size = "" if held.estimate is None else f", about {held.estimate} messages"
        if held.state == "asking":
            text = f"{held.source}: mail from {span}{size} was not read. Ask the owner."
        elif held.state == "approved":
            text = f"{held.source}: reading older mail; {span} is not covered yet."
        else:
            text = f"{held.source}: mail from {span} was left unread at the owner's word."
        yield EventDraft(
            EventKind.SOURCE_STALE,
            held.source,
            text,
            PRIORITY[EventKind.SOURCE_STALE],
            EventAttrs(source=held.source, reason=OLDER),
            discriminator=f"older:{held.end}",
        )


def source_stale(data: DetectorInput) -> Iterable[EventDraft]:
    yield from _held_events(data)
    gaps = {gap.source: gap for gap in data.mail_gaps}
    for source in data.sources:
        gap = gaps.get(source.id)
        if source.state is HealthState.SIGN_IN_NEEDED:
            continue
        stale = source.state in (HealthState.STALE, HealthState.UNAVAILABLE)
        if not stale and gap is None:
            continue
        what = "cannot be reached" if source.state is HealthState.UNAVAILABLE else "is out of date"
        text = f"Source {source.id} {what}; its results may be incomplete."
        if gap is not None:
            text = (
                f"Source {source.id}: mail from {_span(gap.start)} to {_span(gap.end)} UTC "
                "is not covered yet."
            )
        yield EventDraft(
            EventKind.SOURCE_STALE,
            source.id,
            text,
            PRIORITY[EventKind.SOURCE_STALE],
            EventAttrs(source=source.id, health=source.state),
        )


def source_sign_in_needed(data: DetectorInput) -> Iterable[EventDraft]:
    for source in data.sources:
        if source.state is HealthState.SIGN_IN_NEEDED:
            yield EventDraft(
                EventKind.SOURCE_SIGN_IN_NEEDED,
                source.id,
                f"Source {source.id} needs the user to sign in.",
                PRIORITY[EventKind.SOURCE_SIGN_IN_NEEDED],
                EventAttrs(source=source.id, health=source.state),
            )
