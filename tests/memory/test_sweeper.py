"""The sweeper: docs/contracts/06-memory.md (M-21 to M-26, lifetime table)."""

import os
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from memory_helpers import blobs, keep_text, make_context, table_dump

from chamberlain.app.context import Context
from chamberlain.domain.canonical import sha256_bytes
from chamberlain.limits import BACKUP_DAILY_SLOTS, BACKUP_WEEKLY_SLOTS
from chamberlain.memory import backups, repo
from chamberlain.memory import sweeper as sweeper_module
from chamberlain.memory.artifacts import ArtifactService, blob_path, write_blob
from chamberlain.memory.cards import CardService
from chamberlain.memory.checks import storage_checks
from chamberlain.memory.restore import restore
from chamberlain.memory.sweeper import SweepCrash, SweepHooks, rule_names, sweep
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.store.schema import get_state
from chamberlain.writes.repo import audit_rows


def held(ctx: Context) -> list[repo.Row]:
    return repo.held_quarantine(ctx.db)


def test_m21_a_sweep_is_audited_as_one_row_and_idempotent(
    ctx: Context, store: ArtifactService, action: str, clock: ManualClock
) -> None:
    keep_text(store, action, "something")
    sweep(ctx, clock.now())
    assert [row.kind for row in audit_rows(ctx.db)] == ["sweep"]
    clock.advance(hours=24 * 40)
    first = sweep(ctx, clock.now())
    assert first.changed()
    before = (table_dump(ctx.db), blobs(ctx.data_root))
    second = sweep(ctx, clock.now())
    assert not second.changed()
    assert (table_dump(ctx.db), blobs(ctx.data_root)) == before
    assert [row.kind for row in audit_rows(ctx.db)] == ["sweep", "sweep", "sweep"]


@pytest.mark.parametrize("crash_after", rule_names())
def test_m21_an_interrupted_sweep_is_finished_by_the_next_one(
    tmp_path: Path, crash_after: str
) -> None:
    clock = ManualClock()
    ctx = make_context(tmp_path / "data", clock)
    try:
        cards = CardService(ctx.db, clock)
        store = ArtifactService(ctx.db, clock, ctx.data_root)
        card = cards.create(title="Interrupted")
        action = cards.add_action(card.handle, None, title="t", due_at=None, snooze_until=None)
        handle = action.actions[0].handle
        released = keep_text(store, handle, "will be released")
        keep_text(store, handle, "expires soon", expires_in_hours=1)
        cards.finish_action(handle, None, "Done.")
        stray = ctx.data_root / "stray.bin"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_bytes(b"orphan")
        clock.advance(hours=48)

        def die(name: str) -> None:
            if name == crash_after:
                raise SweepCrash(name)

        with pytest.raises(SweepCrash):
            sweep(ctx, clock.now(), hooks=SweepHooks(after_rule=die))
        sweep(ctx, clock.now())
        states = {r["state"] for r in ctx.db.execute("SELECT state FROM artifacts")}
        assert states <= {"deleted"}
        assert blobs(ctx.data_root) == set()
        assert not stray.exists()
        again = sweep(ctx, clock.now())
        assert not again.changed()
        assert released
    finally:
        ctx.db.close()


def test_m21_a_crash_between_unlink_and_marking_is_recovered(
    ctx: Context, store: ArtifactService, action: str, clock: ManualClock
) -> None:
    handle = keep_text(store, action, "crashy")
    store.release(handle, None)

    def unlink_then_die(path: Path) -> None:
        path.unlink()
        raise SweepCrash("died after the unlink")

    with pytest.raises(SweepCrash):
        sweep(ctx, clock.now(), hooks=SweepHooks(unlink=unlink_then_die))
    assert blobs(ctx.data_root) == set()
    state = ctx.db.execute("SELECT state FROM artifacts WHERE handle = ?", (handle,)).fetchone()
    assert state["state"] == "released"
    sweep(ctx, clock.now())
    state = ctx.db.execute("SELECT state FROM artifacts WHERE handle = ?", (handle,)).fetchone()
    assert state["state"] == "deleted"


