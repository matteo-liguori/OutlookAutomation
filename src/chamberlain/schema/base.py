"""Base model, shared field types and the tool names.

Contract: docs/contracts/01-domain.md (handles, time) and 02-tools.md.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, StringConstraints

HANDLE_PREFIXES: tuple[str, ...] = (
    "card",
    "act",
    "note",
    "evd",
    "art",
    "grant",
    "op",
    "evt",
    "ses",
    "run",
    "msg",
    "cev",
    "con",
)
_TOKEN = r"_[a-z0-9]{8,32}$"


def handle_pattern(*prefixes: str) -> str:
    """Regular expression matching a handle with one of the given prefixes."""
    return "^(" + "|".join(prefixes) + ")" + _TOKEN


class Model(BaseModel):
    """Closed, immutable base for every exchanged shape (T-4)."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def _to_utc_seconds(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(microsecond=0)


Timestamp = Annotated[AwareDatetime, AfterValidator(_to_utc_seconds)]
"""UTC, second precision (D-7)."""

Handle = Annotated[str, StringConstraints(pattern=handle_pattern(*HANDLE_PREFIXES))]
CardHandle = Annotated[str, StringConstraints(pattern=handle_pattern("card"))]
ActionHandle = Annotated[str, StringConstraints(pattern=handle_pattern("act"))]
NoteHandle = Annotated[str, StringConstraints(pattern=handle_pattern("note"))]
EvidenceHandle = Annotated[str, StringConstraints(pattern=handle_pattern("evd"))]
ArtifactHandle = Annotated[str, StringConstraints(pattern=handle_pattern("art"))]
GrantHandle = Annotated[str, StringConstraints(pattern=handle_pattern("grant"))]
OperationHandle = Annotated[str, StringConstraints(pattern=handle_pattern("op"))]
EventHandle = Annotated[str, StringConstraints(pattern=handle_pattern("evt"))]
RunHandle = Annotated[str, StringConstraints(pattern=handle_pattern("run"))]
SessionHandle = Annotated[str, StringConstraints(pattern=handle_pattern("ses"))]
MessageHandle = Annotated[str, StringConstraints(pattern=handle_pattern("msg"))]
CalendarEventHandle = Annotated[str, StringConstraints(pattern=handle_pattern("cev"))]
ContactHandle = Annotated[str, StringConstraints(pattern=handle_pattern("con"))]

SOURCE_ID_PATTERN = r"^(mail|calendar|contacts):[a-z0-9-]{1,32}$"
SourceId = Annotated[str, StringConstraints(pattern=SOURCE_ID_PATTERN)]

OUTSIDE_SOURCE = "outside:owner"
"""The fixed source of an outside action (04-writes W-44). No connector stands behind it."""
WRITE_SOURCE_PATTERN = r"^((mail|calendar|contacts):[a-z0-9-]{1,32}|outside:owner)$"
WriteSourceId = Annotated[str, StringConstraints(pattern=WRITE_SOURCE_PATTERN)]
"""A source a write or a grant may name: a configured source, or the outside source."""

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
EmailAddress = Annotated[
    str,
    StringConstraints(pattern=r"^[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+$", to_lower=True),
]
"""A bare address, lower-cased (D-15)."""


class ToolName(StrEnum):
    """The twelve agent-facing tools (T-2)."""

    START = "start"
    NEXT = "next"
    GUIDE = "guide"
    FIND = "find"
    OPEN = "open"
    KEEP = "keep"
    FORGET = "forget"
    CARD = "card"
    PROPOSE = "propose"
    DO = "do"
    RECONCILE = "reconcile"
    DOCTOR = "doctor"
