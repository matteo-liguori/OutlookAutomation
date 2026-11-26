"""Connector-local freshness, fencing and crash recovery without live accounts."""

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import cast

from pydantic import JsonValue

from chamberlain.app.context import SourceRouter
from chamberlain.app.core import App
from chamberlain.connections.owl_freshness import OwlFreshnessStore
from chamberlain.connectors.thunderbird import protocol as wire
from chamberlain.connectors.thunderbird.calendar import (
    OwlBinding,
    OwlCalendarConfig,
    ThunderbirdCalendar,
)
from chamberlain.connectors.thunderbird.client import BridgeClient, Reply
from chamberlain.domain.capabilities import HealthState
from chamberlain.livetest.catalogue import build_plan
from chamberlain.livetest.config import LiveCalendar, LiveTestConfig
from chamberlain.livetest.env import Environment
from chamberlain.livetest.runner import RunOptions, execute
from chamberlain.ports.clock import ManualClock
from chamberlain.ports.health import passive_health_checks
from chamberlain.schema.base import Model
from chamberlain.store.db import DB_NAME, connect
from chamberlain.store.migrate import open_store

SOURCE = "calendar:owl"


class Bridge(BridgeClient):
    def __init__(self) -> None:
        self.entry: dict[str, JsonValue] = {
            "id": "cal",
            "type": "owl",
            "name": "Test calendar",
            "disabled": False,
            "read_only": False,
        }

    def hello(self) -> wire.Welcome:
        return wire.Welcome(
            v=3,
            id="calendar_test",
            host_version="test",
            addon_version="test",
            calendar_available=True,
        )

    def call(
        self,
        cmd: wire.Command,
        args: Model,
        *,
        payloads: Mapping[str, bytes] | None = None,
        calendar: bool = False,
    ) -> Reply:
        assert calendar
        assert cmd is wire.Command.CALENDAR_LIST
        return Reply(wire.CalendarListResult.model_validate({"calendars": [self.entry]}))


class Port(ThunderbirdCalendar):
    def __init__(self, root: Path, clock: ManualClock):
        self.fake_bridge = Bridge()
        super().__init__(
            OwlCalendarConfig(bindings={SOURCE: OwlBinding(calendar_id="cal")}),
            self.fake_bridge,
            clock,
            freshness_store=OwlFreshnessStore(root / DB_NAME, clock),
        )
        self.calls = []
        self.result = True
        self.enter: Event | None = None
        self.release: Event | None = None

    def probe(self, source, token, *, cleanup_only=False):
        self.calls.append((source, token, cleanup_only))
        if self.enter:
            self.enter.set()
            assert self.release is not None
            self.release.wait(10)
        return self.result


def setup_port(root: Path):
    clock = ManualClock()
    db = open_store(root, clock)
    db.close()
    return Port(root, clock), clock


def test_success_is_reused_for_ten_minutes_and_then_checked_again(tmp_path: Path) -> None:
    port, clock = setup_port(tmp_path)
    first = port.health(SOURCE)
    assert first.state is HealthState.OK
    assert first.fresh_as_of == clock.now().replace(microsecond=0)
    token = port.calls[0][1]
    assert len(token) == 32
    assert int(token[:8], 16) == int(clock.now().timestamp())
    assert port.health(SOURCE).state is HealthState.OK
    assert len(port.calls) == 1
    clock.advance(minutes=10)
    assert port.health(SOURCE).state is HealthState.OK
    assert len(port.calls) == 2
    assert port.calls[0][1] != port.calls[1][1]


def test_passive_planning_never_probes_and_reuses_only_existing_freshness(tmp_path: Path) -> None:
    port, _ = setup_port(tmp_path)
    config = LiveTestConfig(enabled=True, calendars={SOURCE: LiveCalendar()})
    with passive_health_checks():
        plan = build_plan(
            config,
            SourceRouter(calendar={SOURCE: port}),
            only=("calendar.create_update_delete",),
        )
    assert plan[0].skip == f"{SOURCE} is stale"
    assert not port.calls
    assert not (tmp_path / "run" / "owl-freshness").exists()
    assert port.health(SOURCE).state is HealthState.OK
    with passive_health_checks():
        plan = build_plan(
            config,
            SourceRouter(calendar={SOURCE: port}),
            only=("calendar.create_update_delete",),
        )
    assert not plan[0].skip
    assert len(port.calls) == 1