def test_m23_a_failed_deletion_is_visible_and_retried(
    ctx: Context, store: ArtifactService, action: str, clock: ManualClock
) -> None:
    handle = keep_text(store, action, "locked file")
    store.release(handle, None)

    def locked(path: Path) -> None:
        raise PermissionError("file is locked")

    report = sweep(ctx, clock.now(), hooks=SweepHooks(unlink=locked))
    assert report.failed_deletions == 1
    assert get_state(ctx.db, "memory.pending_deletions") == "1"
    assert len(blobs(ctx.data_root)) == 1
    statuses = {c.id: c.status for c in storage_checks(ctx)}
    assert statuses["storage.deletions"] == "warn"
    retry = sweep(ctx, clock.now())
    assert retry.failed_deletions == 0
    assert get_state(ctx.db, "memory.pending_deletions") == "0"
    assert blobs(ctx.data_root) == set()
    assert {c.id: c.status for c in storage_checks(ctx)}["storage.deletions"] == "ok"


def _interrupted_sweep_is_finished(tmp_path: Path, crash_after: str) -> None:
    clock = ManualClock()
    ctx = make_context(tmp_path / "data", clock)
    try:
        cards = CardService(ctx.db, clock)
        store = ArtifactService(ctx.db, clock, ctx.data_root)
        card = cards.create(title="Interrupted")
        action = cards.add_action(card.handle, None, title="t", due_at=None, snooze_until=None)
        handle = action.actions[0].handle
        released = keep_text(store, handle, "will be released")
        keep_text(store, handle, "expires soon", expires_in_hours=1)
        cards.finish_action(handle, None, "Done.")
        stray = ctx.data_root / "stray.bin"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_bytes(b"orphan")
        clock.advance(hours=48)

        def die(name: str) -> None:
            if name == crash_after:
                raise SweepCrash(name)

        with pytest.raises(SweepCrash):
            sweep(ctx, clock.now(), hooks=SweepHooks(after_rule=die))
        sweep(ctx, clock.now())
        states = {r["state"] for r in ctx.db.execute("SELECT state FROM artifacts")}
        assert states <= {"deleted"}
        assert blobs(ctx.data_root) == set()
        assert not stray.exists()
        again = sweep(ctx, clock.now())
        assert not again.changed()
        assert released
    finally:
        ctx.db.close()


def test_m26_orphans_are_quarantined_reported_once_then_deleted_after_the_grace_period(
    ctx: Context, clock: ManualClock, tmp_path: Path
) -> None:
    root = ctx.data_root
    root.mkdir(parents=True, exist_ok=True)
    (root / "stray.txt").write_text("not ours")
    (root / "junk").mkdir()
    (root / "junk" / "inner.txt").write_text("also not ours")
    stray_blob = blob_path(root, "ab" + "0" * 62)
    stray_blob.parent.mkdir(parents=True)
    stray_blob.write_bytes(b"blob without a row")
    long_ago = clock.now().timestamp() - 3600
    os.utime(stray_blob, (long_ago, long_ago))
    (root / "bridge").mkdir()
    (root / "bridge" / "token").write_text("kept")
    (root / "config").mkdir()
    (root / "config" / "settings.yaml").write_text("kept: true")
    outside = tmp_path / "outside.txt"
    outside.write_text("must survive")
    first = sweep(ctx, clock.now())
    assert first.orphans_found == 3
    assert not (root / "stray.txt").exists()
    assert not (root / "junk").exists()
    assert not stray_blob.exists()
    assert (root / "bridge" / "token").read_text() == "kept"  # R9
    assert (root / "config" / "settings.yaml").exists()
    assert len(held(ctx)) == 3
    assert get_state(ctx.db, "memory.last_orphan_scan_at") == "2026-01-05T09:00:00Z"
    assert {row["found_at"] for row in held(ctx)} == {"2026-01-05T09:00:00Z"}
    clock.advance(hours=1)
    again = sweep(ctx, clock.now())
    assert again.orphans_found == 0  # reported once
    assert len(held(ctx)) == 3
    quarantined = [Path(str(row["quarantined_path"])) for row in held(ctx)]
    assert all(p.exists() and "quarantine" in p.parts for p in quarantined)
    clock.advance(hours=24 * 13)
    sweep(ctx, clock.now())
    assert all(p.exists() for p in quarantined)
    clock.advance(hours=24 * 2)
    sweep(ctx, clock.now())
    assert not any(p.exists() for p in quarantined)
    assert held(ctx) == []
    assert outside.read_text() == "must survive"


