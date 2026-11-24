"""The real readers run over imported rows: the owner's viewer path, `open`, doctor, the sweep.

Raw-row checks cannot catch a reader that chokes on a NULL scope or an operation with no
intent, so these call the tracks' own code on what the importer wrote.
"""

import io
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from old_fixture import MemorySecrets

from chamberlain.app.core import App
from chamberlain.app.wiring import COMPACTORS
from chamberlain.connections import open_app
from chamberlain.importer.report import ImportReport
from chamberlain.importer.run import run_import
from chamberlain.memory.sweeper import sweep
from chamberlain.ports.clock import ManualClock
from chamberlain.surface import cli
from chamberlain.writes.confirm import grant_detail, list_grants
from chamberlain.writes.maintenance import maintain


@pytest.fixture
def app(data_root: Path, imported: ImportReport, secrets: MemorySecrets, clock: ManualClock) -> App:
    return open_app(data_root, clock=clock, secrets=secrets)


def handles(app: App, sql: str) -> list[str]:
    return [r[0] for r in app.context.db.execute(sql)]


def test_g_the_viewer_lists_and_describes_every_imported_grant(app: App) -> None:
    try:
        listed = list_grants(app.context)
        assert len(listed) == 7
        for handle in handles(app, "SELECT handle FROM grants"):
            detail = grant_detail(app.context, handle)
            assert detail["handle"] == handle
    finally:
        app.close()


def test_t_open_reads_every_kind_of_imported_record(app: App) -> None:
    try:
        started = app.call("start", {"agent": "test"})
        session: str = started.data.session  # type: ignore[union-attr]
        picks = [
            "SELECT handle FROM cards WHERE over_cap = 1",
            "SELECT handle FROM cards WHERE state = 'closed'",
            "SELECT handle FROM cards WHERE state = 'open' AND over_cap = 0",
            "SELECT handle FROM actions LIMIT 1",
            "SELECT handle FROM grants WHERE kind = 'standing' AND state = 'suspended'",
            "SELECT handle FROM grants WHERE state = 'spent'",
            "SELECT handle FROM operations WHERE state = 'uncertain' LIMIT 1",
            "SELECT handle FROM operations WHERE state = 'confirmed' LIMIT 1",
            "SELECT handle FROM evidence LIMIT 1",
        ]
        for sql in picks:
            for handle in handles(app, sql)[:1]:
                reply = app.call("open", {"handle": handle, "session": session})
                error = reply.error.code.value if reply.error else ""
                assert not error.startswith("internal."), (sql, error)
                assert reply.ok, (sql, error)
    finally:
        app.close()


def test_m_doctor_with_metrics_runs_over_imported_data(app: App) -> None:
    try:
        reply = app.call("doctor", {"metrics": True})
        assert reply.ok
        assert app.call("doctor", {"check": "schema"}).ok
    finally:
        app.close()


def test_w_the_sweep_and_write_maintenance_keep_what_the_owner_still_needs(
    app: App, clock: ManualClock
) -> None:
    try:
        db = app.context.db
        before_ops = db.execute(
            "SELECT handle, state FROM operations WHERE state = 'uncertain' ORDER BY handle"
        ).fetchall()
        clock.advance(hours=48)
        sweep(app.context, clock.now(), compactors=COMPACTORS)
        maintain(app.context)
        after_ops = db.execute(
            "SELECT handle, state FROM operations WHERE state = 'uncertain' ORDER BY handle"
        ).fetchall()
        assert [tuple(r) for r in after_ops] == [tuple(r) for r in before_ops]
        assert len(before_ops) == 5
        wording = db.execute(
            "SELECT count(*) FROM evidence e JOIN evidence_refs r ON r.evidence = e.handle "
            "JOIN grants g ON g.handle = r.owner WHERE g.kind = 'standing' "
            "AND g.state = 'suspended' AND e.text LIKE 'Old standing permission%'"
        ).fetchone()[0]
        assert wording == 2  # the dated and the undated one; the expired one is terminal
        on_cards = db.execute(
            "SELECT count(*) FROM evidence_refs WHERE owner_kind = 'card'"
        ).fetchone()[0]
        assert on_cards >= 15
        assert db.execute("SELECT count(*) FROM cards WHERE state = 'open'").fetchone()[0] == 2
    finally:
        app.close()