def test_dry_run_uses_passive_health_even_when_calendar_is_selected(tmp_path: Path) -> None:
    port, clock = setup_port(tmp_path)
    sources = SourceRouter(calendar={SOURCE: port})
    app = cast(App, SimpleNamespace(context=SimpleNamespace(sources=sources)))
    env = cast(Environment, SimpleNamespace(clock=clock))
    config = LiveTestConfig(enabled=True, calendars={SOURCE: LiveCalendar()})
    result = execute(
        app, env, config, RunOptions(dry_run=True, only=("calendar.create_update_delete",))
    )
    assert result.results[0].reason == f"{SOURCE} is stale"
    assert not port.calls
    assert not (tmp_path / "run" / "owl-freshness").exists()


def test_unselected_or_explicitly_skipped_sources_do_not_trigger_probe(tmp_path: Path) -> None:
    port, _ = setup_port(tmp_path)
    sources = SourceRouter(calendar={SOURCE: port})
    config = LiveTestConfig(enabled=True, calendars={SOURCE: LiveCalendar()})
    plan = build_plan(config, sources, only=("harness.doctor_clean",))
    assert len(plan) == 1
    assert not port.calls
    plan = build_plan(config, sources, only=("calendar.create_update_delete",), skip=("calendar",))
    assert plan[0].skip == f"{SOURCE} is stale"
    assert not port.calls


def test_failed_or_crashed_attempt_reuses_token_for_cleanup_before_new_create(
    tmp_path: Path,
) -> None:
    port, clock = setup_port(tmp_path)
    port.result = False
    assert port.health(SOURCE).state is HealthState.STALE
    token = port.calls[0][1]
    assert port.health(SOURCE).state is HealthState.STALE
    assert len(port.calls) == 1
    clock.advance(minutes=11)
    port.result = True
    assert port.health(SOURCE).state is HealthState.STALE
    assert port.calls[1] == (SOURCE, token, True)
    assert port.health(SOURCE).state is HealthState.OK
    assert port.calls[2][2] is False
    assert port.calls[2][1] != token


def test_pending_token_is_not_cleaned_in_a_changed_binding(tmp_path: Path) -> None:
    port, clock = setup_port(tmp_path)
    port.result = False
    assert port.health(SOURCE).state is HealthState.STALE
    db = connect(tmp_path / DB_NAME)
    db.execute("UPDATE owl_freshness SET calendar_id='old' WHERE source_id=?", (SOURCE,))
    db.close()
    clock.advance(minutes=11)
    port.result = True
    assert port.health(SOURCE).state is HealthState.STALE
    assert len(port.calls) == 1


def test_read_only_and_disabled_calendars_never_run_the_probe(tmp_path: Path) -> None:
    port, _ = setup_port(tmp_path)
    port.fake_bridge.entry["read_only"] = True
    assert port.health(SOURCE).state is HealthState.STALE
    assert not port.calls
    port.fake_bridge.entry["disabled"] = True
    assert port.health(SOURCE).state is HealthState.UNAVAILABLE
    assert not port.calls


def test_running_owner_is_fenced_even_if_lease_expires(tmp_path: Path) -> None:
    port, clock = setup_port(tmp_path)
    port.enter, port.release = Event(), Event()
    assert port.enter is not None
    assert port.release is not None
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(port.health, SOURCE)
        assert port.enter.wait(5)
        clock.advance(minutes=11)
        second = pool.submit(port.health, SOURCE)
        assert second.result(timeout=5).state is HealthState.STALE
        assert len(port.calls) == 1
        port.release.set()
        assert first.result(timeout=5).state is HealthState.OK
