"""Work items and notices: one event, one harness line, one exact call (A-9, E-4, E-6).

Contract: docs/contracts/07-attention.md (Delivery) and 03-envelope-errors.md.
"""

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
