"""A test rig for the writes track: a store, the fake provider and the service doubles.

Used by `tests/writes/`. It builds the real audit log and write control over an
in-memory store and wraps the fake mail port so a test can crash a process,
race a takeover or script reconcile answers, all without a real mailbox.
"""

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from chamberlain.app.context import Context, SourceRouter
from chamberlain.app.doubles import (
    MemoryArtifactStore,
    MemoryEvidenceStore,
    MemoryRefResolver,
)
from chamberlain.app.services import Services
from chamberlain.connectors.fake import FakeProvider
from chamberlain.domain.handles import HandleKind
from chamberlain.domain.grants import StandingScope
from chamberlain.domain.intents import CalendarCreate, IntentKind, MailSend
from chamberlain.domain.mail import FolderRole
from chamberlain.domain.operations import (
    DispatchResult,
    Prepared,
    ReconcileResult,
)
from chamberlain.ports.clock import ManualClock
from chamberlain.ports.calendar import CalendarPort
from chamberlain.ports.mail import MailPort
from chamberlain.store.db import MEMORY, connect
from chamberlain.store.migrate import migrate, stamp
from chamberlain.writes import repo
from chamberlain.writes.confirm import confirm_grant
from chamberlain.writes.engine import Engine, Proposal
from chamberlain.writes.models import GrantRow, OperationRow, WritePolicy
from chamberlain.writes.services import build

_NO_ROOT = Path("unused-data-root")
MAIL = "mail:test"
CALENDAR = "calendar:test"
SESSION = "ses_testsession01"


class SimulatedCrash(BaseException):
    """The process dies here. Not an `Exception`, so nothing in the engine catches it."""


