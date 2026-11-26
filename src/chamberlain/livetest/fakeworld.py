"""The fake world for the live-test self-test: owned mailboxes that really deliver.

Contract: docs/live-test.md (Self-test). The shared fake provider never delivers mail: a
send only lands in the sender's Sent folder. Here a wrapper copies an applied send into the
Inbox of every fake mailbox that owns a recipient address, with the reply headers a real
server would keep. Nothing in this module touches a real account.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from chamberlain.app.context import SourceRouter
from chamberlain.app.core import App
from chamberlain.app.wiring import build_app
from chamberlain.connections.model import Connections, GoogleCalendarSource, ThunderbirdMailSource
from chamberlain.connections.repo import sync_sources
from chamberlain.connectors.fake import FakeProvider, Fault
from chamberlain.domain.intents import IntentKind, MailIntent
from chamberlain.domain.operations import DispatchResult, Prepared
from chamberlain.livetest.config import LiveCalendar, LiveMailbox, LivePair, LiveTestConfig
from chamberlain.livetest.env import Environment, PortProbe
from chamberlain.ports.clock import ManualClock
from chamberlain.ports.mail import MailPort
from chamberlain.store.db import transaction
from chamberlain.store.migrate import stamp

FAKE_MAIL_A = "mail:laba"
FAKE_MAIL_B = "mail:labb"
FAKE_CAL_A = "calendar:laba"
FAKE_CAL_B = "calendar:labb"
ADDRESS_A = "lab.a@lab.example.test"
ADDRESS_B = "lab.b@lab.example.test"


def _items(value: object) -> list[Any]:
    return list[Any](cast("list[Any]", value)) if isinstance(value, list) else []


class DeliveringMail:
    """A mail port that also delivers applied sends to the mailboxes that own the recipients."""

    def __init__(self, provider: FakeProvider, owners: Mapping[str, str]) -> None:
        self._provider = provider
        self._inner = provider.mail
        self._owners = dict(owners)
        """Owned address -> fake mail source."""

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def cut_off(self, address: str) -> None:
        """Stop delivering to an address (a test of what a missing arrival looks like)."""
        self._owners.pop(address, None)

    def prepare(self, intent: MailIntent, refs: Mapping[str, str]) -> Prepared:
        return self._inner.prepare(intent, refs)

    def dispatch(
        self, prepared: Prepared, key: str, payloads: Mapping[str, bytes]
    ) -> DispatchResult:
        before = len(self._provider.dispatch_log)
        result = self._inner.dispatch(prepared, key, payloads)
        log = self._provider.dispatch_log
        landed = len(log) > before and log[-1].applied
        if landed and prepared.intent_kind is IntentKind.MAIL_SEND:
            self._deliver(prepared, payloads)
        return result

    def _deliver(self, prepared: Prepared, payloads: Mapping[str, bytes]) -> None:
        data = prepared.data
        sender = next((a for a, s in self._owners.items() if s == prepared.source), "")
        recipients = [str(a) for k in ("to", "cc", "bcc") for a in _items(data.get(k))]
        in_reply_to = ""
        locator = data.get("reply_locator")
        if isinstance(locator, str):
            in_reply_to = self._inner.resolve(prepared.source, locator).internet_message_id
        attachments: list[tuple[str, bytes]] = []
        for item in _items(data.get("attachments")):
            if isinstance(item, dict):
                entry = cast("dict[str, Any]", item)
                digest = str(entry.get("sha256"))
                if digest in payloads:
                    attachments.append((str(entry["filename"]), payloads[digest]))
        visible = tuple(str(a) for a in _items(data.get("to")))
        for address in dict.fromkeys(recipients):
            owner = self._owners.get(address)
            if owner is None:
                continue
            self._inner.add_message(
                owner,
                internet_message_id=prepared.reconcile_token or "",
                subject=str(data["subject"]),
                sender=sender,
                to=visible,
                body=str(data["body"]),
                attachments=tuple(attachments),
                in_reply_to=in_reply_to,
                references=(in_reply_to,) if in_reply_to else (),
            )


class FakeFaultInjector:
    """Arms the fake's lost-response switch (the native host's file hook, in the fake world)."""

    def __init__(self, provider: FakeProvider) -> None:
        self._provider = provider

    def why_unavailable(self) -> str | None:
        return None

    def arm_lost_response(self, run_id: str) -> None:
        self._provider.arm(Fault.LOSE_RESPONSE_AFTER_APPLY)

    def fired(self) -> bool:
        return not self._provider.world.armed(Fault.LOSE_RESPONSE_AFTER_APPLY)

    def disarm(self) -> None:
        self._provider.disarm(Fault.LOSE_RESPONSE_AFTER_APPLY)


@dataclass
class FakeLab:
    """An app over two owned fake mailboxes and two fake calendars, plus its fake world."""

    app: App
    provider: FakeProvider
    clock: ManualClock
    config: LiveTestConfig
    env: Environment
    root: Path
    delivery: DeliveringMail

    def close(self) -> None:
        self.app.close()


def _bindings(config: LiveTestConfig) -> Connections:
    """The profile a real installation would have: each source bound to its own address.

    The `sources` table is where the harness learns which addresses are the owner's, so the
    self-test fills it the way `open_app` does.
    """
    mail: dict[str, Any] = {
        source: ThunderbirdMailSource(
            account_id=source.partition(":")[2],
            mailbox=box.address,
            inbox_folder_id="inbox",
            sent_folder_id="sent",
            draft_folder_id="drafts",
            trash_folder_id="trash",
        )
        for source, box in config.mail.items()
    }
    calendars: dict[str, Any] = {
        source: GoogleCalendarSource(account=address, calendar_id="primary")
        for source, address in ((FAKE_CAL_A, ADDRESS_A), (FAKE_CAL_B, ADDRESS_B))
    }
    return Connections(sources={**mail, **calendars}, live_test=config)


def build_fake_lab(root: Path, *, approve_calendar_edits: bool = True) -> FakeLab:
    """The whole self-test world under `root` (a temporary data root)."""
    clock = ManualClock()
    provider = FakeProvider(clock)
    for source in (FAKE_MAIL_A, FAKE_MAIL_B):
        provider.mail.add_source(source)
    provider.calendar.add_source(FAKE_CAL_A, address=ADDRESS_A)
    provider.calendar.add_source(FAKE_CAL_B, address=ADDRESS_B)
    owners = {ADDRESS_A: FAKE_MAIL_A, ADDRESS_B: FAKE_MAIL_B}
    delivery = DeliveringMail(provider, owners)
    mail = cast(MailPort, delivery)
    router = SourceRouter(
        mail={FAKE_MAIL_A: mail, FAKE_MAIL_B: mail},
        calendar={FAKE_CAL_A: provider.calendar, FAKE_CAL_B: provider.calendar},
    )
    app = build_app(root, clock=clock, sources=router)
    config = LiveTestConfig(
        enabled=True,
        mail={
            FAKE_MAIL_A: LiveMailbox(address=ADDRESS_A),
            FAKE_MAIL_B: LiveMailbox(address=ADDRESS_B),
        },
        pairs=(
            LivePair(source=FAKE_MAIL_A, to=FAKE_MAIL_B),
            LivePair(source=FAKE_MAIL_B, to=FAKE_MAIL_A),
        ),
        calendars={
            FAKE_CAL_A: LiveCalendar(invitee_mail=FAKE_MAIL_B, invitee_calendar=FAKE_CAL_B),
            FAKE_CAL_B: LiveCalendar(),
        },
        approve_calendar_edits=approve_calendar_edits,
    )
    sources = _bindings(config)
    with transaction(app.context.db) as db:
        sync_sources(db, sources, stamp(clock))
    env = Environment(
        mode="fake",
        clock=clock,
        sleep=lambda seconds: clock.advance(seconds=seconds),
        faults=FakeFaultInjector(provider),
        probe=PortProbe(app.context),
        dispatches=lambda: len(provider.dispatch_log),
    )
    return FakeLab(
        app=app,
        provider=provider,
        clock=clock,
        config=config,
        env=env,
        root=root,
        delivery=delivery,
    )
