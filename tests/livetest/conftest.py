"""Fixtures for the live-test self-test: the fake world, and the test playing the owner."""

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from chamberlain.app.context import Context
from chamberlain.domain.calendar import EventFields
from chamberlain.domain.intents import MailSend
from chamberlain.livetest.authority import start_standing_proposals
from chamberlain.livetest.fakeworld import (
    FAKE_CAL_A,
    FAKE_MAIL_A,
    FAKE_MAIL_B,
    FakeLab,
    build_fake_lab,
)
from chamberlain.livetest.runner import RunOptions, open_lab
from chamberlain.writes.confirm import ConfirmationApp, confirm_grant
from chamberlain.writes.core import Engine


def confirm_all(lab: FakeLab, options: RunOptions | None = None) -> list[str]:
    """What the owner does in the viewer: open each proposed grant and confirm it.

    This is the test's own code, standing in for a person. The runner under test has no
    way to do it (`test_the_runner_cannot_confirm`).
    """
    chosen = options or RunOptions()
    live = open_lab(lab.app, lab.env, lab.config, "20260105T090000-0000", chosen)
    states = start_standing_proposals(live, list(lab.config.mail))
    handles: list[str] = []
    for state in states:
        if state.state == "proposed":
            ctx: Context = lab.app.context
            grant = lab.app.context.db.execute(
                "SELECT content_sha256 FROM grants WHERE handle = ?", (state.handle,)
            ).fetchone()
            confirm_grant(ctx, state.handle, {"content_sha256": grant["content_sha256"]})
            handles.append(state.handle)
    return handles


@dataclass
class OwnerState:
    """What the owner already had before the run; the run must leave all of it alone."""

    foreign_operation: str
    inbox_subjects: dict[str, list[str]]
    event_title: str


def seed_owner_state(lab: FakeLab, *, competing_card_to: str = "friend@example.test") -> OwnerState:
    """Real-looking existing state: an uncertain send, an open card that corresponds with
    `competing_card_to`, unread unmarked mail (and lookalike subjects), an unmarked event."""
    ctx = lab.app.context
    engine = Engine(ctx)
    foreign = engine.propose_intent(
        MailSend(
            source=FAKE_MAIL_A, to=("stranger@example.org",), subject="Did this go out?", body="x"
        )
    ).operation
    assert foreign is not None
    ctx.db.execute(
        "UPDATE operations SET state = 'uncertain', dispatched_at = ? WHERE handle = ?",
        ("2026-01-05T08:00:00Z", foreign.handle),
    )
    started = lab.app.call("start", {"agent": "owner"}).model_dump(mode="json")
    session = started["data"]["session"]
    card = lab.app.call(
        "card", {"op": "create", "fields": {"title": "Owner's own case"}, "session": session}
    ).model_dump(mode="json")
    card_handle = card["data"]["card"]["handle"]
    engine.propose_intent(
        MailSend(source=FAKE_MAIL_A, to=(competing_card_to,), subject="Case mail", body="y"),
        card=card_handle,
    )
    subjects = {
        FAKE_MAIL_A: ["Quarterly numbers", "About CHAMBERLAIN LIVE TEST 2026 plans"],
        FAKE_MAIL_B: ["Lunch on Friday"],
    }
    for source, names in subjects.items():
        for number, subject in enumerate(names, start=1):
            lab.provider.mail.add_message(
                source,
                internet_message_id=f"<own{number}-{source[-1]}@example.test>",
                subject=subject,
            )
    start = datetime(2026, 1, 7, 10, 0, tzinfo=UTC)
    title = "Dentist"
    lab.provider.calendar.add_event(FAKE_CAL_A, EventFields(title=title, start=start, end=start))
    return OwnerState(foreign.handle, subjects, title)


VIEWER_ORIGIN = "http://127.0.0.1:5555"
VIEWER_TOKEN = "v" * 43


def owner_clicks(lab: FakeLab) -> Callable[[str], None]:
    """The owner confirming a held reply in the viewer: the real confirm endpoint, which sends."""

    def click(grant: str) -> None:
        ctx: Context = lab.app.context
        row = ctx.db.execute("SELECT content_sha256 FROM grants WHERE handle = ?", (grant,))
        shown = row.fetchone()["content_sha256"]
        viewer = ConfirmationApp(lambda: ctx, token=VIEWER_TOKEN, origin=VIEWER_ORIGIN)
        headers = {"Host": "127.0.0.1:5555", "X-Chamberlain-Token": VIEWER_TOKEN}
        headers["Origin"] = VIEWER_ORIGIN
        body = json.dumps({"content_sha256": shown}).encode()
        reply = viewer.handle("POST", f"/api/grants/{grant}/confirm", headers, body)
        assert reply.status == 200, reply.body

    return click


@pytest.fixture
def lab(tmp_path: Path) -> Iterator[FakeLab]:
    built = build_fake_lab(tmp_path / "data")
    built.env.owner_confirms = owner_clicks(built)
    try:
        yield built
    finally:
        built.close()
