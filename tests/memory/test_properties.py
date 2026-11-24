"""The four sweeper properties, as a Hypothesis state machine.

Contract: docs/contracts/06-memory.md ("Property tests that define done"). Random
sequences of create, own, keep, finish, close, expire, sweep and crash-mid-sweep.
After every clean sweep:

1. nothing owned by a live owner is deleted;
2. nothing survives past its limit without a live owner;
3. no file exists in `artifacts/` without a live row, and no live row lacks its file;
4. sweeping twice changes nothing the second time.
"""

import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule
from memory_helpers import (
    add_operation,
    add_session,
    blobs,
    make_context,
    settle_operation,
    table_dump,
)

from chamberlain.domain.artifacts import ArtifactKind
from chamberlain.domain.canonical import sha256_bytes
from chamberlain.memory import repo
from chamberlain.memory.artifacts import ArtifactService, blob_path
from chamberlain.memory.cards import CardService
from chamberlain.memory.sweeper import DATA_ROOT_LAYOUT, SweepCrash, SweepHooks, rule_names, sweep
from chamberlain.memory.timeutil import parse
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.errors import ChamberlainError

PAYLOADS = (b"alpha", b"beta", b"gamma gamma", b"")
LIFETIMES = (1, 6, 30, 24 * 10, 24 * 60)


class SweeperMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self._tmp = tempfile.TemporaryDirectory()
        self.clock = ManualClock()
        self.ctx = make_context(Path(self._tmp.name) / "data", self.clock)
        self.store = ArtifactService(self.ctx.db, self.clock, self.ctx.data_root)
        self.cards = CardService(self.ctx.db, self.clock)
        self.open_actions: dict[str, str] = {}  # action -> card
        self.ended_actions: set[str] = set()
        self.ops: dict[str, datetime | None] = {}  # op -> settled_at (None while pending)
        self.sessions: dict[str, datetime] = {}  # session -> last seen
        self.holds: dict[str, set[str]] = {}  # artifact -> owners
        self.payload: dict[str, bytes] = {}
        self.dead: set[str] = set()
        self.counter = 0

    def teardown(self) -> None:
        self.ctx.db.close()
        self._tmp.cleanup()

    # --- the model's own idea of liveness (M-4), independent of the SQL ------------------

    def live(self, owner: str) -> bool:
        now = self.clock.now()
        if owner in self.open_actions:
            return True
        if owner in self.ops:
            settled = self.ops[owner]
            return settled is None or settled + timedelta(hours=24) > now
        if owner in self.sessions:
            return self.sessions[owner] + timedelta(hours=4) > now
        return False

    def live_owners(self) -> list[str]:
        return sorted(o for o in (*self.open_actions, *self.ops, *self.sessions) if self.live(o))

    # --- rules --------------------------------------------------------------------------------

    @rule()
    def create_action(self) -> None:
        card = self.cards.create(title=f"Card {self.counter}")
        self.counter += 1
        view = self.cards.add_action(
            card.handle, None, title="work", due_at=None, snooze_until=None
        )
        self.open_actions[view.actions[0].handle] = card.handle

    @rule()
    def create_operation(self) -> None:
        self.counter += 1
        handle = f"op_property{self.counter:08d}"
        add_operation(self.ctx.db, self.clock, handle)
        self.ops[handle] = None

    @rule()
    def create_session(self) -> None:
        self.counter += 1
        handle = f"ses_property{self.counter:08d}"
        add_session(self.ctx.db, self.clock, handle)
        self.sessions[handle] = self.clock.now()

    @rule(
        pick=st.integers(0, 50), payload=st.sampled_from(PAYLOADS), life=st.sampled_from(LIFETIMES)
    )
    def keep(self, pick: int, payload: bytes, life: int) -> None:
        owners = self.live_owners()
        if not owners:
            return
        owner = owners[pick % len(owners)]
        kept = self.store.keep_bytes(
            data=payload,
            kind=ArtifactKind.TEXT,
            owner=owner,
            reason="property",
            expires_in_hours=life,
        )
        self.holds[kept.handle] = {owner}
        self.payload[kept.handle] = payload

    @rule(pick=st.integers(0, 50), owner_pick=st.integers(0, 50))
    def add_hold(self, pick: int, owner_pick: int) -> None:
        candidates = sorted(h for h in self.holds if h not in self.dead)
        owners = self.live_owners()
        if not candidates or not owners:
            return
        handle = candidates[pick % len(candidates)]
        try:
            self.store.add_owner(handle, owners[owner_pick % len(owners)])
        except ChamberlainError:
            return
        self.holds[handle].add(owners[owner_pick % len(owners)])

    @rule(pick=st.integers(0, 50))
    def finish_action(self, pick: int) -> None:
        if not self.open_actions:
            return
        action = sorted(self.open_actions)[pick % len(self.open_actions)]
        self.cards.finish_action(action, None, "Done.")
        del self.open_actions[action]
        self.ended_actions.add(action)

    @rule(pick=st.integers(0, 50))
    def close_card(self, pick: int) -> None:
        if not self.open_actions:
            return
        action = sorted(self.open_actions)[pick % len(self.open_actions)]
        card = self.open_actions[action]
        self.cards.close(card, None, "Closed.")
        for other in [a for a, c in self.open_actions.items() if c == card]:
            del self.open_actions[other]
            self.ended_actions.add(other)

    @rule(pick=st.integers(0, 50), terminal=st.booleans())
    def settle_operation(self, pick: int, terminal: bool) -> None:
        pending = sorted(o for o, s in self.ops.items() if s is None)
        if not pending or not terminal:
            return
        handle = pending[pick % len(pending)]
        settle_operation(self.ctx.db, self.clock, handle, "confirmed")
        self.ops[handle] = self.clock.now()

    @rule(hours=st.sampled_from((1, 5, 30, 24 * 10, 24 * 40)))
    def advance(self, hours: int) -> None:
        self.clock.advance(hours=hours)

    @rule(hours=st.sampled_from((2, 7, 31, 24 * 11, 24 * 61)))
    def age_and_sweep(self, hours: int) -> None:
        self.clock.advance(hours=hours)
        self.clean_sweep()

    @rule(name=st.sampled_from(("stray.bin", "notes.txt", "junk")))
    def add_stray_file(self, name: str) -> None:
        root = self.ctx.data_root
        root.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(b"stray")

    @rule(crash_after=st.sampled_from(rule_names()), unlink_crash=st.booleans())
    def crash_mid_sweep(self, crash_after: str, unlink_crash: bool) -> None:
        def die(name: str) -> None:
            if name == crash_after:
                raise SweepCrash(name)

        def unlink_then_die(path: Path) -> None:
            path.unlink()
            raise SweepCrash("after unlink")

        hooks = SweepHooks(unlink=unlink_then_die) if unlink_crash else SweepHooks(after_rule=die)
        try:
            sweep(self.ctx, self.clock.now(), hooks=hooks)
        except SweepCrash:
            return

    @rule()
    def clean_sweep(self) -> None:
        sweep(self.ctx, self.clock.now())
        self.check_properties()
        before = (table_dump(self.ctx.db), blobs(self.ctx.data_root))
        second = sweep(self.ctx, self.clock.now())
        assert not second.changed()  # property 4
        assert (table_dump(self.ctx.db), blobs(self.ctx.data_root)) == before

    # --- the properties ------------------------------------------------------------------------

    def expected_live(self, handle: str) -> bool:
        now = self.clock.now()
        row = repo.get_artifact(self.ctx.db, handle)
        if row is None or handle in self.dead:
            return False
        holders = [o for o in self.holds[handle] if self.live(o)]
        if not holders:
            return False
        expires = parse(str(row["expires_at"]))
        return now < expires or any(o in self.ops for o in holders)

    def check_properties(self) -> None:
        for handle in sorted(self.holds):
            row = repo.get_artifact(self.ctx.db, handle)
            if self.expected_live(handle):
                # 1. nothing owned by a live owner is deleted
                assert row is not None
                assert row["state"] == "live", handle
                assert self.store.read_bytes(handle) == self.payload[handle]
            else:
                # 2. nothing survives past its limit without a live owner
                self.dead.add(handle)
                assert row is None or row["state"] != "live", handle
        # 3. files and live rows match one to one
        live_rows = repo.live_artifacts(self.ctx.db)
        wanted = {str(r["sha256"]) for r in live_rows}
        assert blobs(self.ctx.data_root) == wanted
        for row in live_rows:
            assert blob_path(self.ctx.data_root, str(row["sha256"])).exists()
            assert sha256_bytes(self.payload[str(row["handle"])]) == str(row["sha256"])
        # nothing but the fixed layout is left in the data root
        names = (
            {p.name for p in self.ctx.data_root.iterdir()} if self.ctx.data_root.exists() else set()
        )
        assert names <= DATA_ROOT_LAYOUT

    @invariant()
    @precondition(lambda self: bool(self.holds))
    def database_agrees_with_itself(self) -> None:
        orphans = self.ctx.db.execute(
            "SELECT count(*) AS n FROM artifact_owners WHERE artifact NOT IN "
            "(SELECT handle FROM artifacts)"
        ).fetchone()["n"]
        assert orphans == 0


SweeperMachine.TestCase.settings = settings(
    max_examples=100,
    stateful_step_count=35,
    deadline=None,
    database=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.data_too_large,
        HealthCheck.filter_too_much,
    ],
)

TestSweeperProperties = SweeperMachine.TestCase
