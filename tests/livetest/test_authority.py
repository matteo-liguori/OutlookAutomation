"""Authority: one standing grant per mailbox, proposed never confirmed, and the run stops
when it is missing or waiting. The runner has no way to confirm anything."""

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
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                names = [node.module or "", *[a.name for a in node.names]]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.Name):
                names = [node.id]
            elif isinstance(node, ast.Attribute):
                names = [node.attr]
            else:
                continue
            offenders += [
                f"{path.name}: {name}"
                for name in names
                if name.startswith("chamberlain.writes.confirm") or name in FORBIDDEN_NAMES
            ]
    assert not offenders


def test_grant_proposes_exactly_the_scope_and_only_the_owner_confirms(lab: FakeLab) -> None:
    live = open_lab(lab.app, lab.env, lab.config, "20260105T090000-0000", RunOptions())
    sources = [FAKE_MAIL_A, FAKE_MAIL_B]
    states = authority.start_standing_proposals(live, sources)
    assert [s.state for s in states] == ["proposed", "proposed"]
    again = authority.start_standing_proposals(live, sources)
    assert [s.handle for s in again] == [s.handle for s in states]  # no second proposal
    waiting = execute(lab.app, lab.env, lab.config, RunOptions())
    assert "waiting for you" in waiting.stopped

    confirm_all(lab)
    card = authority.ensure_card(live)
    assert live.ok("open", handle=card)["view"]["title"] == CARD_TITLE
    for source in sources:
        [hit] = live.ok("find", scope="grants", query=grant_title(source))["hits"]
        row = repo.get_grant(lab.app.context.db, hit["handle"])
        assert row is not None
        assert (row.kind, row.state) == (GrantKind.STANDING, GrantState.ACTIVE)
        assert row.requirement.value == "human_confirmation"
        scope = row.scope
        assert scope is not None
        assert (scope.source, scope.card) == (source, card)
        assert scope.recipients == {ADDRESS_A, ADDRESS_B}
        assert scope.recipient_domains == frozenset()
        assert scope.intent_kinds == {
            IntentKind.MAIL_SEND,
            IntentKind.MAIL_DRAFT_SAVE,
            IntentKind.MAIL_DRAFT_UPDATE,
            IntentKind.MAIL_MOVE,
            IntentKind.MAIL_TRASH,
            IntentKind.MAIL_MARK_READ,
        }
        assert scope.folders <= {f.id for f in lab.env.probe.folders(source)}


def test_a_run_stops_before_doing_anything_without_a_confirmed_grant(lab: FakeLab) -> None:
    report = execute(lab.app, lab.env, lab.config, RunOptions())
    assert "No confirmed live-test grant" in report.stopped
    assert "chamberlain live-test grant" in report.stopped
    assert all(r.status is Status.SKIPPED for r in report.results)

    confirm_all(lab)
    moved = lab.config.mail[FAKE_MAIL_B].model_copy(update={"address": "new.b@lab.example.test"})
    changed = lab.config.model_copy(update={"mail": {**lab.config.mail, FAKE_MAIL_B: moved}})
    stale = execute(lab.app, lab.env, changed, RunOptions())
    assert (
        "No confirmed live-test grant" in stale.stopped
    )  # a changed address list needs a new grant
    assert lab.provider.dispatch_log == []
    assert all(not messages for messages in lab.provider.world.mail.values())