def test_m26_a_symlink_is_moved_but_its_target_is_never_followed(
    ctx: Context, clock: ManualClock, tmp_path: Path
) -> None:
    target = tmp_path / "precious"
    target.mkdir()
    (target / "keep.txt").write_text("precious")
    ctx.data_root.mkdir(parents=True, exist_ok=True)
    try:
        (ctx.data_root / "escape").symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("this Windows account cannot create symlinks; run with developer mode")
    sweep(ctx, clock.now())
    clock.advance(hours=24 * 15)
    sweep(ctx, clock.now())
    assert (target / "keep.txt").read_text() == "precious"


def test_m25_the_fixed_layout_is_never_quarantined(ctx: Context, clock: ManualClock) -> None:
    for name in sweeper_module.DATA_ROOT_LAYOUT:
        if name.startswith("chamberlain"):
            continue
        (ctx.data_root / name).mkdir(parents=True, exist_ok=True)
    report = sweep(ctx, clock.now())
    assert report.orphans_found == 0


def test_m21_p3_a_row_without_a_file_is_cleared_and_a_file_without_a_row_is_quarantined(
    ctx: Context, store: ArtifactService, action: str, clock: ManualClock
) -> None:
    handle = keep_text(store, action, "vanishes")
    blob_path(ctx.data_root, sha256_bytes(b"vanishes")).unlink()
    report = sweep(ctx, clock.now())
    assert report.total("rows_without_file") == 1
    with pytest.raises(ChamberlainError) as gone:
        store.read_bytes(handle)
    assert gone.value.code is ErrorCode.ARTIFACT_EXPIRED


def test_m3_tombstones_vanish_after_a_week(
    ctx: Context, store: ArtifactService, action: str, clock: ManualClock
) -> None:
    handle = keep_text(store, action, "tombstone")
    store.release(handle, None)
    sweep(ctx, clock.now())
    assert repo.get_artifact(ctx.db, handle) is not None
    clock.advance(hours=24 * 8)
    sweep(ctx, clock.now())
    assert repo.get_artifact(ctx.db, handle) is None


def test_m4_unclaimed_inputs_expire_but_pending_work_survives(
    ctx: Context, store: ArtifactService, cards: CardService, clock: ManualClock, session: str
) -> None:
    scratch = keep_text(store, session, "session scratch")
    card = cards.create(title="Pending work")
    action = cards.add_action(card.handle, None, title="t", due_at=None, snooze_until=None)
    owned = keep_text(
        store, action.actions[0].handle, "owned by open work", expires_in_hours=24 * 60
    )
    clock.advance(hours=5)  # past the 4 hour idle limit for the session
    sweep(ctx, clock.now())
    with pytest.raises(ChamberlainError):
        store.read_bytes(scratch)
    assert store.read_bytes(owned) == b"owned by open work"


