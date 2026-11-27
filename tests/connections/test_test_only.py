"""`test_only` sources and live-test-marked events stay out of daily operation.

Contract: docs/live-test.md (the dedicated test mailbox and calendar); docs/configuration.md.
"""

import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from conn_support import MemorySecrets, full_profile, write_profile

from chamberlain.app.context import Context, SourceRouter
from chamberlain.attention.collect import collect
from chamberlain.connections import load_connections
from chamberlain.connections.build import build_runtime
from chamberlain.connections.model import Connections
from chamberlain.connections.repo import sync_sources
from chamberlain.connectors.fake import FakeProvider
from chamberlain.domain.calendar import EventFields
from chamberlain.domain.testmark import MARKER
from chamberlain.memory.find import find
from chamberlain.ports.clock import ManualClock
from chamberlain.runs import report_repo
from chamberlain.schema.tools import FindRequest, FindResponse
from chamberlain.store.migrate import stamp
from chamberlain.surface.viewer import pages

MARKED = f"{MARKER} 20261127T100000-ab12 dinner"


@pytest.fixture
def world(ctx: Context, fake: FakeProvider, clock: ManualClock) -> Context:
    """`test` sources are daily; `lab` sources are `test_only`."""
    fake.mail.add_source("mail:lab")
    fake.calendar.add_source("calendar:lab")
    sources = SourceRouter(
        mail={"mail:test": fake.mail, "mail:lab": fake.mail},
        calendar={"calendar:test": fake.calendar, "calendar:lab": fake.calendar},
        test_only=frozenset({"mail:lab", "calendar:lab"}),
        hide_test_marked=True,
    )
    day = clock.now() + timedelta(days=1)

    def fields(title: str, hour: int) -> EventFields:
        start = day.replace(hour=hour, minute=0, second=0, microsecond=0)
        return EventFields(title=title, start=start, end=start + timedelta(hours=1))

    fake.calendar.add_event("calendar:test", fields("Standup", 9))
    fake.calendar.add_event("calendar:test", fields(MARKED, 11))
    fake.calendar.add_event("calendar:lab", fields("Lab thing", 13))
    return replace(ctx, sources=sources)


def test_collect_skips_test_only_sources(world: Context) -> None:
    data = collect(world).data
    assert {s.id for s in data.sources} == {"mail:test", "calendar:test"}
    assert [s.title for s in data.calendar_slots] == ["Standup"]


def test_the_live_tests_app_sees_test_sources_and_marked_events(world: Context) -> None:
    live = replace(
        world, sources=replace(world.sources, test_only=frozenset(), hide_test_marked=False)
    )
    data = collect(live).data
    assert {s.id for s in data.sources} >= {"mail:lab", "calendar:lab"}
    assert sorted(s.title for s in data.calendar_slots) == sorted(["Standup", MARKED, "Lab thing"])


def test_report_facts_leave_out_test_sources(world: Context, db: sqlite3.Connection) -> None:
    collect(world)  # writes health rows
    connections = load_connections_with(world)
    sync_sources(db, connections, stamp(world.clock))
    every = {s.id for s in report_repo.source_facts(db)}
    assert {"mail:lab", "calendar:lab"} <= every
    daily = {s.id for s in report_repo.source_facts(db, world.sources.test_only)}
    assert daily == every - {"mail:lab", "calendar:lab"}


def load_connections_with(world: Context) -> Connections:
    return Connections.model_validate(
        {
            "sources": {
                "mail:test": _mail(),
                "mail:lab": _mail() | {"test_only": True},
                "calendar:test": _cal(),
                "calendar:lab": _cal() | {"test_only": True},
            }
        }
    )


def _mail() -> dict[str, object]:
    return {
        "kind": "thunderbird",
        "account_id": "a",
        "mailbox": "me@example.test",
        "inbox_folder_id": "i",
        "sent_folder_id": "s",
        "draft_folder_id": "d",
        "trash_folder_id": "t",
    }


def _cal() -> dict[str, object]:
    return {"kind": "google", "account": "me@gmail.example", "calendar_id": "primary"}


def test_viewer_calendar_hides_test_sources_and_marked_events(world: Context) -> None:
    view = pages.calendar(world, "UTC", None, None)
    assert [s["id"] for s in view["sources"]] == ["calendar:test"]
    assert [e["title"] for e in view["events"]] == ["Standup"]
    live = replace(
        world, sources=replace(world.sources, test_only=frozenset(), hide_test_marked=False)
    )
    shown = pages.calendar(live, "UTC", None, None)
    assert {e["title"] for e in shown["events"]} == {"Standup", MARKED[:200], "Lab thing"}


def test_find_defaults_to_daily_sources_but_names_a_test_source(world: Context) -> None:
    def hits(**request: object) -> int:
        reply = find(world, FindRequest(scope="calendar", **request))  # type: ignore[arg-type]
        assert isinstance(reply.data, FindResponse)
        return len(reply.data.hits)

    assert hits() == 2  # Standup and the marked one: find is not a daily view
    assert hits(source="calendar:lab") == 1


def test_open_runtime_marks_test_only_sources(data_root: Path, clock: ManualClock) -> None:
    document = full_profile()
    document["sources"]["calendar:primary"]["test_only"] = True
    write_profile(data_root, document)
    connections = load_connections(data_root)
    secrets = MemorySecrets()
    daily = build_runtime(connections, data_root, clock, secrets=secrets).sources
    assert daily.test_only == frozenset({"calendar:primary"})
    assert daily.hide_test_marked
    live = build_runtime(connections, data_root, clock, secrets=secrets, include_test=True).sources
    assert live.test_only == frozenset()
    assert not live.hide_test_marked
