"""The seams between tracks.

Contract: docs/contracts/09-storage.md (Tables, S-3). A track reads another
track's tables with plain SELECT; anything more goes through one of these
protocols. The owning track implements it; `app/wiring.py` puts the
implementation into `Context.services`. Until then tests use the doubles in
`chamberlain.app.doubles`.

Shared file (docs/OWNERSHIP.md): change only via docs/BOARD.md.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from chamberlain.domain.handles import HandleKind


class AuditLog(Protocol):
    """Implemented by writes (05-grants.md, G-20 to G-23)."""

    def append(
        self,
        *,
        actor: str,
        kind: str,
        subject: str | None,
        summary: str,
        payload_sha256: str | None = None,
    ) -> int:
        """Append one row inside the caller's transaction; return its `seq`."""
        ...


@dataclass(frozen=True)
class ArtifactInfo:
    handle: str
    sha256: str
    size: int
    media_type: str
    filename: str | None
    live: bool


class ArtifactStore(Protocol):
    """Implemented by memory (06-memory.md, M-4 to M-11)."""

    def info(self, handle: str) -> ArtifactInfo:
        """Describe an artifact, or raise `request.unknown_handle`."""
        ...

    def read_bytes(self, handle: str) -> bytes:
        """The verified bytes (M-7), or `artifact.expired` / `artifact.hash_mismatch`."""
        ...

    def add_owner(self, handle: str, owner: str) -> None:
        """Add a hold, for example an operation holding its attachments (M-11)."""
        ...

    def release_owner(self, handle: str, owner: str) -> None: ...


class EvidenceStore(Protocol):
    """Implemented by memory (06-memory.md, M-15)."""

    def add(self, *, text: str, owner: str, session: str | None, ref: str | None = None) -> str:
        """Store one immutable statement under an owner; return its handle."""
        ...

    def link(self, evidence: str, owner: str) -> None:
        """Add a referrer, for example the grant an approval activated (G-13)."""
        ...

    def created_in_session(self, evidence: str, session: str) -> bool:
        """True if this evidence exists and was created by that session (G-13)."""
        ...


class RefResolver(Protocol):
    """Implemented by memory, which owns the `refs` table (01-domain.md, D-5)."""

    def locate(self, handle: str) -> tuple[str, str]:
        """`(source id, stable locator)` for a msg, cev or con handle."""
        ...

    def remember(
        self, kind: HandleKind, source: str, locator: str, internet_message_id: str | None = None
    ) -> str:
        """Record a remote item and return its deterministic handle (D-6)."""
        ...

    def repoint(
        self, old_locator: str, new_locator: str, *, source: str | None = None
    ) -> str | None:
        """Keep a handle alive after a move or draft update changed the locator (R4).

        Returns the surviving handle, or None if the old locator was unknown.
        """
        ...


class WriteControl(Protocol):
    """Implemented by writes; lets `forget` reach grants and operations (T-11)."""

    def revoke_grant(self, handle: str, *, actor: str) -> None: ...

    def cancel_operation(self, handle: str, *, actor: str) -> None: ...

    def own_addresses(self, source: str) -> frozenset[str]:
        """The mailbox address(es) the source is bound to, lower-cased."""
        ...

    def learn_recipients(self, source: str, addresses: Iterable[str]) -> int:
        """Record people the owner wrote to as known recipients (origin sent-history, G-15).

        The source's own addresses and `.invalid` addresses are skipped. Never touches grants.
        Runs inside the caller's transaction; returns how many were new.
        """
        ...


@dataclass(frozen=True)
class Services:
    audit: AuditLog
    artifacts: ArtifactStore
    evidence: EvidenceStore
    refs: RefResolver
    writes: WriteControl
