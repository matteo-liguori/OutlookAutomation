"""The owner's clean-up rules: old mail to Trash, only under the owner's own grant."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from chamberlain.app.context import TickSteps
from chamberlain.attention import tick as attention_tick
from chamberlain.cleanup.rules import CleanupConfig, CleanupRule, own_folders
from chamberlain.cleanup.run import daily_cleanup, propose_grants, run_cleanup
from chamberlain.domain.mail import Folder, FolderRole, MailQuery
from chamberlain.livetest.fakeworld import FAKE_MAIL_A, build_fake_lab
from chamberlain.writes.confirm import confirm_grant

RULES = CleanupConfig(
    rules=(
        CleanupRule(
            name="Inbox-only mail after three years",
            sources=(FAKE_MAIL_A,),
            folder="inbox",
            older_than_days=1095,
            only_unfiled=True,
        ),
    )
)


def inbox_ids(lab, folder: str = "f-inbox") -> set[str]:  # type: ignore[no-untyped-def]
    page = lab.provider.mail.search(FAKE_MAIL_A, MailQuery(folder_id=folder, limit=50))
    return {item.internet_message_id for item in page.items}


def test_old_inbox_mail_goes_to_trash_only_after_the_owner_confirms(tmp_path: Path) -> None:
    lab = build_fake_lab(tmp_path / "data")
    try:
        now = lab.clock.now()
        for number in (1, 2):
            lab.provider.mail.add_message(
                FAKE_MAIL_A,
                internet_message_id=f"<old{number}@x>",
                date=now - timedelta(days=4 * 365),
            )
        lab.provider.mail.add_message(
            FAKE_MAIL_A, internet_message_id="<recent@x>", date=now - timedelta(days=30)
        )
        plan = run_cleanup(lab.app, RULES, dry_run=True)
        assert plan.planned == {"Inbox-only mail after three years": 2}
        assert inbox_ids(lab) == {"<old1@x>", "<old2@x>", "<recent@x>"}, "a plan moves nothing"

        before = run_cleanup(lab.app, RULES, dry_run=False)
        assert before.moved == 0
        assert before.waiting == [FAKE_MAIL_A], "nothing moves until the owner confirms"

        [(source, state, grant)] = propose_grants(lab.app, RULES)
        assert (source, state) == (FAKE_MAIL_A, "proposed")
        row = lab.app.context.db.execute(
            "SELECT content_sha256 FROM grants WHERE handle = ?", (grant,)
        ).fetchone()
        # The test stands in for the owner clicking confirm in the viewer.
        confirm_grant(lab.app.context, grant, {"content_sha256": row["content_sha256"]})

        done = run_cleanup(lab.app, RULES, dry_run=False)
        assert (done.moved, done.failed) == (2, [])
        assert inbox_ids(lab) == {"<recent@x>"}
        assert inbox_ids(lab, "f-trash") == {"<old1@x>", "<old2@x>"}
        assert run_cleanup(lab.app, RULES, dry_run=True).planned == {
            "Inbox-only mail after three years": 0
        }
    finally:
        lab.close()


def test_the_background_clean_up_is_off_by_default_and_runs_once_a_day(tmp_path: Path) -> None:
    lab = build_fake_lab(tmp_path / "data")
    try:
        now = lab.clock.now()
        lab.provider.mail.add_message(
            FAKE_MAIL_A, internet_message_id="<old@x>", date=now - timedelta(days=4 * 365)
        )
        assert CleanupConfig().enabled is False
        assert daily_cleanup(lab.app, RULES) is None, "not turned on: nothing at all"
        assert lab.app.context.db.execute("SELECT COUNT(*) FROM grants").fetchone()[0] == 0

        on = RULES.model_copy(update={"enabled": True})
        first = daily_cleanup(lab.app, on)
        assert first is not None
        assert first.waiting == [FAKE_MAIL_A]
        grant = lab.app.context.db.execute(
            "SELECT handle, content_sha256 FROM grants WHERE state = 'proposed'"
        ).fetchone()
        assert grant is not None, "the background run proposes the grant for the owner"
        confirm_grant(lab.app.context, grant["handle"], {"content_sha256": grant["content_sha256"]})
        assert daily_cleanup(lab.app, on) is None, "at most once a day"
        assert inbox_ids(lab) == {"<old@x>"}

        lab.clock.advance(hours=24)
        second = daily_cleanup(lab.app, on)
        assert second is not None
        assert second.moved == 1
        assert inbox_ids(lab) == set()
    finally:
        lab.close()


def test_background_steps_run_only_in_the_schedulers_tick(tmp_path: Path) -> None:
    lab = build_fake_lab(tmp_path / "data")
    try:
        seen: list[str] = []
        steps = TickSteps(background=(lambda ctx: seen.append("ran"),))
        ctx = replace(lab.app.context, tick_steps=steps)
        attention_tick.tick(ctx, force=True)
        assert seen == [], "a tool call's tick never runs the slow background steps"
        attention_tick.tick(ctx, force=True, background=True)
        assert seen == ["ran"]
    finally:
        lab.close()


def test_a_message_in_one_of_the_owners_folders_or_starred_is_kept() -> None:
    folders = [
        Folder(id="inbox", name="Inbox", path="/INBOX", role=FolderRole.INBOX),
        Folder(id="all", name="All Mail", path="/[Gmail]/All Mail", role=FolderRole.ARCHIVE),
        Folder(id="star", name="Starred", path="/[Gmail]/Starred", role=FolderRole.OTHER),
        Folder(id="imp", name="Important", path="/[Gmail]/Important", role=FolderRole.OTHER),
        Folder(id="bank", name="Bank", path="/Bank", role=FolderRole.OTHER),
        Folder(id="trash", name="Trash", path="/[Gmail]/Trash", role=FolderRole.TRASH),
    ]
    assert own_folders(folders) == {"bank", "star"}
