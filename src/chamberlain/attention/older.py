"""The owner's answer about older mail (A-23).

A scan reads the last day of a long uncovered span at once and holds the older part, because
reading it costs tokens. The agent asks the owner. A yes arrives as `next` with the question's
event listed under `older_mail`; a question marked handled without that is a no. A no leaves the
span uncovered and visible: the span is reported again as its own `source.stale` event and
`doctor` names it. A yes can still come later, by the same call, from that event's handle.
Every answer is audited.
"""

from collections.abc import Iterable

from chamberlain.app.context import Context
from chamberlain.attention import inbox, repo
from chamberlain.attention.detectors.sources import OLDER, OLDER_DECLINED
from chamberlain.attention.models import EventRow
from chamberlain.domain.events import EventKind
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.store.db import transaction


def _is_question(event: EventRow) -> bool:
    return (
        event.kind is EventKind.SOURCE_STALE
        and event.attrs.reason in (OLDER, OLDER_DECLINED)
        and event.subject is not None
    )


def _current(ctx: Context, event: EventRow) -> repo.Held | None:
    """The held span this question is about, or None when it has moved on or is gone."""
    if event.subject is None:
        return None
    state = repo.watermark(ctx.db, event.subject)
    held = None if state is None else state.held
    if held is None:
        return None
    ends = (f":older:{held.end}", f":declined:{held.end}")
    return held if event.dedupe_key.endswith(ends) else None


def next_with_answers(
    ctx: Context, handled: Iterable[str], yes: Iterable[str], *, actor: str
) -> None:
    """Mark events handled and record the owner's answers to old-mail questions."""
    said_yes = list(dict.fromkeys(yes))
    wanted = list(dict.fromkeys([*handled, *said_yes]))
    with transaction(ctx.db):
        events = {e.handle: e for e in repo.events_by_handles(ctx.db, wanted)}
        for handle in said_yes:
            event = events.get(handle)
            if event is None or not _is_question(event):
                raise ChamberlainError(
                    ErrorCode.REQUEST_INVALID,
                    "Only an old-mail question can be answered yes.",
                    fix="List only the question's own event under the owner's yes.",
                    field="older_mail",
                )
            if _current(ctx, event) is None:
                raise ChamberlainError(
                    ErrorCode.REQUEST_INVALID,
                    "That old-mail question is out of date.",
                    fix="Call next again and ask the owner about the question it shows now.",
                    field="older_mail",
                )
        inbox.handle_events(ctx.db, ctx.clock, wanted)
        for handle in wanted:
            event = events.get(handle)
            if event is None or not _is_question(event) or event.subject is None:
                continue
            held = _current(ctx, event)
            if held is None:
                continue
            answered_yes = handle in said_yes
            if answered_yes and held.state != "approved":
                repo.held_state(ctx.db, event.subject, "approved")
                ctx.services.audit.append(
                    actor=actor,
                    kind="mail.older_yes",
                    subject=event.subject,
                    summary=f"Owner said to read older mail of {event.subject} from "
                    f"{held.start} to {held.end}.",
                )
            elif not answered_yes and held.state == "asking":
                repo.held_state(ctx.db, event.subject, "declined")
                ctx.services.audit.append(
                    actor=actor,
                    kind="mail.older_no",
                    subject=event.subject,
                    summary=f"Older mail of {event.subject} from {held.start} to {held.end} "
                    "was left unread.",
                )