class ScriptedPort:
    """A fake port (mail or calendar) with test hooks around its write calls."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.crash_in_prepare = False
        self.crash_after_dispatch = False
        self.crash_before_dispatch = False
        self.after_prepare: Callable[[], None] | None = None
        self.before_dispatch: Callable[[], None] | None = None
        self.during_dispatch: Callable[[], None] | None = None
        self.reconcile_script: list[ReconcileResult] = []
        self.reconcile_calls = 0
        self.last_refs: dict[str, str] = {}
        self.result_transform: Callable[[DispatchResult], DispatchResult] | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def prepare(self, intent: Any, refs: Mapping[str, str]) -> Prepared:
        if self.crash_in_prepare:
            raise SimulatedCrash
        self.last_refs = dict(refs)
        prepared: Prepared = self._inner.prepare(intent, refs)
        if self.after_prepare is not None:
            self.after_prepare()
        return prepared

    def dispatch(
        self, prepared: Prepared, key: str, *payloads: Mapping[str, bytes]
    ) -> DispatchResult:
        if self.crash_before_dispatch:
            raise SimulatedCrash
        if self.before_dispatch is not None:
            self.before_dispatch()
        result: DispatchResult = self._inner.dispatch(prepared, key, *payloads)
        if self.during_dispatch is not None:
            self.during_dispatch()
        if self.crash_after_dispatch:
            raise SimulatedCrash
        if self.result_transform is not None:
            return self.result_transform(result)
        return result

    def reconcile(self, prepared: Prepared, key: str) -> ReconcileResult:
        self.reconcile_calls += 1
        if self.reconcile_script:
            return self.reconcile_script.pop(0)
        answer: ReconcileResult = self._inner.reconcile(prepared, key)
        return answer


ScriptedMail = ScriptedPort


@dataclass
class WriteRig:
    db: sqlite3.Connection
    clock: ManualClock
    fake: FakeProvider
    mail: ScriptedPort
    calendar: ScriptedPort
    artifacts: MemoryArtifactStore
    evidence: MemoryEvidenceStore
    refs: MemoryRefResolver
    ctx: Context
    policy: WritePolicy
    pauses: list[float] = field(default_factory=lambda: [])

    def engine(self, *, worker: str | None = None, session: str | None = SESSION) -> Engine:
        context = replace(self.ctx, session=session)
        return Engine(context, self.policy, pause=self._pause, worker=worker or "worker-main")

    def _pause(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.clock.advance(seconds=seconds)

    def close(self) -> None:
        self.db.close()

    # --- fixtures of the world ------------------------------------------------------

    def seed_message(self, message_id: str = "<seed1@example.org>", **kwargs: Any) -> str:
        """A message in the inbox of the fake mailbox; returns its `msg_` handle."""
        locator = self.fake.mail.add_message(MAIL, internet_message_id=message_id, **kwargs)
        return self.refs.remember(HandleKind.MESSAGE, MAIL, locator, message_id)

    def know(self, *addresses: str) -> None:
        """Make addresses known recipients, as a human-confirmed grant would (G-15)."""
        stored = stamp(self.clock)
        handle = "grant_knownseed00"
        self.db.execute(
            "INSERT OR IGNORE INTO grants (handle, kind, state, source_id, title, requirement, "
            "content_sha256, proposed_at) VALUES (?, 'one_off', 'spent', ?, 'seed', "
            "'human_confirmation', ?, ?)",
            (handle, MAIL, "0" * 64, stored),
        )
        for address in addresses:
            repo.add_known_recipient(self.db, MAIL, address, handle, stored)

    # --- intents --------------------------------------------------------------------

    @staticmethod
    def send(
        to: tuple[str, ...] = ("friend@example.org",),
        subject: str = "Hello",
        body: str = "A short note.",
    ) -> MailSend:
        return MailSend(source=MAIL, to=to, subject=subject, body=body)

    @staticmethod
    def event(title: str = "Lunch", attendees: tuple[str, ...] = ()) -> CalendarCreate:
        return CalendarCreate.model_validate(
            {
                "source": CALENDAR,
                "event": {
                    "title": title,
                    "start": datetime(2026, 2, 1, 12, tzinfo=UTC),
                    "end": datetime(2026, 2, 1, 13, tzinfo=UTC),
                    "attendees": attendees,
                },
            }
        )

    # --- approvals ------------------------------------------------------------------

    def human_approves(self, proposal: Proposal, *, engine: Engine | None = None) -> None:
        """The person confirms in the viewer, seeing exactly the stored hash."""
        assert proposal.grant is not None
        confirm_grant(
            self.engine().ctx if engine is None else engine.ctx,
            proposal.grant.handle,
            {"content_sha256": proposal.content_sha256},
            self.policy,
        )

    def agent_approves(self, proposal: Proposal, words: str = "Yes, send it.") -> Proposal:
        """The agent records the user's words and attaches them (G-13)."""
        assert proposal.operation is not None
        eng = self.engine()
        evidence = self.evidence.add(text=words, owner=proposal.operation.handle, session=SESSION)
        return eng.attach_approval(proposal.operation.handle, evidence)

    def standing_grant(
        self,
        recipients: tuple[str, ...] = ("friend@example.org",),
        *,
        max_per_day: int = 5,
        allow_repeat: bool = False,
        card: str | None = None,
    ) -> GrantRow:
        """A standing send grant, proposed by the agent and confirmed by the human."""
        scope = StandingScope(
            source=MAIL,
            intent_kinds=frozenset({IntentKind.MAIL_SEND}),
            title="Send to a friend",
            recipients=frozenset(recipients),
            max_per_day=max_per_day,
            allow_repeat=allow_repeat,
            card=card,
        )
        proposal = self.engine().propose_standing(scope)
        assert proposal.grant is not None
        return confirm_grant(
            self.engine().ctx,
            proposal.grant.handle,
            {"content_sha256": proposal.content_sha256},
            self.policy,
        )

    def ready_send(self, **kwargs: Any) -> OperationRow:
        """Propose a send to a known recipient, approve it, and return the ready operation."""
        intent = self.send(**kwargs)
        self.know(*intent.to)
        eng = self.engine()
        proposal = eng.propose_intent(intent)
        approved = self.agent_approves(proposal)
        assert approved.ready
        assert approved.operation is not None
        return approved.operation

    # --- observations ---------------------------------------------------------------

    def sent_copies(self) -> int:
        """How many messages the fake holds in its Sent folder."""
        sent = self.fake.world.folder_by_role(MAIL, FolderRole.SENT).id
        return len([m for m in self.fake.world.mail[MAIL] if m.folder_id == sent])

    def state(self, handle: str) -> str:
        op = repo.get_operation(self.db, handle)
        assert op is not None
        return op.state.value


def make_rig(*, send_absence_provable: bool = False, policy: WritePolicy | None = None) -> WriteRig:
    clock = ManualClock()
    db = connect(MEMORY)
    migrate(db, clock, backup_dir=None)
    fake = FakeProvider(clock, send_absence_provable=send_absence_provable)
    fake.mail.add_source(MAIL)
    fake.calendar.add_source(CALENDAR)
    mail = ScriptedPort(fake.mail)
    calendar = ScriptedPort(fake.calendar)
    artifacts = MemoryArtifactStore()
    evidence = MemoryEvidenceStore()
    refs = MemoryRefResolver()
    audit, control = build(db, clock, policy=policy, artifacts=artifacts)
    ctx = Context(
        db=db,
        clock=clock,
        data_root=_NO_ROOT,
        sources=SourceRouter(
            mail={MAIL: cast(MailPort, mail)}, calendar={CALENDAR: cast(CalendarPort, calendar)}
        ),
        services=Services(
            audit=audit, artifacts=artifacts, evidence=evidence, refs=refs, writes=control
        ),
        session=SESSION,
    )
    return WriteRig(
        db, clock, fake, mail, calendar, artifacts, evidence, refs, ctx, policy or WritePolicy()
    )
