"""The artifact store: docs/contracts/06-memory.md (M-4 to M-11, M-27)."""

import os
from pathlib import Path

import pytest
from memory_helpers import add_grant, add_operation, blobs, keep_text, settle_operation

from chamberlain.app.context import Context
from chamberlain.domain.artifacts import ArtifactKind
from chamberlain.domain.canonical import sha256_bytes
from chamberlain.memory import artifacts as artifacts_module
from chamberlain.memory.artifacts import ArtifactService, Budgets, blob_path, read_import
from chamberlain.memory.cards import CardService
from chamberlain.memory.sweeper import sweep
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.errors import ChamberlainError, ErrorCode


def test_m4_no_owner_no_storage(store: ArtifactService, cards: CardService, ctx: Context) -> None:
    with pytest.raises(ChamberlainError) as unknown:
        keep_text(store, "act_doesnotexist01")
    assert unknown.value.code is ErrorCode.ARTIFACT_NO_OWNER
    card = cards.create(title="Open card")
    with pytest.raises(ChamberlainError) as wrong:
        keep_text(store, card.handle)  # a card holds evidence only
    assert wrong.value.code is ErrorCode.ARTIFACT_NO_OWNER
    assert blobs(ctx.data_root) == set()


def test_m4_owner_ended_is_refused(store: ArtifactService, cards: CardService, action: str) -> None:
    cards.finish_action(action, None, "Done.")
    with pytest.raises(ChamberlainError) as ended:
        keep_text(store, action)
    assert ended.value.code is ErrorCode.ARTIFACT_OWNER_ENDED


def test_m6_identical_content_is_stored_once_and_shared(
    store: ArtifactService, action: str, ctx: Context
) -> None:
    first = keep_text(store, action, "same bytes")
    second = keep_text(store, action, "same bytes")
    assert first != second
    assert len(blobs(ctx.data_root)) == 1
    assert store.read_bytes(first) == store.read_bytes(second) == b"same bytes"


def test_m10_ownership_is_independent_while_bytes_deduplicate(
    store: ArtifactService, action: str, ctx: Context
) -> None:
    first = keep_text(store, action, "shared")
    second = keep_text(store, action, "shared")
    store.release(first, None)
    sweep(ctx, ctx.clock.now())
    assert store.read_bytes(second) == b"shared"
    assert len(blobs(ctx.data_root)) == 1
    store.release(second, None)
    sweep(ctx, ctx.clock.now())
    assert blobs(ctx.data_root) == set()


def test_m7_hash_mismatch_never_returns_bytes(
    store: ArtifactService, action: str, ctx: Context
) -> None:
    handle = keep_text(store, action, "original")
    path = blob_path(ctx.data_root, sha256_bytes(b"original"))
    path.write_bytes(b"tampered")
    with pytest.raises(ChamberlainError) as bad:
        store.read_bytes(handle)
    assert bad.value.code is ErrorCode.ARTIFACT_HASH_MISMATCH
    row = ctx.db.execute("SELECT state FROM artifacts WHERE handle = ?", (handle,)).fetchone()
    assert row["state"] == "deleted"
    with pytest.raises(ChamberlainError) as gone:
        store.read_bytes(handle)
    assert gone.value.code is ErrorCode.ARTIFACT_EXPIRED


def test_m7_interrupted_write_leaves_nothing(
    store: ArtifactService, action: str, ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(source: object, target: object) -> None:
        raise OSError("disk went away")

    monkeypatch.setattr(artifacts_module.os, "replace", explode)
    with pytest.raises(OSError, match="disk went away"):
        keep_text(store, action, "never lands")
    monkeypatch.undo()
    assert blobs(ctx.data_root) == set()
    assert list((ctx.data_root / "run").glob("*")) == []
    assert ctx.db.execute("SELECT count(*) AS n FROM artifacts").fetchone()["n"] == 0


def test_m8_import_copies_without_touching_the_original(
    store: ArtifactService, action: str, tmp_path: Path
) -> None:
    source = tmp_path / "outside" / "notes.txt"
    source.parent.mkdir()
    source.write_bytes(b"imported text")
    data, name = read_import(str(source))
    assert (data, name) == (b"imported text", "notes.txt")
    handle = store.keep_bytes(
        data=data, kind=ArtifactKind.IMPORT, owner=action, reason="import", filename=name
    ).handle
    assert store.read_bytes(handle) == b"imported text"
    assert source.read_bytes() == b"imported text"


def test_m8_symlink_import_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("secret")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("this Windows account cannot create symlinks; run with developer mode")
    with pytest.raises(ChamberlainError) as refused:
        read_import(str(link))
    assert refused.value.code is ErrorCode.REQUEST_INVALID


def test_m8_oversize_relative_and_folder_imports_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 100)
    monkeypatch.setattr(artifacts_module, "IMPORT_MAX_BYTES", 50)
    with pytest.raises(ChamberlainError) as oversize:
        read_import(str(big))
    assert oversize.value.code is ErrorCode.REQUEST_TOO_LARGE
    with pytest.raises(ChamberlainError):
        read_import("relative/file.txt")
    with pytest.raises(ChamberlainError):
        read_import(str(tmp_path))


