"""Notes the person leaves for the assistant (the `user.note` event).

Contract: docs/contracts/07-attention.md. A note is short on purpose: its text is the event
summary the agent sees (160 characters at most). The audit row keeps a hash and the handle
of the actor, never the text (G-21).
"""

from chamberlain.app.context import Context
from chamberlain.domain.canonical import sha256_text
from chamberlain.runs import repo
from chamberlain.runs.tuning import NOTE_MAX_CHARS, VIEWER_ACTOR
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.store.db import transaction
from chamberlain.store.migrate import stamp


def clean_note(text: str) -> str:
    """One line of plain text within the limit, or `request.invalid`."""
    one_line = " ".join(text.split())
    if not one_line or len(one_line) > NOTE_MAX_CHARS or not one_line.isprintable():
        raise ChamberlainError(
            ErrorCode.REQUEST_INVALID,
            f"A note is one line of 1 to {NOTE_MAX_CHARS} characters.",
            field="text",
        )
    return one_line


def add_note(ctx: Context, text: str) -> int:
    """Store a note and audit it; returns its number. Attention raises it as `user.note`."""
    note = clean_note(text)
    with transaction(ctx.db):
        number = repo.insert_note(ctx.db, note, stamp(ctx.clock))
        ctx.services.audit.append(
            actor=VIEWER_ACTOR,
            kind="user.note",
            subject=None,
            summary="The person left a note for the assistant.",
            payload_sha256=sha256_text(note),
        )
    return number


def withdraw_note(ctx: Context, number: int) -> None:
    """The person takes a note back; the event expires at the next tick."""
    with transaction(ctx.db):
        if repo.set_note_state(ctx.db, number, "withdrawn") == 0:
            raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "There is no such note.")
        ctx.services.audit.append(
            actor=VIEWER_ACTOR,
            kind="user.note",
            subject=None,
            summary="The person withdrew a note.",
        )