def test_m24_a_backup_never_holds_bytes_and_a_restore_cannot_revive_expired_content(
    tmp_path: Path,
) -> None:
    clock = ManualClock(datetime(2026, 1, 5, 9, 0, tzinfo=UTC))
    ctx = make_context(tmp_path / "data", clock)
    restored = None
    try:
        cards = CardService(ctx.db, clock)
        store = ArtifactService(ctx.db, clock, ctx.data_root)
        card = cards.create(title="Backed up")
        handle = cards.add_action(card.handle, None, title="t", due_at=None, snooze_until=None)
        art = keep_text(store, handle.actions[0].handle, "will expire", expires_in_hours=24)
        slot = backups.backup(ctx.db, clock, ctx.data_root)[0]
        assert slot.read_bytes()[:6] == b"SQLite"
        assert all(path.suffix == ".sqlite3" for path in (ctx.data_root / "backups").iterdir())
        assert b"will expire" not in slot.read_bytes()
        clock.advance(hours=24 * 3)
        sweep(ctx, clock.now())
        assert blobs(ctx.data_root) == set()
        ctx.db.close()
        restored, report = restore(slot, ctx.data_root, clock)
        row = restored.execute("SELECT state FROM artifacts WHERE handle = ?", (art,)).fetchone()
        assert row is None or row["state"] != "live"  # the sweeper ran before first use
        assert blobs(ctx.data_root) == set()
        assert report.total("artifacts_expired") == 1
    finally:
        if restored is not None:
            restored.close()


def test_m24_backup_slots_are_fixed(ctx: Context, clock: ManualClock) -> None:
    for _ in range(20):
        backups.backup(ctx.db, clock, ctx.data_root)
        clock.advance(hours=24)
    names = sorted(p.name for p in (ctx.data_root / "backups").iterdir())
    assert len([n for n in names if n.startswith("daily-")]) == BACKUP_DAILY_SLOTS
    assert len([n for n in names if n.startswith("weekly-")]) <= BACKUP_WEEKLY_SLOTS
    assert get_state(ctx.db, "memory.last_backup_at") is not None


def test_m3_logs_rotate_by_age_and_size_and_stale_temp_files_go(
    ctx: Context, clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    logs = ctx.data_root / "logs"
    logs.mkdir(parents=True)
    old = logs / "old.log"
    old.write_text("old")
    fresh = logs / "fresh.log"
    fresh.write_text("fresh")
    now = clock.now().timestamp()
    os.utime(old, (now - 15 * 86400, now - 15 * 86400))
    os.utime(fresh, (now - 60, now - 60))
    run = ctx.data_root / "run"
    run.mkdir()
    stale = run / "write-dead.tmp"
    stale.write_text("half written")
    os.utime(stale, (now - 7200, now - 7200))
    sweep(ctx, clock.now())
    assert not old.exists()
    assert fresh.exists()
    assert not stale.exists()
    monkeypatch.setattr(sweeper_module, "LOG_MAX_BYTES", 4)
    (logs / "newer.log").write_text("more than four bytes")
    os.utime(logs / "newer.log", (now - 30, now - 30))
    sweep(ctx, clock.now())
    assert not fresh.exists()  # the oldest goes first when the folder is over its size


def test_exports_go_after_a_week(ctx: Context, clock: ManualClock) -> None:
    exports = ctx.data_root / "exports"
    exports.mkdir(parents=True)
    old, fresh = exports / "old thread.zip", exports / "new thread.zip"
    old.write_bytes(b"zip")
    fresh.write_bytes(b"zip")
    now = clock.now().timestamp()
    os.utime(old, (now - 8 * 86400, now - 8 * 86400))
    os.utime(fresh, (now - 86400, now - 86400))
    sweep(ctx, clock.now())
    assert not old.exists()
    assert fresh.exists()


def test_m3_other_tracks_compactors_run_inside_the_one_sweep(
    ctx: Context, clock: ManualClock
) -> None:
    seen: list[datetime] = []

    def compact_operations(context: Context, now: datetime) -> int:
        seen.append(now)
        return 3

    report = sweep(ctx, clock.now(), compactors=[("operations_compacted", compact_operations)])
    assert report.total("operations_compacted") == 3
    assert seen == [clock.now()]
    assert len(audit_rows(ctx.db)) == 1
    assert "operations_compacted=3" in audit_rows(ctx.db)[0].summary


def test_m26_a_directory_junction_is_never_followed_or_left_held_forever(
    ctx: Context, clock: ManualClock, tmp_path: Path
) -> None:
    if os.name != "nt":
        pytest.skip("junctions exist only on Windows; the symlink test covers other systems")
    target = tmp_path / "precious"
    target.mkdir()
    (target / "keep.txt").write_text("precious")
    ctx.data_root.mkdir(parents=True, exist_ok=True)
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(ctx.data_root / "escape"), str(target)],
        capture_output=True,
        check=False,
    )
    if made.returncode != 0:
        pytest.skip("could not create a junction here")
    report = sweep(ctx, clock.now())
    assert report.orphans_found == 1
    assert (target / "keep.txt").read_text() == "precious"
    clock.advance(hours=24 * 15)
    sweep(ctx, clock.now())
    assert (target / "keep.txt").read_text() == "precious"
    assert held(ctx) == []
    assert get_state(ctx.db, "memory.pending_deletions") == "0"
    assert not any(p.name == "escape" for p in (ctx.data_root / "quarantine").rglob("*"))


