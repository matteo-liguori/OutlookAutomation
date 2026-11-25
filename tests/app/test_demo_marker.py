"""Replies carry the demo marker in coverage while demo sources exist (E-3)."""

from pathlib import Path

from chamberlain.app.context import SourceRouter
from chamberlain.app.wiring import build_app
from chamberlain.connectors.fake import FakeProvider
from chamberlain.ports.clock import ManualClock


def _find(
    data_root: Path, clock: ManualClock, fake: FakeProvider, demo: frozenset[str]
) -> dict[str, object]:
    sources = SourceRouter(mail={"mail:test": fake.mail}, demo=demo)
    app = build_app(data_root, clock=clock, sources=sources)
    try:
        reply = app.call("guide", {})
    finally:
        app.close()
    assert reply.coverage is not None
    return reply.coverage.model_dump(mode="json", exclude_none=True)


def test_e3_coverage_is_marked_demo_when_demo_sources_exist_and_1_related_checks(
    data_root: Path, clock: ManualClock, fake: FakeProvider
) -> None:
    # e3_coverage_is_marked_demo_when_demo_sources_exist
    assert _find(data_root, clock, fake, frozenset({"mail:test"}))["demo"] is True

    # e3_coverage_has_no_demo_key_otherwise
    assert "demo" not in _find(data_root, clock, fake, frozenset())
