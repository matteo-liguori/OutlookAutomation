"""Paged lists: a character budget per page and immutable, session-bound cursors.

Contract: docs/contracts/06-memory.md (M-17, M-18) and 03-envelope-errors.md
(E-3). A list is gathered once into a snapshot; a cursor continues over that
snapshot, so a continuation sees exactly the collection it started with.
"""

import secrets
from dataclasses import dataclass

from pydantic import BaseModel

from chamberlain.app.context import Context
from chamberlain.domain.canonical import sha256_text
from chamberlain.limits import CURSOR_HOURS, HIT_LINE_MAX, PAGE_CHARS
from chamberlain.memory import repo
from chamberlain.memory.timeutil import plus, ts
from chamberlain.schema.base import Model
from chamberlain.schema.envelope import ContentKind, ContentLine, Coverage, CoverageGap
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.schema.tools import Hit
from chamberlain.store.db import transaction

SNAPSHOT_MAX = 200
"""Most hits one search keeps in its snapshot."""


class Snapshot(Model):
    """The stored form of a cursor's collection (S-2: one named model)."""

    hits: tuple[Hit, ...] = ()
    gaps: tuple[CoverageGap, ...] = ()
    incomplete: bool = False
    """The source had more than the snapshot holds; the reply must say so."""


@dataclass(frozen=True)
class Page:
    hits: tuple[Hit, ...]
    coverage: Coverage


def query_hash(tool: str, request: BaseModel) -> str:
    """Identifies the question, ignoring the paging fields."""
    body = request.model_dump_json(exclude={"cursor", "limit", "session"}, exclude_none=True)
    return sha256_text(f"{tool}\n{body}")


def one_line(text: str, kind: ContentKind, origin: str) -> ContentLine:
    """Remote or stored text as a single bounded line (T-8, E-7)."""
    flat = " ".join(text.split())
    truncated = len(flat) > HIT_LINE_MAX
    return ContentLine(origin=origin, kind=kind, text=flat[:HIT_LINE_MAX], truncated=truncated)


def _fit(hits: tuple[Hit, ...], start: int, limit: int) -> tuple[Hit, ...]:
    """Hits from `start`, at most `limit`, within the page's character budget."""
    chosen: list[Hit] = []
    used = 0
    for hit in hits[start : start + limit]:
        size = len(hit.model_dump_json(exclude_none=True)) + 1
        if chosen and used + size > PAGE_CHARS:
            break
        chosen.append(hit)
        used += size
    return tuple(chosen)


def _coverage(
    snapshot: Snapshot, shown: int, start: int, cursor: str | None, total: int
) -> Coverage:
    more = start + shown < total
    incomplete = more or bool(snapshot.gaps) or snapshot.incomplete
    gaps = snapshot.gaps
    if snapshot.incomplete and not gaps and not more:
        raise ChamberlainError(ErrorCode.INTERNAL_UNEXPECTED, "An incomplete result has no gap.")
    return Coverage(
        complete=not incomplete,
        returned=shown,
        total=total,
        cursor=cursor if more else None,
        gaps=gaps,
    )


def first_page(ctx: Context, tool: str, qhash: str, snapshot: Snapshot, limit: int) -> Page:
    """The first page of a fresh snapshot; stores a cursor only when more remain."""
    shown = _fit(snapshot.hits, 0, limit)
    cursor: str | None = None
    if len(shown) < len(snapshot.hits):
        cursor = _store(ctx, tool, qhash, snapshot) + f".{len(shown)}"
    return Page(shown, _coverage(snapshot, len(shown), 0, cursor, len(snapshot.hits)))


def next_page(ctx: Context, tool: str, qhash: str, cursor: str, limit: int) -> Page:
    """Continue a cursor: same session, same question, within two hours (M-17)."""
    cursor_id, _, raw = cursor.partition(".")
    if not raw.isdigit():
        raise ChamberlainError(ErrorCode.REQUEST_BAD_CURSOR, "That cursor is not valid.")
    row = repo.get_cursor(ctx.db, cursor_id)
    if row is None:
        raise ChamberlainError(ErrorCode.REQUEST_BAD_CURSOR, "That cursor is not known.")
    if str(row["expires_at"]) <= ts(ctx.clock.now()):
        raise ChamberlainError(ErrorCode.CURSOR_EXPIRED, "That cursor has expired.")
    if (
        row["session"] != ctx.session
        or str(row["tool"]) != tool
        or str(row["query_sha256"]) != qhash
    ):
        raise ChamberlainError(
            ErrorCode.REQUEST_BAD_CURSOR, "That cursor belongs to a different question."
        )
    snapshot = Snapshot.model_validate_json(str(row["payload_json"]))
    start = int(raw)
    shown = _fit(snapshot.hits, start, limit)
    if not shown:
        raise ChamberlainError(ErrorCode.REQUEST_BAD_CURSOR, "That cursor is past the end.")
    following = f"{cursor_id}.{start + len(shown)}"
    return Page(shown, _coverage(snapshot, len(shown), start, following, len(snapshot.hits)))


def _store(ctx: Context, tool: str, qhash: str, snapshot: Snapshot) -> str:
    if ctx.session is None or not repo.session_exists(ctx.db, ctx.session):
        raise ChamberlainError(ErrorCode.SESSION_REQUIRED, "That session is not open.")
    cursor_id = f"cur{secrets.token_hex(8)}"
    now = ctx.clock.now()
    with transaction(ctx.db):
        repo.insert_cursor(
            ctx.db,
            cursor_id=cursor_id,
            session=ctx.session,
            tool=tool,
            query_sha256=qhash,
            payload_json=snapshot.model_dump_json(exclude_none=True),
            now=ts(now),
            expires_at=plus(now, hours=CURSOR_HOURS),
        )
    return cursor_id
