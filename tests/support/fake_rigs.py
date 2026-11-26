"""Rigs over the fake provider. Every track may use these in its own tests."""

from itertools import count

from chamberlain.connectors.fake import FakeProvider, Fault
from chamberlain.domain.calendar import EventFields, Response
from chamberlain.domain.mail import FolderRole
from chamberlain.ports.calendar import CalendarPort
from chamberlain.ports.contacts import ContactsPort
from chamberlain.ports.mail import MailPort

_ROLE_IDS = {
    FolderRole.INBOX: "f-inbox",
    FolderRole.SENT: "f-sent",
    FolderRole.DRAFTS: "f-drafts",
    FolderRole.TRASH: "f-trash",
    FolderRole.ARCHIVE: "f-archive",
}


class _FakeFaults:
    def __init__(self, fake: FakeProvider) -> None:
        self.fake = fake
        self._serial = count(1)

    def inject(self, fault: Fault) -> bool:
        self.fake.arm(fault)
        return True

    def restart(self) -> bool:
        self.fake.restart()
        return True

    def advance(self, seconds: float) -> None:
        self.fake.clock.advance(seconds=seconds)


class FakeMailRig(_FakeFaults):
    def __init__(self, fake: FakeProvider | None = None, source: str = "mail:test") -> None:
        super().__init__(fake or FakeProvider())
        self.source = source
        self.fake.mail.add_source(source)
        self.fake.mail.add_folder(source, "f-filed", "Filed")
        self.port: MailPort = self.fake.mail

    def folder_id(self, role: FolderRole) -> str:
        return _ROLE_IDS[role]

    def filing_folder_id(self) -> str:
        return "f-filed"

    def new_message_id(self) -> str:
        return f"<contract-{next(self._serial)}@fake.example>"

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
        self.fake.clock.advance(seconds=1)
        return self.fake.mail.add_message(
            self.source,
            folder_id=_ROLE_IDS[folder],
            internet_message_id=internet_message_id,
            subject=subject,
            body=body,
            unread=unread,
            attachments=attachments,
        )

    def copies(self, internet_message_id: str, folder_id: str | None = None) -> int:
        return self.fake.mail.copies(self.source, internet_message_id, folder_id)

    def is_unread(self, locator: str) -> bool:
        return self.fake.mail.is_unread(self.source, locator)


class FakeCalendarRig(_FakeFaults):
    def __init__(self, fake: FakeProvider | None = None, source: str = "calendar:test") -> None:
        super().__init__(fake or FakeProvider())
        self.source = source
        self.fake.calendar.add_source(source)
        self.port: CalendarPort = self.fake.calendar

    def new_uid(self) -> str:
        return f"contract-{next(self._serial)}@fake.example"

    def seed(self, fields: EventFields) -> str:
        return self.fake.calendar.add_event(self.source, fields)

    def count(self, uid: str) -> int:
        return self.fake.calendar.count(self.source, uid)

    def seed_invitation(self, organiser: str, fields: EventFields) -> str | None:
        return self.fake.calendar.add_event(
            self.source,
            fields.model_copy(update={"attendees": (organiser,)}),
            organiser=organiser,
            my_response=Response.NEEDS_ACTION,
        )


class FakeContactsRig:
    def __init__(self, fake: FakeProvider | None = None, source: str = "contacts:test") -> None:
        self.fake = fake or FakeProvider()
        self.source = source
        self.fake.contacts.add_source(source)
        self.port: ContactsPort = self.fake.contacts

    def seed(self, name: str, *emails: str) -> str:
        return self.fake.contacts.add_contact(self.source, name, *emails)
