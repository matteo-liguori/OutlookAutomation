"""Building the connector router from the profile.

Contract: docs/contracts/08-ports.md (P-1), 09-storage.md (S-16, S-17). The core reaches a
connector only through `SourceRouter`. Nothing here connects: every connector opens its
transport at the first call, so building is cheap and works with Thunderbird closed.
"""

from dataclasses import dataclass
from pathlib import Path

from chamberlain.app.context import SourceRouter
from chamberlain.connections.demo import demo_provider
from chamberlain.connections.model import (
    CalDavSource,
    Connections,
    DemoSource,
    GoogleCalendarSource,
    OwlCalendarSource,
    ThunderbirdContactsSource,
    ThunderbirdMailSource,
)
from chamberlain.connectors.caldav.calendar import CalDavCalendar
from chamberlain.connectors.caldav.config import CalDavBinding, CalDavConfig
from chamberlain.connectors.caldav.credentials import password_source
from chamberlain.connectors.calendar_support.secrets import SecretStore, default_store
from chamberlain.connectors.google_calendar.auth import AccessTokens
from chamberlain.connectors.google_calendar.calendar import GoogleCalendar
from chamberlain.connectors.google_calendar.config import GoogleBinding, GoogleCalendarConfig
from chamberlain.connectors.thunderbird.calendar import (
    OwlBinding,
    OwlCalendarConfig,
    ThunderbirdCalendar,
)
from chamberlain.connectors.thunderbird.calendar_bridge import ClientCalendarBridge
from chamberlain.connectors.thunderbird.client import BridgeClient, RuntimeConnector
from chamberlain.connectors.thunderbird.contacts import ContactsBinding, ThunderbirdContacts
from chamberlain.connectors.thunderbird.mail import ThunderbirdBinding, ThunderbirdMail
from chamberlain.memory.artifacts import Budgets
from chamberlain.ports.calendar import CalendarPort
from chamberlain.ports.clock import Clock
from chamberlain.ports.contacts import ContactsPort
from chamberlain.ports.mail import MailPort
from chamberlain.writes.models import WritePolicy

BRIDGE_DIR_NAME = "bridge"


@dataclass(frozen=True)
class Runtime:
    """Everything `build_app` takes that comes from configuration."""

    sources: SourceRouter
    policy: WritePolicy
    budgets: Budgets


def bridge_directory(connections: Connections, data_root: Path) -> Path:
    return Path(connections.bridge_dir) if connections.bridge_dir else data_root / BRIDGE_DIR_NAME


def _mail_binding(source: ThunderbirdMailSource) -> ThunderbirdBinding:
    return ThunderbirdBinding(
        account_id=source.account_id,
        mailbox=source.mailbox,
        inbox_folder_id=source.inbox_folder_id,
        sent_folder_id=source.sent_folder_id,
        draft_folder_id=source.draft_folder_id,
        trash_folder_id=source.trash_folder_id,
    )


def build_router(
    connections: Connections,
    data_root: Path,
    clock: Clock,
    *,
    secrets: SecretStore | None = None,
) -> SourceRouter:
    """The real connectors for every enabled source. `secrets` defaults to the OS store."""
    mail_sources = connections.of_kind(ThunderbirdMailSource)
    contacts_sources = connections.of_kind(ThunderbirdContactsSource)
    owl_sources = connections.of_kind(OwlCalendarSource)
    google_sources = connections.of_kind(GoogleCalendarSource)
    caldav_sources = connections.of_kind(CalDavSource)

    mail: dict[str, MailPort] = {}
    contacts: dict[str, ContactsPort] = {}
    calendar: dict[str, CalendarPort] = {}

    client = BridgeClient(RuntimeConnector(bridge_directory(connections, data_root)))
    if mail_sources or contacts_sources:
        if mail_sources:
            mail_port = ThunderbirdMail(
                client, {sid: _mail_binding(s) for sid, s in mail_sources.items()}, clock
            )
            mail = dict.fromkeys(mail_sources, mail_port)
        if contacts_sources:
            contacts_port = ThunderbirdContacts(
                client,
                {
                    sid: ContactsBinding(address_book_ids=s.address_book_ids)
                    for sid, s in contacts_sources.items()
                },
                clock,
            )
            contacts = dict.fromkeys(contacts_sources, contacts_port)

    if owl_sources:
        owl = ThunderbirdCalendar(
            OwlCalendarConfig(
                bindings={
                    sid: OwlBinding(calendar_id=s.calendar_id) for sid, s in owl_sources.items()
                }
            ),
            ClientCalendarBridge(client),
            clock,
        )
        calendar.update(dict.fromkeys(owl_sources, owl))

    store = secrets
    if (google_sources or caldav_sources) and store is None:
        store = default_store(data_root)
    if google_sources and store is not None:
        google = GoogleCalendar(
            GoogleCalendarConfig(
                bindings={
                    sid: GoogleBinding(account=s.account, calendar_id=s.calendar_id)
                    for sid, s in google_sources.items()
                }
            ),
            AccessTokens(store, clock),
            clock,
        )
        calendar.update(dict.fromkeys(google_sources, google))
    if caldav_sources and store is not None:
        dav = CalDavCalendar(
            CalDavConfig(
                bindings={
                    sid: CalDavBinding(
                        url=s.url,
                        username=s.username,
                        credential=s.credential,
                        address=s.address,
                    )
                    for sid, s in caldav_sources.items()
                }
            ),
            password_source(store),
            clock,
        )
        calendar.update(dict.fromkeys(caldav_sources, dav))

    demo_ids = sorted(connections.of_kind(DemoSource))
    if demo_ids:
        provider = demo_provider(demo_ids, clock)
        for sid in demo_ids:
            role = sid.partition(":")[0]
            if role == "mail":
                mail[sid] = provider.mail
            elif role == "calendar":
                calendar[sid] = provider.calendar
            else:
                contacts[sid] = provider.contacts

    return SourceRouter(mail=mail, calendar=calendar, contacts=contacts, demo=frozenset(demo_ids))


def policy_of(connections: Connections) -> WritePolicy:
    """The write policy the profile configures, in the profile's time zone."""
    return WritePolicy(
        send_known=connections.write_policy.send_known,
        other=connections.write_policy.other,
        sends_per_hour=connections.write_policy.sends_per_hour,
        zone=connections.timezone,
    )


def build_runtime(
    connections: Connections,
    data_root: Path,
    clock: Clock,
    *,
    secrets: SecretStore | None = None,
) -> Runtime:
    policy = policy_of(connections)
    budgets = Budgets(
        per_card_bytes=connections.budgets.per_card_bytes,
        total_bytes=connections.budgets.total_bytes,
    )
    return Runtime(
        sources=build_router(connections, data_root, clock, secrets=secrets),
        policy=policy,
        budgets=budgets,
    )
