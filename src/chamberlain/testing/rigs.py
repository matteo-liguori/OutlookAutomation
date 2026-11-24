"""What a connector provides to plug into the contract suites.

Contract: docs/contracts/08-ports.md (Contract suites). A rig wraps one port
and one disposable source, with helpers to seed and inspect it. `inject` and
`restart` return False when the connector cannot do that; the tests that need
it are then skipped, visibly.
"""

from typing import Protocol

from chamberlain.connectors.fake.world import Fault
from chamberlain.domain.calendar import EventFields
from chamberlain.domain.mail import FolderRole
from chamberlain.ports.calendar import CalendarPort
from chamberlain.ports.contacts import ContactsPort
from chamberlain.ports.mail import MailPort

__all__ = ["CalendarRig", "ContactsRig", "Fault", "MailRig"]


class _Faulty(Protocol):
    def inject(self, fault: Fault) -> bool:
        """Arm one fault for the next call. False if this rig cannot."""
        ...

    def restart(self) -> bool:
        """Restart the client so session IDs change. False if this rig cannot."""
        ...

    def advance(self, seconds: float) -> None:
        """Let time pass (a fake moves its clock; a live rig waits)."""
        ...


class MailRig(_Faulty, Protocol):
    port: MailPort
    source: str

    def folder_id(self, role: FolderRole) -> str: ...

    def filing_folder_id(self) -> str:
        """An ordinary folder to move mail into."""
        ...

    def new_message_id(self) -> str:
        """A unique RFC Message-ID carrying this run's marker."""
        ...

    def seed(
        self,
        *,
        internet_message_id: str,
        folder: FolderRole = FolderRole.INBOX,
        subject: str = "Contract test",
        body: str = "Body text.",
        unread: bool = True,
        attachments: tuple[tuple[str, bytes], ...] = (),
    ) -> str:
        """Put one message in place and return its stable locator."""
        ...

    def copies(self, internet_message_id: str, folder_id: str | None = None) -> int: ...

    def is_unread(self, locator: str) -> bool:
        """Read the flag by a route that cannot itself change it."""
        ...


class CalendarRig(_Faulty, Protocol):
    port: CalendarPort
    source: str

    def new_uid(self) -> str: ...

    def seed(self, fields: EventFields) -> str:
        """Create one event and return its locator."""
        ...

    def count(self, uid: str) -> int:
        """Live (not cancelled) events with this uid."""
        ...


class ContactsRig(Protocol):
    port: ContactsPort
    source: str

    def seed(self, name: str, *emails: str) -> str: ...
