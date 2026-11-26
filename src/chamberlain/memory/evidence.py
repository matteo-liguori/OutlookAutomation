"""Evidence: small immutable statements, deleted when nothing refers to them.

Contract: docs/contracts/06-memory.md (M-15). Implements `EvidenceStore`
Evidence never holds a mail body, a card dump or a file.
"""

import json
import re
import sqlite3
from typing import cast

from chamberlain.domain.cards import EvidenceOwnerKind, evidence_owner_kind
from chamberlain.domain.handles import HandleKind, new_handle
from chamberlain.domain.timefmt import ts
from chamberlain.limits import EVIDENCE_MAX
from chamberlain.memory import repo
from chamberlain.ports.clock import Clock
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.store.db import transaction

_HEADER_LINE = re.compile(r"^(from|to|cc|subject|date|sent):\s", re.IGNORECASE | re.MULTILINE)
_QUOTED = re.compile(r"-----\s*original message\s*-----|^on .{5,80} wrote:\s*$", re.I | re.M)
_CARD_KEYS = frozenset({"title", "goal", "position", "rules", "actions", "version", "state"})
_OWNER_TABLE = {
    EvidenceOwnerKind.CARD: "card",
    EvidenceOwnerKind.ACTION: "action",
    EvidenceOwnerKind.GRANT: "grant",
    EvidenceOwnerKind.OPERATION: "operation",
}


def _refuse(message: str) -> ChamberlainError:
    return ChamberlainError(
        ErrorCode.EVIDENCE_OVER_CAP,
        message,
        fix="State the fact in a sentence; store long content as an artifact instead.",
        field="text",
    )


def check_statement(text: str) -> None:
    """Refuse text that is too long, a mail body or a card dump (M-12, M-15)."""
    if len(text) > EVIDENCE_MAX:
        raise _refuse(f"The statement is {len(text)} characters; the limit is {EVIDENCE_MAX}.")
    if len(_HEADER_LINE.findall(text)) >= 3 or _QUOTED.search(text):
        raise _refuse("Evidence is a statement, not a copy of a message.")
    stripped = text.strip()
    if stripped[:1] in ("{", "["):
        try:
            parsed = cast(object, json.loads(stripped))
        except ValueError:
            return
        if isinstance(parsed, dict):
            keys = {str(k) for k in cast(dict[object, object], parsed)}
        else:
            return
        if len(_CARD_KEYS & keys) >= 2:
            raise _refuse("Evidence is a statement, not a dump of a card.")


class EvidenceService:
    def __init__(self, db: sqlite3.Connection, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    def _owner(self, owner: str) -> EvidenceOwnerKind:
        kind = evidence_owner_kind(owner)
        if kind is None or not repo.owner_exists(self._db, _OWNER_TABLE[kind], owner):
            raise ChamberlainError(
                ErrorCode.ARTIFACT_NO_OWNER,
                "Evidence belongs to a card, action, grant or operation.",
                field="owner",
            )
        live = True
        if kind is EvidenceOwnerKind.CARD:
            card = repo.get_card(self._db, owner)
            live = card is not None and str(card["state"]) == "open"
        elif kind is EvidenceOwnerKind.ACTION:
            action = repo.get_action(self._db, owner)
            live = action is not None and str(action["state"]) == "open"
        if not live:
            raise ChamberlainError(
                ErrorCode.ARTIFACT_OWNER_ENDED, "That owner has finished.", field="owner"
            )
        return kind

    def add(self, *, text: str, owner: str, session: str | None, ref: str | None = None) -> str:
        """Store one immutable statement under an owner; return its handle."""
        check_statement(text)
        kind = self._owner(owner)
        handle = new_handle(HandleKind.EVIDENCE)
        now = ts(self._clock.now())
        with transaction(self._db):
            repo.insert_evidence(
                self._db,
                handle=handle,
                text=text,
                ref=ref,
                subject_sha256=None,
                session=session,
                now=now,
            )
            repo.add_evidence_ref(self._db, handle, kind.value, owner, now)
        return handle

    def link(self, evidence: str, owner: str) -> None:
        """Add a referrer, for example the grant an approval activated (G-13)."""
        if repo.get_evidence(self._db, evidence) is None:
            raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "No such evidence.")
        kind = self._owner(owner)
        with transaction(self._db):
            repo.add_evidence_ref(self._db, evidence, kind.value, owner, ts(self._clock.now()))

    def created_in_session(self, evidence: str, session: str) -> bool:
        row = repo.get_evidence(self._db, evidence)
        return row is not None and row["session"] == session

    def release(self, evidence: str, owner: str | None) -> tuple[bool, int]:
        """Drop one referrer or all; delete the statement at once when none remain (M-15)."""
        if repo.get_evidence(self._db, evidence) is None:
            raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "No such evidence.")
        with transaction(self._db):
            repo.remove_evidence_ref(self._db, evidence, owner)
            remaining = repo.evidence_referrers(self._db, evidence)
            if remaining == 0:
                repo.delete_unreferenced_evidence(self._db)
        return remaining == 0, remaining
