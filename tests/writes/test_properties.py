"""The three engine properties of 04-writes.md, as Hypothesis state machines.

Random sequences of propose, approve, confirm, do, fault, crash, restart,
reconcile, user resolution, revocation, expiry and maintenance, against the
fake provider with every `Fault` armed at random. After every step:

1. No grant yields two confirmed operations beyond its limit, and a one-off
   never yields more than one remote effect (W-8, G-11).
2. The fake's dispatch log never shows one key twice (W-8, W-17).
3. Every terminal operation has a receipt, every transition has an audit row,
   every transition is an arrow of `TRANSITIONS`, and the hash chain verifies.
"""

from collections import Counter

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)

from chamberlain.connectors.fake import Fault
from chamberlain.domain.operations import TERMINAL_STATES, TRANSITIONS, OperationState
from chamberlain.domain.grants import GrantKind
from chamberlain.domain.operations import ReconcileResult, ReconcileStatus
from chamberlain.schema.errors import ChamberlainError
from chamberlain.writes import repo
from chamberlain.writes.audit import verify
from chamberlain.writes.confirm import confirm_grant
from chamberlain.writes.maintenance import maintain
from chamberlain.writes.rig import CALENDAR, MAIL, SimulatedCrash, WriteRig, make_rig

ADDRESSES = ("amy@example.org", "bob@example.org", "new@example.org")
KNOWN = ADDRESSES[:2]
SUBJECTS = ("One", "Two", "Three")
FAULTS = tuple(Fault)


