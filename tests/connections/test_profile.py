"""The connection profile: shape, validation, secrets never stored, router building."""

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from conn_support import MemorySecrets, full_profile, write_profile

from chamberlain.connections import (
    load_connections,
    open_app,
    parse_connections,
    render_redacted,
    save_connections,
)
from chamberlain.connections.build import build_router, build_runtime
from chamberlain.connections.load import dump_connections, profile_path
from chamberlain.domain.grants import Requirement
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.errors import ChamberlainError, ErrorCode


def test_s16_a_missing_profile_is_an_empty_one(data_root: Path) -> None:
    assert load_connections(data_root).sources == {}


def test_s16_a_full_profile_builds_every_connector_kind(
    data_root: Path, secrets: MemorySecrets, clock: ManualClock
) -> None:
    write_profile(data_root, full_profile())
    connections = load_connections(data_root)
    runtime = build_runtime(connections, data_root, clock, secrets=secrets)
    assert sorted(runtime.sources.mail) == ["mail:work"]
    assert sorted(runtime.sources.contacts) == ["contacts:work"]
    assert sorted(runtime.sources.calendar) == [
        "calendar:icloud",
        "calendar:primary",
        "calendar:work",
    ]
    assert runtime.policy.send_known is Requirement.HUMAN_CONFIRMATION
    assert runtime.policy.sends_per_hour == 10
    assert runtime.policy.zone == "Europe/London"


def test_s16_the_caldav_register_output_is_a_valid_source(data_root: Path) -> None:
    """`caldav.setup register` writes exactly these four keys; they must load."""
    write_profile(
        data_root,
        {
            "version": 1,
            "sources": {
                "calendar:icloud": {
                    "kind": "caldav",
                    "url": "https://caldav.example.test/u/cal/",
                    "username": "me@icloud.example",
                    "credential": "password:me@icloud.example",
                }
            },
        },
    )
    assert "calendar:icloud" in load_connections(data_root).sources


def test_s17_no_field_can_hold_a_secret(data_root: Path) -> None:
    document = full_profile()
    document["sources"]["calendar:icloud"]["password"] = "hunter2-hunter2"
    write_profile(data_root, document)
    with pytest.raises(ChamberlainError) as raised:
        load_connections(data_root)
    assert raised.value.code is ErrorCode.CONFIG_INVALID
    assert raised.value.field is not None
    assert raised.value.field.endswith("password")
    assert "hunter2" not in raised.value.message + raised.value.detail


Mutation = Callable[[dict[str, Any]], object]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(version=2),
        lambda d: d.update(timezone="Mars/Olympus"),
        lambda d: d["sources"].update({"mail:bad": {"kind": "caldav"}}),
        lambda d: d["sources"]["mail:work"].pop("inbox_folder_id"),
        lambda d: d["sources"]["mail:work"].update(mailbox="not-an-address"),
        lambda d: d["write_policy"].update(sends_per_hour=500),
        lambda d: d["sources"].update({"mail:Upper": d["sources"]["mail:work"]}),
        lambda d: d["sources"].update({"calendar:oops": d["sources"]["mail:work"]}),
    ],
)
def test_s16_a_malformed_profile_is_config_invalid_not_a_traceback(
    data_root: Path, mutate: Mutation
) -> None:
    document = copy.deepcopy(full_profile())
    mutate(document)
    write_profile(data_root, document)
    with pytest.raises(ChamberlainError) as raised:
        load_connections(data_root)
    assert raised.value.code is ErrorCode.CONFIG_INVALID


def test_s16_a_yaml_syntax_error_is_config_invalid(data_root: Path) -> None:
    path = profile_path(data_root)
    path.parent.mkdir(parents=True)
    path.write_text("sources: [unclosed\n", encoding="utf-8")
    with pytest.raises(ChamberlainError) as raised:
        load_connections(data_root)
    assert raised.value.code is ErrorCode.CONFIG_INVALID


def test_g12_the_policy_cannot_loosen_a_mandatory_row() -> None:
    connections = parse_connections({"write_policy": {"other": "none"}})
    with pytest.raises(ChamberlainError) as raised:
        build_runtime(connections, Path("unused"), ManualClock())
    assert raised.value.code is ErrorCode.CONFIG_INVALID


