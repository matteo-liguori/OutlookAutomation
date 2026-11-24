"""Authority: one standing grant per mailbox, proposed never confirmed, and the run stops
when it is missing or waiting. The runner has no way to confirm anything (layering test)."""

import ast
from pathlib import Path

from tests.livetest.conftest import confirm_all

from chamberlain.domain.grants import GrantKind, GrantState
from chamberlain.domain.intents import IntentKind
from chamberlain.livetest import authority
from chamberlain.livetest.fakeworld import (
    ADDRESS_A,
    ADDRESS_B,
    FAKE_MAIL_A,
    FAKE_MAIL_B,
    FakeLab,
)
from chamberlain.livetest.markers import CARD_TITLE, grant_title
from chamberlain.livetest.model import Status
from chamberlain.livetest.runner import RunOptions, execute, open_lab
from chamberlain.writes import repo

LIVETEST = Path(__file__).resolve().parents[2] / "src" / "chamberlain" / "livetest"
FORBIDDEN_NAMES = {
    "confirm_grant",
    "decline_grant",
    "revoke_grant",
    "keep_grant",
    "resolve_operation",
    "ConfirmationApp",
    "Engine",
}


def test_the_runner_cannot_confirm() -> None:
    """No module of the package imports the confirmation code or the engine."""
    offenders: list[str] = []
    for path in sorted(LIVETEST.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom | ast.Import):
                names = (
                    [node.module or ""] + [a.name for a in node.names]
                    if isinstance(node, ast.ImportFrom)
                    else [a.name for a in node.names]
                )
                for name in names:
                    if name.startswith("chamberlain.writes.confirm") or name in FORBIDDEN_NAMES:
                        offenders.append(f"{path.name}: imports {name}")
            elif isinstance(node, ast.Name | ast.Attribute):
                name = node.id if isinstance(node, ast.Name) else node.attr
                if name in FORBIDDEN_NAMES:
                    offenders.append(f"{path.name}: uses {name}")
    assert not offenders


def test_grant_proposes_one_standing_grant_per_mailbox_and_never_confirms(lab: FakeLab) -> None:
    live = open_lab(lab.app, lab.env, lab.config, "20260105T090000-0000", RunOptions())
    states = authority.start_standing_proposals(live, [FAKE_MAIL_A, FAKE_MAIL_B])
    assert [s.state for s in states] == ["proposed", "proposed"]
    rows = [repo.get_grant(lab.app.context.db, s.handle) for s in states]
    for row in rows:
        assert row is not None
        assert row.kind is GrantKind.STANDING
        assert row.state is GrantState.PROPOSED
    again = authority.start_standing_proposals(live, [FAKE_MAIL_A, FAKE_MAIL_B])
    assert [s.handle for s in again] == [s.handle for s in states]  # no second proposal


def test_the_grant_covers_exactly_the_owned_recipients_and_the_test_card(lab: FakeLab) -> None:
    confirm_all(lab)
    db = lab.app.context.db
    live = open_lab(lab.app, lab.env, lab.config, "20260105T090000-0000", RunOptions())
    card = authority.ensure_card(live)
    assert live.ok("open", handle=card)["view"]["title"] == CARD_TITLE
    for source in (FAKE_MAIL_A, FAKE_MAIL_B):
        found = live.ok("find", scope="grants", query=grant_title(source))["hits"]
        assert len(found) == 1
        row = repo.get_grant(db, found[0]["handle"])
        assert row is not None
        assert row.state is GrantState.ACTIVE
        scope = row.scope
        assert scope is not None
        assert scope.source == source
        assert scope.recipients == {ADDRESS_A, ADDRESS_B}
        assert scope.recipient_domains == frozenset()
        assert scope.card == card
        assert scope.intent_kinds == {
            IntentKind.MAIL_SEND,
            IntentKind.MAIL_DRAFT_SAVE,
            IntentKind.MAIL_DRAFT_UPDATE,
            IntentKind.MAIL_MOVE,
            IntentKind.MAIL_TRASH,
            IntentKind.MAIL_MARK_READ,
        }
        ids = {f.id for f in lab.env.probe.folders(source)}
        assert scope.folders <= ids
        assert row.requirement.value == "human_confirmation"


def test_a_run_stops_before_doing_anything_without_a_confirmed_grant(lab: FakeLab) -> None:
    report = execute(lab.app, lab.env, lab.config, RunOptions())
    assert "No confirmed live-test grant" in report.stopped
    assert "chamberlain live-test grant" in report.stopped
    assert all(r.status is Status.SKIPPED for r in report.results)
    assert lab.provider.dispatch_log == []
    assert all(not messages for messages in lab.provider.world.mail.values())


def test_a_run_stops_while_a_grant_is_waiting_for_the_owner(lab: FakeLab) -> None:
    live = open_lab(lab.app, lab.env, lab.config, "20260105T090000-0000", RunOptions())
    authority.start_standing_proposals(live, [FAKE_MAIL_A, FAKE_MAIL_B])
    report = execute(lab.app, lab.env, lab.config, RunOptions())
    assert "waiting for you" in report.stopped
    assert lab.provider.dispatch_log == []


def test_a_changed_address_list_needs_a_new_confirmed_grant(lab: FakeLab) -> None:
    confirm_all(lab)
    moved = lab.config.mail[FAKE_MAIL_B].model_copy(update={"address": "new.b@lab.example.test"})
    changed = lab.config.model_copy(update={"mail": {**lab.config.mail, FAKE_MAIL_B: moved}})
    report = execute(lab.app, lab.env, changed, RunOptions())
    assert "No confirmed live-test grant" in report.stopped
    assert lab.provider.dispatch_log == []