def test_s14_two_old_ids_that_normalise_alike_are_refused_not_merged(
    tmp_path: Path, secrets: MemorySecrets, clock: ManualClock
) -> None:
    from old_fixture import build_old_root

    from chamberlain.schema.errors import ChamberlainError

    old = build_old_root(tmp_path / "clash")
    profile_path = old / "monitor" / "profile.json"
    profile: dict[str, Any] = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["mail"]["mail:work_x"] = "second@example.test"
    profile["mail"]["mail:work-x"] = "third@example.test"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    with pytest.raises(ChamberlainError) as raised:
        run_import(old, tmp_path / "new", clock=clock, secrets=secrets)
    assert "mail:work-x" in raised.value.message


def test_s14_a_broken_old_record_is_a_named_config_error_and_rolls_back(
    tmp_path: Path, secrets: MemorySecrets, clock: ManualClock
) -> None:
    from old_fixture import build_old_root

    from chamberlain.schema.errors import ChamberlainError

    old = build_old_root(tmp_path / "broken")
    db = sqlite3.connect(old / "monitor" / "monitor.db")
    db.execute("UPDATE cases SET details = 'not json' WHERE id = 'case-big'")
    db.commit()
    db.close()
    with pytest.raises(ChamberlainError) as raised:
        run_import(old, tmp_path / "new", clock=clock, secrets=secrets)
    assert "case case-big" in raised.value.message
    assert not (tmp_path / "new" / "chamberlain.sqlite3").exists()


def test_s14_a_failure_while_writing_names_the_old_record_and_rolls_back(
    tmp_path: Path, secrets: MemorySecrets, clock: ManualClock
) -> None:
    from old_fixture import build_old_root

    from chamberlain.schema.errors import ChamberlainError

    old = build_old_root(tmp_path / "broken-write")
    db = sqlite3.connect(old / "monitor" / "monitor.db")
    details = json.dumps({"id": "oa-bad", "status": "approved", "granted_at": 5})
    db.execute("INSERT INTO approvals VALUES('oa-bad', NULL, ?)", (details,))
    db.execute("INSERT INTO meta VALUES('thunderbird:send:oa-bad', 'not json at all')")
    db.commit()
    db.close()
    first = run_import(old, tmp_path / "new", clock=clock, secrets=secrets)
    assert first.changed  # odd fields still import, as inert placeholders
    broken = json.loads((old / "monitor" / "profile.json").read_text(encoding="utf-8"))
    broken["mail"]["mail:work"] = "not-an-address"
    (old / "monitor" / "profile.json").write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(ChamberlainError) as raised:
        run_import(old, tmp_path / "second", clock=clock, secrets=secrets)
    assert "source mail:work" in raised.value.message
    assert not (tmp_path / "second" / "chamberlain.sqlite3").exists()


def test_s14_the_cli_imports_and_a_second_run_reports_nothing_new(
    old_root: Path,
    data_root: Path,
    secrets: MemorySecrets,
    clock: ManualClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("chamberlain.importer.run.default_store", lambda _root: secrets)
    outputs: list[dict[str, Any]] = []
    for _ in range(2):
        out = io.StringIO()
        code = cli.main(["--data", str(data_root), "import", str(old_root)], out)
        assert code == cli.EXIT_OK
        outputs.append(json.loads(out.getvalue()))
    assert outputs[0]["counts"]["cards"] == 3
    assert outputs[1]["counts"] == {}
    assert "refresh-token-value" not in json.dumps(outputs)


def test_s14_the_cli_reports_a_bad_old_root_as_an_error_reply(
    tmp_path: Path, data_root: Path
) -> None:
    out = io.StringIO()
    code = cli.main(["--data", str(data_root), "import", str(tmp_path / "nothing")], out)
    assert code == cli.EXIT_FAILED
    assert json.loads(out.getvalue())["error"]["code"] == "config.invalid"