def test_s16_disabled_sources_get_no_connector(
    data_root: Path, secrets: MemorySecrets, clock: ManualClock
) -> None:
    document = full_profile()
    document["sources"]["calendar:icloud"]["enabled"] = False
    write_profile(data_root, document)
    router = build_router(load_connections(data_root), data_root, clock, secrets=secrets)
    assert "calendar:icloud" not in router.calendar


def test_s17_an_insecure_caldav_url_is_refused_when_building(
    data_root: Path, secrets: MemorySecrets, clock: ManualClock
) -> None:
    document = full_profile()
    document["sources"]["calendar:icloud"]["url"] = "http://caldav.example.test/cal/"
    connections = parse_connections(document)
    with pytest.raises(ChamberlainError):
        build_router(connections, data_root, clock, secrets=secrets)


def test_s16_save_and_load_round_trip_and_second_save_is_a_no_op(data_root: Path) -> None:
    connections = parse_connections(full_profile())
    assert save_connections(data_root, connections) is True
    assert save_connections(data_root, connections) is False
    assert load_connections(data_root) == connections
    assert yaml.safe_load(dump_connections(connections))["version"] == 1


def test_s17_show_has_no_secret_values(secrets: MemorySecrets) -> None:
    secrets.set("chamberlain.caldav", "password:me@icloud.example", "app-password-value")
    shown = str(render_redacted(parse_connections(full_profile())))
    assert "app-password-value" not in shown
    assert "password:me@icloud.example" in shown  # the reference, not the secret


def test_s16_open_app_with_no_profile_runs_with_no_sources(
    data_root: Path, clock: ManualClock
) -> None:
    app = open_app(data_root, clock=clock)
    try:
        assert app.call("doctor", {}).ok
    finally:
        app.close()


def test_s16_open_app_fills_the_sources_table_once(
    data_root: Path, secrets: MemorySecrets, clock: ManualClock
) -> None:
    write_profile(data_root, full_profile())
    app = open_app(data_root, clock=clock, secrets=secrets)
    try:
        rows = app.context.db.execute("SELECT id, kind, state FROM sources ORDER BY id").fetchall()
        assert [(r["id"], r["kind"], r["state"]) for r in rows] == [
            ("calendar:icloud", "calendar", "enabled"),
            ("calendar:primary", "calendar", "enabled"),
            ("calendar:work", "calendar", "enabled"),
            ("contacts:work", "contacts", "enabled"),
            ("mail:work", "mail", "enabled"),
        ]
        binding = app.context.db.execute(
            "SELECT binding_json FROM sources WHERE id = 'calendar:icloud'"
        ).fetchone()[0]
        assert "password:me@icloud.example" in binding
    finally:
        app.close()
    clock.advance(minutes=5)
    again = open_app(data_root, clock=clock, secrets=secrets)
    try:
        moved = again.context.db.execute(
            "SELECT count(*) FROM sources WHERE updated_at <> created_at"
        ).fetchone()[0]
        assert moved == 0
    finally:
        again.close()


def test_s16_a_source_removed_from_the_profile_is_disabled_not_deleted(
    data_root: Path, secrets: MemorySecrets, clock: ManualClock
) -> None:
    write_profile(data_root, full_profile())
    open_app(data_root, clock=clock, secrets=secrets).close()
    document = full_profile()
    del document["sources"]["calendar:icloud"]
    write_profile(data_root, document)
    app = open_app(data_root, clock=clock, secrets=secrets)
    try:
        state = app.context.db.execute(
            "SELECT state FROM sources WHERE id = 'calendar:icloud'"
        ).fetchone()[0]
        assert state == "disabled"
    finally:
        app.close()


def test_s16_a_malformed_profile_stops_open_app_before_the_store_is_created(
    data_root: Path, clock: ManualClock
) -> None:
    write_profile(data_root, {"version": 9})
    with pytest.raises(ChamberlainError):
        open_app(data_root, clock=clock)
    assert not (data_root / "chamberlain.sqlite3").exists()


def test_s16_an_owl_source_is_unavailable_not_a_crash(
    data_root: Path, secrets: MemorySecrets, clock: ManualClock
) -> None:
    owl = full_profile()["sources"]["calendar:work"]
    write_profile(data_root, {"version": 1, "sources": {"calendar:work": owl}})
    app = open_app(data_root, clock=clock, secrets=secrets)
    try:
        health = app.context.sources.calendar_port("calendar:work").health("calendar:work")
        assert health.state.value == "unavailable"
    finally:
        app.close()