def test_m9_lease_copies_the_file_and_the_sweeper_removes_it(
    store: ArtifactService, action: str, ctx: Context, clock: ManualClock
) -> None:
    handle = keep_text(store, action, "leased", filename="note.txt")
    leased = store.lease(handle)
    path = Path(leased.path)
    assert path.read_bytes() == b"leased"
    assert path.parent.parent == ctx.data_root / "leases"
    path.write_bytes(b"edited copy")
    assert store.read_bytes(handle) == b"leased"  # edits never reach the store
    sweep(ctx, clock.now())
    assert path.exists()
    clock.advance(minutes=31)
    sweep(ctx, clock.now())
    assert not path.exists()
    assert ctx.db.execute("SELECT count(*) AS n FROM leases").fetchone()["n"] == 0


def test_m10_finishing_the_owner_releases_the_hold_and_the_bytes(
    store: ArtifactService, cards: CardService, action: str, ctx: Context
) -> None:
    handle = keep_text(store, action, "to be released")
    cards.finish_action(action, None, "Done.")
    state = ctx.db.execute("SELECT state FROM artifacts WHERE handle = ?", (handle,)).fetchone()
    assert state["state"] == "released"
    sweep(ctx, ctx.clock.now())
    assert blobs(ctx.data_root) == set()
    with pytest.raises(ChamberlainError) as expired:
        store.read_bytes(handle)
    assert expired.value.code is ErrorCode.ARTIFACT_EXPIRED


def test_m10_forget_releases_one_owner_early(
    store: ArtifactService, ctx: Context, clock: ManualClock, action: str, session: str
) -> None:
    handle = keep_text(store, action, "held twice")
    store.add_owner(handle, session)
    gone, remaining = store.release(handle, action)
    assert (gone, remaining) == (False, 1)
    gone, remaining = store.release(handle, session)
    assert (gone, remaining) == (True, 0)


def test_m11_a_pending_operations_artifact_survives_expiry(
    store: ArtifactService, ctx: Context, clock: ManualClock, session: str
) -> None:
    add_operation(ctx.db, clock, "op_pending00000001", "authorised")
    handle = keep_text(store, "op_pending00000001", "attachment bytes", expires_in_hours=1)
    clock.advance(hours=960)
    sweep(ctx, clock.now())
    assert store.read_bytes(handle) == b"attachment bytes"
    settle_operation(ctx.db, clock, "op_pending00000001", "confirmed")
    sweep(ctx, clock.now())  # inside the 24 hour grace
    assert store.read_bytes(handle) == b"attachment bytes"
    clock.advance(hours=25)
    sweep(ctx, clock.now())
    with pytest.raises(ChamberlainError):
        store.read_bytes(handle)
    assert blobs(ctx.data_root) == set()


def test_m4_every_owner_kind_is_live_exactly_while_the_table_says(
    store: ArtifactService, ctx: Context, clock: ManualClock, session: str
) -> None:
    add_grant(ctx.db, clock, "grant_live0000001", "active")
    add_grant(ctx.db, clock, "grant_dead0000001", "revoked")
    keep_text(store, "grant_live0000001")
    with pytest.raises(ChamberlainError) as revoked:
        keep_text(store, "grant_dead0000001")
    assert revoked.value.code is ErrorCode.ARTIFACT_OWNER_ENDED
    handle = keep_text(store, session, "session scoped", expires_in_hours=500)
    expires = ctx.db.execute(
        "SELECT created_at, expires_at FROM artifacts WHERE handle = ?", (handle,)
    ).fetchone()
    assert expires["expires_at"] == "2026-01-06T09:00:00Z"  # session-owned: 24 hours at most
    clock.advance(hours=5)  # the session goes idle after 4 hours
    sweep(ctx, clock.now())
    with pytest.raises(ChamberlainError):
        store.read_bytes(handle)