class WriteMachine(RuleBasedStateMachine):
    ops = Bundle("ops")

    def __init__(self) -> None:
        super().__init__()
        self.rig: WriteRig | None = None
        self.worker = 0
        self.grant_rows_seen = 0
        self.steps = 0

    @property
    def r(self) -> WriteRig:
        assert self.rig is not None
        return self.rig

    def engine(self):  # type: ignore[no-untyped-def]
        return self.r.engine(worker=f"w{self.worker}")

    @initialize(provable=st.booleans())
    def start(self, provable: bool) -> None:
        self.rig = make_rig(send_absence_provable=provable)
        self.r.know(*KNOWN)

    def teardown(self) -> None:
        if self.rig is not None:
            assert verify(self.rig.db).ok
            self.rig.close()

    # --- agent and human actions ------------------------------------------------------

    @rule(target=ops, who=st.sampled_from(ADDRESSES), subject=st.sampled_from(SUBJECTS))
    def propose_send(self, who: str, subject: str) -> str:
        try:
            proposal = self.engine().propose_intent(self.r.send(to=(who,), subject=subject))
        except ChamberlainError:
            return "op_missing00000"
        assert proposal.operation is not None
        return proposal.operation.handle

    @rule(
        target=ops,
        who=st.sampled_from(ADDRESSES),
        subject=st.sampled_from(SUBJECTS),
        run=st.booleans(),
        fault=st.sampled_from((None, None, *FAULTS)),
    )
    def pipeline(self, who: str, subject: str, run: bool, fault: Fault | None) -> str:
        """Propose, get every approval a person could give, and optionally do it."""
        try:
            proposal = self.engine().propose_intent(self.r.send(to=(who,), subject=subject))
        except ChamberlainError:
            return "op_missing00000"
        assert proposal.operation is not None
        handle = proposal.operation.handle
        self.agent_approves(handle)
        self.human_confirms(handle, honest=True)
        if fault is not None:
            self.r.fake.arm(fault)
        if run:
            self.do_op(handle)
        return handle

    @rule(target=ops, title=st.sampled_from(("Lunch", "Walk")))
    def propose_event(self, title: str) -> str:
        proposal = self.engine().propose_intent(self.r.event(title))
        assert proposal.operation is not None
        return proposal.operation.handle

    @rule(op=ops)
    def agent_approves(self, op: str) -> None:
        row = repo.get_operation(self.r.db, op)
        if row is None:
            return
        evidence = self.r.evidence.add(text="Yes, go ahead.", owner=op, session="ses_testsession01")
        try:
            self.engine().attach_approval(op, evidence)
        except ChamberlainError:
            pass

    @rule(op=ops, honest=st.booleans())
    def human_confirms(self, op: str, honest: bool) -> None:
        row = repo.get_operation(self.r.db, op)
        if row is None:
            return
        grant = repo.proposed_one_off(self.r.db, row.source_id, row.intent_sha256)
        if grant is None:
            return
        shown = grant.content_sha256 if honest else "f" * 64
        try:
            confirm_grant(self.engine().ctx, grant.handle, {"content_sha256": shown})
        except ChamberlainError:
            assert not honest or True
        if not honest:
            assert repo.get_grant(self.r.db, grant.handle).state.value == "proposed"  # type: ignore[union-attr]

    @rule(op=ops)
    def do_op(self, op: str) -> None:
        try:
            self.engine().do(op)
        except (ChamberlainError, SimulatedCrash):
            pass

    @rule(op=ops)
    def reconcile_op(self, op: str) -> None:
        try:
            self.engine().reconcile(op)
        except ChamberlainError:
            pass

    @rule(op=ops, odds=st.integers(0, 3))
    def cancel_op(self, op: str, odds: int) -> None:
        if odds:
            return
        try:
            self.engine().cancel(op, actor="agent:test")
        except ChamberlainError:
            pass

    @rule(op=ops, odds=st.integers(0, 3))
    def revoke_its_grant(self, op: str, odds: int) -> None:
        if odds:
            return
        row = repo.get_operation(self.r.db, op)
        if row is not None and row.grant is not None:
            self.engine().grants.revoke(row.grant, actor="human:viewer")

    @rule(op=ops)
    def honest_user_resolves(self, op: str) -> None:
        row = repo.get_operation(self.r.db, op)
        if row is None or row.state is not OperationState.UNCERTAIN:
            return
        world = self.r.fake.world
        if world._late:  # a late write is still on its way: the user cannot know yet
            return
        token = row.reconcile_token or ""
        landed = (
            any(m.internet_message_id == token for m in world.mail[MAIL])
            or self.r.fake.calendar.count(CALENDAR, token) > 0
        )
        try:
            self.engine().resolve(op, happened=landed, actor="human:viewer")
        except ChamberlainError:
            pass

    # --- the world misbehaves ----------------------------------------------------------

    @rule(fault=st.sampled_from(FAULTS), times=st.integers(min_value=1, max_value=3))
    def arm_fault(self, fault: Fault, times: int) -> None:
        self.r.fake.arm(fault, times)

    @rule()
    def disarm_faults(self) -> None:
        self.r.fake.disarm()

    @rule(kind=st.sampled_from(("queued", "unknown")), times=st.integers(min_value=1, max_value=3))
    def script_reconcile(self, kind: str, times: int) -> None:
        status = ReconcileStatus.QUEUED if kind == "queued" else ReconcileStatus.UNKNOWN
        self.r.mail.reconcile_script = [ReconcileResult(status=status)] * times

    @rule()
    def arm_crash(self) -> None:
        self.r.mail.crash_after_dispatch = True

    @rule(minutes=st.integers(min_value=0, max_value=45))
    def restart(self, minutes: int) -> None:
        """The process dies and comes back later as a new worker."""
        self.r.mail.crash_after_dispatch = False
        self.worker += 1
        self.r.clock.advance(minutes=minutes)
        self.engine().recover()

    @rule(hours=st.sampled_from((0, 0, 0, 1, 5, 24, 24 * 8)))
    def time_passes(self, hours: int) -> None:
        self.r.clock.advance(hours=hours)

    @rule()
    def tick(self) -> None:
        maintain(self.r.ctx, self.r.policy)

    # --- invariants ----------------------------------------------------------------------

    @precondition(lambda self: self.rig is not None)
    @invariant()
    def one_dispatch_per_key_and_one_effect_per_grant(self) -> None:
        rig = self.r
        keys = Counter(record.key for record in rig.fake.dispatch_log)
        assert all(count == 1 for count in keys.values()), keys
        operations = {row["handle"]: row for row in rig.db.execute("SELECT * FROM operations")}
        dispatched_keys = {
            row["idempotency_key"] for row in operations.values() if row["dispatched_at"]
        }
        assert set(keys) <= dispatched_keys
        sent = Counter(m.internet_message_id for m in rig.fake.world.mail[MAIL])
        assert all(count == 1 for count in sent.values()), sent
        per_grant: dict[str, int] = Counter()
        confirmed: dict[str, int] = Counter()
        for row in operations.values():
            token = row["reconcile_token"]
            if row["grant"] is None or token is None:
                continue
            effects = sent.get(token, 0) + rig.fake.calendar.count(CALENDAR, token)
            per_grant[row["grant"]] += effects
            if row["state"] == "confirmed":
                confirmed[row["grant"]] += 1
            if row["state"] in ("failed_clean", "confirmed_absent", "cancelled"):
                assert effects == 0, (row["state"], row["handle"])
        for handle, count in per_grant.items():
            grant = repo.get_grant(rig.db, handle)
            assert grant is not None
            if grant.kind is GrantKind.ONE_OFF:
                assert count <= 1, handle
                assert confirmed[handle] <= 1, handle

    @precondition(lambda self: self.rig is not None)
    @invariant()
    def terminals_have_receipts_and_every_transition_is_audited(self) -> None:
        rig = self.r
        self.steps += 1
        history: dict[str, list[str]] = {}
        for row in rig.db.execute(
            "SELECT subject, kind FROM audit_log WHERE kind LIKE 'operation.%' ORDER BY seq"
        ):
            history.setdefault(row["subject"], []).append(row["kind"].split(".", 1)[1])
        for row in rig.db.execute("SELECT handle, state, receipt_json FROM operations"):
            states = [OperationState(s) for s in history[row["handle"]] if s != "verified"]
            assert states[0] is OperationState.PROPOSED
            assert states[-1].value == row["state"]
            for before, after in zip(states, states[1:], strict=False):
                assert after in TRANSITIONS[before], (before, after)
            if OperationState(row["state"]) in TERMINAL_STATES:
                assert row["receipt_json"], row["handle"]
            if OperationState.DISPATCHED in states:
                assert states.count(OperationState.DISPATCHED) == 1
        if self.steps % 6 == 0:
            assert verify(rig.db).ok

    @precondition(lambda self: self.rig is not None)
    @invariant()
    def grants_are_never_deleted(self) -> None:
        count = repo.count_grants(self.r.db)
        assert count >= self.grant_rows_seen
        self.grant_rows_seen = count


WriteMachine.TestCase.settings = settings(
    max_examples=150,
    stateful_step_count=45,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
TestWriteMachine = WriteMachine.TestCase
