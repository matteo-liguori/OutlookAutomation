"""Refs: stable handles for remote messages, events and contacts.

Contract: docs/contracts/01-domain.md (D-5, D-6), 06-memory.md (M-20) and the
board ruling R4 (`repoint`). A ref
is a pointer, never content: it maps a handle to a source and a stable locator.
"""

import sqlite3
from collections.abc import Iterable

from chamberlain.domain.handles import REF_KINDS, HandleKind, ref_handle
from chamberlain.domain.timefmt import ts
from chamberlain.memory import repo
from chamberlain.ports.clock import Clock
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.store.db import transaction

_KIND_NAME = {
    HandleKind.MESSAGE: "message",
    HandleKind.CALENDAR_EVENT: "calendar_event",
    HandleKind.CONTACT: "contact",
}


def remember(
    db: sqlite3.Connection,
    clock: Clock,
    kind: HandleKind,
    source: str,
    locator: str,
    internet_message_id: str | None = None,
) -> str:
    """Record a remote item and return its handle (D-6).

    The same source and locator always give the same handle. A handle that was
    repointed after a write keeps naming the item under its new locator.
    Distinct physical copies have distinct locators and so distinct handles (M-20).
    """
    return remember_many(db, clock, kind, source, [(locator, internet_message_id)])[locator]


def remember_many(
    db: sqlite3.Connection,
    clock: Clock,
    kind: HandleKind,
    source: str,
    copies: Iterable[tuple[str, str | None]],
) -> dict[str, str]:
    """Record one source's refs in one transaction, retaining repointed handles."""
    if kind not in REF_KINDS:
        raise ChamberlainError(ErrorCode.REQUEST_WRONG_KIND, "Only remote items have refs.")
    name = _KIND_NAME[kind]
    now = ts(clock.now())
    handles: dict[str, str] = {}
    with transaction(db):
        for locator, internet_message_id in copies:
            existing = repo.find_ref(db, source, name, locator)
            if existing is not None:
                handle = str(existing["handle"])
                repo.touch_ref(db, handle, internet_message_id, now)
            else:
                handle = ref_handle(kind, source, locator)
                if repo.get(db, "refs", handle) is not None:
                    handle = ref_handle(kind, source, f"{locator}\n{now}")
                repo.insert_ref(
                    db,
                    handle=handle,
                    kind=name,
                    source=source,
                    locator=locator,
                    internet_message_id=internet_message_id,
                    now=now,
                )
            handles[locator] = handle
    return handles


def existing_handles(
    db: sqlite3.Connection, kind: HandleKind, source: str, locators: Iterable[str]
) -> dict[str, str]:
    return repo.ref_handles(db, source, _KIND_NAME[kind], list(locators))


def locate(db: sqlite3.Connection, handle: str) -> tuple[str, str]:
    """`(source id, stable locator)` for a msg, cev or con handle."""
    row = repo.get(db, "refs", handle)
    if row is None:
        raise ChamberlainError(
            ErrorCode.REQUEST_UNKNOWN_HANDLE, "That item is no longer known; find it again."
        )
    return str(row["source_id"]), str(row["locator"])


def repoint(
    db: sqlite3.Connection,
    clock: Clock,
    old_locator: str,
    new_locator: str,
    *,
    source: str | None = None,
) -> str | None:
    """Make the handle that named `old_locator` name `new_locator` (R4).

    The write engine calls this on a confirmed move, trash or draft update so
    handles held by cards, evidence and operations survive the change. Returns
    the surviving handle, or None when nothing referred to the old locator.
    """
    now = ts(clock.now())
    with transaction(db):
        found = repo.refs_by_locator(db, old_locator, source)
        if not found:
            return None
        if len(found) > 1:
            raise ChamberlainError(
                ErrorCode.REMOTE_AMBIGUOUS,
                "More than one source has that locator.",
                fix="Pass the source when repointing a locator.",
            )
        old = found[0]
        handle = str(old["handle"])
        clash = repo.find_ref(db, str(old["source_id"]), str(old["kind"]), new_locator)
        if clash is not None and str(clash["handle"]) != handle:
            clashing = str(clash["handle"])
            repo.delete(db, "refs", "handle", clashing)
            repo.rewrite_ref_users(db, clashing, handle)
        repo.set_ref_locator(db, handle, new_locator, now)
    return handle


class RefService:
    """Binds the module functions to a store and a clock."""

    def __init__(self, db: sqlite3.Connection, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    def locate(self, handle: str) -> tuple[str, str]:
        return locate(self._db, handle)

    def remember(
        self, kind: HandleKind, source: str, locator: str, internet_message_id: str | None = None
    ) -> str:
        return remember(self._db, self._clock, kind, source, locator, internet_message_id)

    def repoint(
        self, old_locator: str, new_locator: str, *, source: str | None = None
    ) -> str | None:
        return repoint(self._db, self._clock, old_locator, new_locator, source=source)