def test_m4_default_and_maximum_expiry(store: ArtifactService, action: str, ctx: Context) -> None:
    default = store.keep_bytes(data=b"a", kind=ArtifactKind.TEXT, owner=action, reason="r")
    longest = store.keep_bytes(
        data=b"b", kind=ArtifactKind.TEXT, owner=action, reason="r", expires_in_hours=24 * 999
    )
    assert default.expires_at == "2026-02-04T09:00:00Z"  # 30 days
    assert longest.expires_at == "2026-07-04T09:00:00Z"  # 180 days


def test_m4_m10_long_open_actions_keep_what_they_own_until_renewed(
    store: ArtifactService, action: str, ctx: Context, clock: ManualClock
) -> None:
    long_lived = keep_text(store, action, "kept for a long time", expires_in_hours=24 * 180)
    short = keep_text(store, action, "default expiry")
    clock.advance(hours=744)
    sweep(ctx, clock.now())
    assert store.read_bytes(long_lived) == b"kept for a long time"
    with pytest.raises(ChamberlainError):
        store.read_bytes(short)
    renewed = keep_text(store, action, "renewed on review")
    clock.advance(hours=696)
    store.renew(renewed, days=30)
    clock.advance(hours=120)
    sweep(ctx, clock.now())
    assert store.read_bytes(renewed) == b"renewed on review"


def test_ported_expiry_is_fixed_at_creation_and_survives_a_restart(
    store: ArtifactService, action: str, ctx: Context, clock: ManualClock
) -> None:
    handle = keep_text(store, action, "fixed")
    before = ctx.db.execute(
        "SELECT expires_at FROM artifacts WHERE handle = ?", (handle,)
    ).fetchone()
    store.read_bytes(handle)
    store.info(handle)
    clock.advance(hours=240)
    fresh = ArtifactService(ctx.db, ctx.clock, ctx.data_root)  # a restarted process
    fresh.read_bytes(handle)
    after = ctx.db.execute(
        "SELECT expires_at FROM artifacts WHERE handle = ?", (handle,)
    ).fetchone()
    assert after["expires_at"] == before["expires_at"]
    clock.advance(hours=504)
    sweep(ctx, clock.now())
    with pytest.raises(ChamberlainError):
        fresh.read_bytes(handle)


def test_m27_global_and_per_card_budgets_refuse_and_report(
    ctx: Context, action: str, clock: ManualClock
) -> None:
    small = ArtifactService(
        ctx.db, ctx.clock, ctx.data_root, Budgets(per_card_bytes=10, total_bytes=14)
    )
    keep_text(small, action, "123456")
    with pytest.raises(ChamberlainError) as per_card:
        keep_text(small, action, "abcdef")  # 12 > 10 for this card
    assert per_card.value.code is ErrorCode.STORAGE_OVER_BUDGET
    assert "art_" in per_card.value.fix  # names the largest releasable handle
    roomy = ArtifactService(
        ctx.db, ctx.clock, ctx.data_root, Budgets(per_card_bytes=100, total_bytes=10)
    )
    with pytest.raises(ChamberlainError) as total:
        keep_text(roomy, action, "zzzzzz")
    assert total.value.code is ErrorCode.STORAGE_OVER_BUDGET
    report = sweep(ctx, clock.now(), budgets=Budgets(per_card_bytes=100, total_bytes=5))
    assert report.over_budget
    state = ctx.db.execute(
        "SELECT value FROM system_state WHERE key = 'memory.over_budget'"
    ).fetchone()
    assert state["value"].startswith("total:")
    clean = sweep(ctx, clock.now())
    assert not clean.over_budget
    assert os.path.exists(ctx.data_root)


def test_m7_keeping_identical_content_repairs_a_damaged_blob(
    store: ArtifactService, action: str, ctx: Context
) -> None:
    first = keep_text(store, action, "same content")
    path = blob_path(ctx.data_root, sha256_bytes(b"same content"))
    path.write_bytes(b"damaged")
    second = keep_text(store, action, "same content")
    assert store.read_bytes(second) == b"same content"
    assert path.read_bytes() == b"same content"
    assert first != second
