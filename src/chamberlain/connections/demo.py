"""The demo provider: a small synthetic mailbox, calendar and address book.

Contract: docs/contracts/09-storage.md (S-16). Built on the fake provider, so every read
and write stays in memory and in this process. Addresses use `example.invalid` and every
subject, title and name says it is a demo, so nothing here can pass for real mail.
"""

from collections.abc import Iterable
from datetime import timedelta

from chamberlain.connections.model import Connections, DemoSource
from chamberlain.connectors.fake import FakeProvider
from chamberlain.domain.calendar import EventFields
from chamberlain.ports.clock import Clock, ManualClock

DEMO_MARK = "[DEMO]"
DEMO_DOMAIN = "example.invalid"
DEMO_OWNER = f"you@{DEMO_DOMAIN}"

_MESSAGES: tuple[tuple[str, str, str, bool], ...] = (
    ("Welcome to the Chamberlain demo", "demo-team", "This mailbox is synthetic.", True),
    (
        "Lunch on Thursday?",
        "sam",
        "Are you free at noon? Reply to this demo message to try a draft.",
        True,
    ),
    (
        "Invoice 1042 for the demo project",
        "billing",
        "Invoice 1042 is attached. Total: 120.00 demo units.",
        True,
    ),
    ("Weekly notes", "sam", "Nothing urgent this week. This is made-up text.", False),
    ("Re: Schedule review", "alex", "Can we move the review to Friday afternoon?", True),
)
_EVENTS: tuple[tuple[str, int, int], ...] = (
    ("Demo stand-up", 1, 30),
    ("Demo schedule review", 28, 60),
    ("Demo lunch with Sam", 54, 60),
)
_CONTACTS: tuple[tuple[str, str], ...] = (
    ("Sam Example (demo)", "sam"),
    ("Alex Example (demo)", "alex"),
    ("Billing Example (demo)", "billing"),
)


def _role(source_id: str) -> str:
    return source_id.partition(":")[0]


def _seed_mail(fake: FakeProvider, source: str) -> None:
    fake.mail.add_source(source)
    for number, (subject, who, body, unread) in enumerate(_MESSAGES, start=1):
        fake.mail.add_message(
            source,
            internet_message_id=f"<demo-{number}@{DEMO_DOMAIN}>",
            subject=f"{DEMO_MARK} {subject}",
            sender=f"{who}@{DEMO_DOMAIN}",
            to=(DEMO_OWNER,),
            body=f"{DEMO_MARK} Synthetic demo data, not real mail.\n\n{body}",
            unread=unread,
            date=fake.clock.now() - timedelta(hours=number * 5),
        )


def _seed_calendar(fake: FakeProvider, source: str) -> None:
    fake.calendar.add_source(source)
    start = fake.clock.now().replace(minute=0, second=0) + timedelta(hours=1)
    for title, offset, length in _EVENTS:
        begin = start + timedelta(minutes=offset)
        fake.calendar.add_event(
            source,
            EventFields(
                title=f"{DEMO_MARK} {title}",
                start=begin,
                end=begin + timedelta(minutes=length),
                description="Synthetic demo event.",
                attendees=(DEMO_OWNER,),
            ),
        )


def _seed_contacts(fake: FakeProvider, source: str) -> None:
    fake.contacts.add_source(source)
    for name, who in _CONTACTS:
        fake.contacts.add_contact(source, f"{DEMO_MARK} {name}", f"{who}@{DEMO_DOMAIN}")


def demo_provider(source_ids: Iterable[str], clock: Clock) -> FakeProvider:
    """One fake world seeded for each demo source, starting at `clock`'s present."""
    provider = FakeProvider(ManualClock(clock.now()))
    seeds = {"mail": _seed_mail, "calendar": _seed_calendar, "contacts": _seed_contacts}
    for source_id in source_ids:
        seeds[_role(source_id)](provider, source_id)
    return provider


def demo_sources(connections: Connections) -> dict[str, DemoSource]:
    return connections.of_kind(DemoSource)


def demo_profile() -> Connections:
    """A profile with only demo sources: mail, calendar and contacts."""
    return Connections(
        sources={
            "mail:demo": DemoSource(label="Demo mailbox"),
            "calendar:demo": DemoSource(label="Demo calendar"),
            "contacts:demo": DemoSource(label="Demo contacts"),
        }
    )
