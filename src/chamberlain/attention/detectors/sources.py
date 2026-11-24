"""Sources that are out of date, unreachable or need the user to sign in (P-5)."""

from collections.abc import Iterable

from chamberlain.attention.models import DetectorInput, EventAttrs, EventDraft
from chamberlain.attention.tuning import PRIORITY
from chamberlain.domain.capabilities import HealthState
from chamberlain.domain.events import EventKind


def _span(stamp: str) -> str:
    return stamp.replace("T", " ").removesuffix("Z")[:16]


def source_stale(data: DetectorInput) -> Iterable[EventDraft]:
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
