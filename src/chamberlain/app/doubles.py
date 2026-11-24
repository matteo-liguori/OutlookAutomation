"""Stand-ins for the cross-track services, until the real ones are wired.

Contract: src/chamberlain/app/services.py. A track tests against these so it
never needs another track's unfinished code.
"""

from dataclasses import dataclass, field

from chamberlain.app.services import ArtifactInfo, Services
from chamberlain.domain.canonical import sha256_bytes
from chamberlain.domain.handles import HandleKind, new_handle, ref_handle
from chamberlain.schema.errors import ChamberlainError, ErrorCode


@dataclass
class AuditRow:
    actor: str
    kind: str
    subject: str | None
    summary: str
    payload_sha256: str | None


@dataclass
class RecordingAuditLog:
    rows: list[AuditRow] = field(default_factory=lambda: [])

    def append(
        self,
        *,
        actor: str,
        kind: str,
        subject: str | None,
        summary: str,
        payload_sha256: str | None = None,
    ) -> int:
        self.rows.append(AuditRow(actor, kind, subject, summary, payload_sha256))
        return len(self.rows)


@dataclass
class MemoryArtifactStore:
    """Artifacts in a dictionary. `put` is the test's way in."""

    blobs: dict[str, bytes] = field(default_factory=lambda: {})
    owners: dict[str, set[str]] = field(default_factory=lambda: {})

    def put(self, data: bytes, owner: str) -> str:
        handle = new_handle(HandleKind.ARTIFACT)
        self.blobs[handle] = data
        self.owners[handle] = {owner}
        return handle

    def info(self, handle: str) -> ArtifactInfo:
        data = self._get(handle)
        return ArtifactInfo(
            handle=handle,
            sha256=sha256_bytes(data),
            size=len(data),
            media_type="application/octet-stream",
            filename=None,
            live=bool(self.owners.get(handle)),
        )

    def read_bytes(self, handle: str) -> bytes:
        return self._get(handle)

    def add_owner(self, handle: str, owner: str) -> None:
        self._get(handle)
        self.owners.setdefault(handle, set()).add(owner)

    def release_owner(self, handle: str, owner: str) -> None:
        self.owners.get(handle, set()).discard(owner)

    def _get(self, handle: str) -> bytes:
        if handle not in self.blobs:
            raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "No such artifact.")
        return self.blobs[handle]


@dataclass
class MemoryEvidenceStore:
    items: dict[str, tuple[str, str | None]] = field(default_factory=lambda: {})
    links: dict[str, set[str]] = field(default_factory=lambda: {})

    def add(self, *, text: str, owner: str, session: str | None, ref: str | None = None) -> str:
        handle = new_handle(HandleKind.EVIDENCE)
        self.items[handle] = (text, session)
        self.links[handle] = {owner}
        return handle

    def link(self, evidence: str, owner: str) -> None:
        self.links[evidence].add(owner)

    def created_in_session(self, evidence: str, session: str) -> bool:
        return evidence in self.items and self.items[evidence][1] == session


@dataclass
class MemoryRefResolver:
    known: dict[str, tuple[str, str]] = field(default_factory=lambda: {})

    def locate(self, handle: str) -> tuple[str, str]:
        if handle not in self.known:
            raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "No such item.")
        return self.known[handle]

    def remember(
        self, kind: HandleKind, source: str, locator: str, internet_message_id: str | None = None
    ) -> str:
        handle = ref_handle(kind, source, locator)
        self.known[handle] = (source, locator)
        return handle


@dataclass
class RecordingWriteControl:
    revoked: list[str] = field(default_factory=lambda: [])
    cancelled: list[str] = field(default_factory=lambda: [])

    def revoke_grant(self, handle: str, *, actor: str) -> None:
        self.revoked.append(handle)

    def cancel_operation(self, handle: str, *, actor: str) -> None:
        self.cancelled.append(handle)


def unwired_services() -> Services:
    """In-memory services. Good for tests and for the skeleton's CLI."""
    return Services(
        audit=RecordingAuditLog(),
        artifacts=MemoryArtifactStore(),
        evidence=MemoryEvidenceStore(),
        refs=MemoryRefResolver(),
        writes=RecordingWriteControl(),
    )
