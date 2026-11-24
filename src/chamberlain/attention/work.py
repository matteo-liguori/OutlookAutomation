"""Work items and notices: one event, one harness line, one exact call (A-9, E-4, E-6).

Contract: docs/contracts/07-attention.md (Delivery) and 03-envelope-errors.md.
"""

from pydantic import JsonValue

from chamberlain.attention.models import EventRow
from chamberlain.domain.events import EventKind
from chamberlain.schema.base import ToolName
from chamberlain.schema.envelope import NextStep, Notice
from chamberlain.schema.tools import WorkItem


def next_step(event: EventRow) -> NextStep:
    """The one call that deals with the event. Always validates against its tool (E-4)."""
    subject = event.subject
    kind = event.kind
    if kind is EventKind.OPERATION_UNCERTAIN and subject is not None:
        return NextStep(
            tool=ToolName.RECONCILE,
            args={"operation": subject},
            why="Read what happened. Never send again.",
        )
    if kind is EventKind.SEND_MISSING and subject is not None:
        if event.attrs.reason == "missing":
            return NextStep(
                tool=ToolName.PROPOSE,
                args={"redo_of": subject},
                why="Weigh it first. Make this call only if you decide it should go again.",
            )
        return NextStep(
            tool=ToolName.OPEN,
            args={"handle": subject, "part": "summary"},
            why="Read where it is. Do not send it again.",
        )
    if kind is EventKind.SOURCE_STALE and event.attrs.reason == "older_mail":
        return NextStep(
            tool=ToolName.DOCTOR,
            args={"check": "sources"},
            why="See the span, then ask the owner whether to read it.",
        )
    if kind in (EventKind.SOURCE_STALE, EventKind.SOURCE_SIGN_IN_NEEDED):
        return NextStep(
            tool=ToolName.DOCTOR,
            args={"check": "sources"},
            why="See which source has the problem.",
        )
    if kind in (EventKind.STORAGE_OVER_BUDGET, EventKind.STORAGE_ORPHANS):
        return NextStep(
            tool=ToolName.DOCTOR,
            args={"check": "storage", "metrics": True},
            why="See what is using the space.",
        )
    if kind is EventKind.SCHEDULE_PASS_DUE:
        return NextStep(
            tool=ToolName.NEXT,
            args={},
            why="Do the pass. Keep its short report with the run named in the item.",
        )
    if kind is EventKind.REPORT_DUE and subject is not None:
        return NextStep(
            tool=ToolName.OPEN,
            args={"handle": subject, "part": "body"},
            why="Read what to write, then keep the report under the run.",
        )
    if kind is EventKind.CALENDAR_CONFLICT and event.attrs.since and event.attrs.until:
        return NextStep(
            tool=ToolName.FIND,
            args={
                "scope": "calendar",
                "since": event.attrs.since,
                "until": event.attrs.until,
                "limit": 10,
            },
            why="List what is on every calendar then; open both events before deciding.",
        )
    if kind is EventKind.HARNESS_REVIEW:
        return NextStep(
            tool=ToolName.OPEN,
            args={"handle": event.handle, "part": "body"},
            why="Read the list, then keep what you find. Leave it out of any report.",
        )
    if kind is EventKind.USER_NOTE:
        return NextStep(
            tool=ToolName.NEXT,
            args={},
            why="The note is in the line above. Act on it, then mark it handled.",
        )
    if kind is EventKind.MAIL_NEW_UNMATCHED and subject is not None:
        args: dict[str, JsonValue] = {"scope": "mail", "source": subject}
        if event.attrs.reason == "sent":
            args["folder"] = "sent"
        if event.attrs.since is not None:
            args["since"] = event.attrs.since
        args["limit"] = 25
        why = "List the new mail; open only what you need."
        if event.attrs.reason == "sent":
            why = "List what you sent; see whether it settles a card."
        return NextStep(tool=ToolName.FIND, args=args, why=why)
    if subject is not None:
        return NextStep(
            tool=ToolName.OPEN,
            args={"handle": subject, "part": "summary"},
            why="Read it before deciding.",
        )
    return NextStep(
        tool=ToolName.DOCTOR, args={"check": "all"}, why="Check the harness for details."
    )


def work_item(event: EventRow) -> WorkItem:
    return WorkItem(
        event=event.handle,
        kind=event.kind,
        subject=event.subject,
        summary=event.summary,
        next=next_step(event),
    )


def notice(event: EventRow) -> Notice:
    return Notice(event=event.handle, kind=event.kind, summary=event.summary, next=next_step(event))
