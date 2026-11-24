"""Sources that are out of date, unreachable or need the user to sign in (P-5)."""

from collections.abc import Iterable

from chamberlain.attention.models import DetectorInput, EventAttrs, EventDraft
from chamberlain.attention.tuning import PRIORITY
from chamberlain.domain.capabilities import HealthState
from chamberlain.domain.events import EventKind


def source_stale(data: DetectorInput) -> Iterable[EventDraft]:
    for source in data.sources:
        if source.state not in (HealthState.STALE, HealthState.UNAVAILABLE):
            continue
        what = "cannot be reached" if source.state is HealthState.UNAVAILABLE else "is out of date"
        yield EventDraft(
            EventKind.SOURCE_STALE,
            source.id,
            f"Source {source.id} {what}; its results may be incomplete.",
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
