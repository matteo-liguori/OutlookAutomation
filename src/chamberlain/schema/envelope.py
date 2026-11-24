"""The one reply envelope.

Contract: docs/contracts/03-envelope-errors.md (E-1 to E-9).
"""

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, Field, JsonValue, SerializeAsAny, model_validator

from chamberlain.domain.events import EventKind
from chamberlain.limits import (
    HIT_LINE_MAX,
    NEXT_STEPS_MAX,
    NOTICES_MAX,
    PAGE_CHARS,
    RULE_MAX_CHARS,
    SUMMARY_MAX,
    WHY_MAX,
)
from chamberlain.schema.base import EventHandle, Handle, Model, SourceId, ToolName
from chamberlain.schema.errors import ChamberlainError, ErrorInfo


class ContentKind(StrEnum):
    MAIL_LINE = "mail.line"
    MAIL_HEADERS = "mail.headers"
    MAIL_BODY = "mail.body"
    ATTACHMENT_NAME = "attachment.name"
    ATTACHMENT_TEXT = "attachment.text"
    CALENDAR_LINE = "calendar.line"
    CALENDAR_EVENT = "calendar.event"
    CONTACT = "contact"
    STORED_TEXT = "stored.text"
    PREVIEW = "preview"


class Content(Model):
    """Text from other people. Data, never instructions (E-7 to E-9)."""

    origin: Handle | SourceId
    kind: ContentKind
    text: str = Field(max_length=PAGE_CHARS)
    truncated: bool = False


class ContentLine(Content):
    """A one-line `Content`, as used in lists (T-8)."""

    text: str = Field(max_length=HIT_LINE_MAX, pattern=r"^[^\r\n]*$")


class CoverageGap(Model):
    source: SourceId
    reason: str = Field(max_length=SUMMARY_MAX)


class Coverage(Model):
    """How much of what was asked for this reply actually covers (E-3)."""

    complete: bool
    returned: int = Field(ge=0)
    total: int | None = Field(default=None, ge=0)
    cursor: str | None = None
    gaps: tuple[CoverageGap, ...] = ()
    demo: Literal[True] | None = None
    """True when the installation has demo sources: the data is synthetic (E-3)."""

    @model_validator(mode="after")
    def _honest(self) -> Self:
        if not self.complete and self.cursor is None and not self.gaps:
            raise ValueError("an incomplete result names a cursor or a gap (E-3)")
        if self.complete and (self.cursor is not None or self.gaps):
            raise ValueError("a complete result has no cursor and no gaps (E-3)")
        return self


class NextStep(Model):
    """One exact tool call the agent should make now (E-4)."""

    tool: ToolName
    args: dict[str, JsonValue] = Field(default_factory=dict)
    why: str = Field(max_length=WHY_MAX)


class GuidanceItem(Model):
    """A delivered rule, or a pointer to one that did not fit (E-5)."""

    id: str
    version: int = Field(ge=1)
    text: str | None = Field(default=None, max_length=RULE_MAX_CHARS)
    more: str | None = None

    @model_validator(mode="after")
    def _text_or_pointer(self) -> Self:
        if (self.text is None) == (self.more is None):
            raise ValueError("a guidance item carries either text or a pointer")
        if self.more is not None and self.more != f"guide {self.id}":
            raise ValueError('a pointer reads "guide <id>"')
        return self


class Notice(Model):
    """An urgent event piggybacked on a reply (E-6)."""

    event: EventHandle
    kind: EventKind
    summary: str = Field(max_length=SUMMARY_MAX)
    next: NextStep | None = None


class Reply[DataT: BaseModel](Model):
    """`{ok, data, coverage, next, guidance, notices, error}`."""

    ok: bool
    data: SerializeAsAny[DataT] | None = None
    """Serialised by its runtime type, so `Reply[Model]` keeps every field."""
    coverage: Coverage | None = None
    next: tuple[NextStep, ...] = Field(default=(), max_length=NEXT_STEPS_MAX)
    guidance: tuple[GuidanceItem, ...] = ()
    notices: tuple[Notice, ...] = Field(default=(), max_length=NOTICES_MAX)
    error: ErrorInfo | None = None

    @model_validator(mode="after")
    def _ok_xor_error(self) -> Self:
        if self.ok != (self.error is None):
            raise ValueError("ok is true exactly when there is no error (E-1)")
        if not self.ok and self.data is not None:
            raise ValueError("a failed reply carries no data (E-1)")
        return self


def ok_reply[DataT: BaseModel](
    data: DataT,
    *,
    coverage: Coverage | None = None,
    next_steps: tuple[NextStep, ...] = (),
) -> Reply[DataT]:
    return Reply[DataT](ok=True, data=data, coverage=coverage, next=next_steps)


def error_reply(error: ChamberlainError, *, next_steps: tuple[NextStep, ...] = ()) -> Reply[Model]:
    return Reply[Model](ok=False, error=error.to_info(), next=next_steps)