def test_m25_import_report_and_secrets_are_never_quarantined(
    ctx: Context, clock: ManualClock
) -> None:
    (ctx.data_root / "import").mkdir(parents=True, exist_ok=True)
    (ctx.data_root / "import" / "report.json").write_text("{}")
    (ctx.data_root / "secrets").mkdir(parents=True, exist_ok=True)
    (ctx.data_root / "secrets" / "store.json").write_text("{}")
    sweep(ctx, clock.now())
    clock.advance(hours=24 * 15)
    report = sweep(ctx, clock.now())
    assert report.orphans_found == 0
    assert (ctx.data_root / "import" / "report.json").exists()
    assert (ctx.data_root / "secrets" / "store.json").exists()


def test_m7_a_blob_younger_than_ten_minutes_is_not_an_and_1_related_checks(
    ctx: Context, clock: ManualClock, store: ArtifactService, action: str
) -> None:
    # m7_a_blob_younger_than_ten_minutes_is_not_an_orphan_yet: Another process may be between `w
    sha = sha256_bytes(b"in flight")
    write_blob(ctx.data_root, b"in flight", sha)
    path = blob_path(ctx.data_root, sha)
    now = clock.now().timestamp()
    os.utime(path, (now - 5 * 60, now - 5 * 60))
    assert sweep(ctx, clock.now()).orphans_found == 0
    assert path.exists()
    os.utime(path, (now - 11 * 60, now - 11 * 60))
    assert sweep(ctx, clock.now()).orphans_found == 1
    assert not path.exists()

    # m7_a_blob_restored_by_another_process_survives_the_delete_step: Interleaving: the sweep ha
    handle = keep_text(store, action, "shared bytes")
    sha = sha256_bytes(b"shared bytes")
    store.release(handle, None)

    def writer_wins(path: Path) -> None:
        raise AssertionError("the sweeper unlinked bytes that have a live row")

    real = repo.sha_has_live_row
    calls = {"n": 0}

    def racing(db: sqlite3.Connection, value: str) -> bool:
        calls["n"] += 1
        if calls["n"] == 2 and value == sha:  # the re-check inside the lock
            ctx.db.execute("UPDATE artifacts SET state = 'live' WHERE sha256 = ?", (sha,))
        return real(db, value)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(repo, "sha_has_live_row", racing)
        report = sweep(ctx, clock.now(), hooks=SweepHooks(unlink=writer_wins))
    assert calls["n"] >= 2
    assert report.failed_deletions == 0
    assert blob_path(ctx.data_root, sha).exists()
