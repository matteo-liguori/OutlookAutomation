"""MailPort: what the core may ask of a mail connector.

Contract: docs/contracts/08-ports.md. The executable form is
`chamberlain.testing.mail_contract.MailPortContract`.
"""

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from chamberlain.domain.capabilities import CapabilitySet, SourceHealth
from chamberlain.domain.intents import MailIntent
from chamberlain.domain.mail import AttachmentBytes, Folder, MailQuery, Message, MessageSummary
from chamberlain.domain.operations import DispatchResult, Prepared, ReconcileResult
from chamberlain.ports.page import Page


@runtime_checkable
class MailPort(Protocol):
    def capabilities(self, source: str) -> CapabilitySet:
        """What this source can do, with limits and verification levels (P-1, P-2)."""
        ...

    def health(self, source: str) -> SourceHealth:
        """Reachability and freshness. Never a guess (P-5)."""
        ...

    # --- reading: MUST NOT change remote state, read flag included (P-3) ----

    def folders(self, source: str) -> list[Folder]: ...

    def search(
        self, source: str, query: MailQuery, cursor: str | None = None
    ) -> Page[MessageSummary]:
        """One page of matching physical copies, newest first (P-4, P-7)."""
        ...

    def read(self, source: str, locator: str) -> Message:
        """The message with its text body and attachment list."""
        ...

    def attachment(self, source: str, locator: str, attachment_id: str) -> AttachmentBytes:
        """The bytes with their hash; a mismatch raises and yields nothing (P-8)."""
        ...

    def resolve(self, source: str, locator: str) -> MessageSummary:
        """The current item for a stable locator, after any client restart (P-6)."""
        ...

    # --- writing: called only by the write engine, in this order -------------

    def prepare(self, intent: MailIntent, refs: Mapping[str, str]) -> Prepared:
        """Read-only. Resolve and check everything `dispatch` needs (P-9).

        `refs` maps each handle named in the intent to its stable locator; a
        connector never sees the store.
        """
        ...

    def dispatch(
        self, prepared: Prepared, key: str, payloads: Mapping[str, bytes]
    ) -> DispatchResult:
        """The one mutation (P-10). `payloads` maps attachment SHA-256 to bytes."""
        ...

    def reconcile(self, prepared: Prepared, key: str) -> ReconcileResult:
        """Read-only. Say whether the write is there (P-11, P-12)."""
        ...
