"""The wiring: real services, the tick's steps, `doctor --metrics` and the CLI faces."""

import io
import json
import re
from pathlib import Path

from tests.integration.conftest import Stack, build_stack

from chamberlain.app.doubles import RecordingAuditLog
from chamberlain.attention import tick as attention_tick
from chamberlain.limits import MAX_REPLY_CHARS
from chamberlain.memory.artifacts import ArtifactService
from chamberlain.memory.evidence import EvidenceService
from chamberlain.memory.refs import RefService
from chamberlain.schema.tools import Metrics
from chamberlain.store.schema import SYSTEM_KEYS
from chamberlain.surface.cli import main
from chamberlain.writes.audit import AuditChain, verify

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "chamberlain"


def test_the_wired_services_are_the_real_ones_not_the_doubles(stack: Stack) -> None:
    services = stack.app.context.services
    assert isinstance(services.audit, AuditChain)
    assert not isinstance(services.audit, RecordingAuditLog)
    assert isinstance(services.artifacts, ArtifactService)
    assert isinstance(services.evidence, EvidenceService)
    assert isinstance(services.refs, RefService)


def test_building_a_second_app_replaces_the_tick_steps_rather_than_stacking_them(
    tmp_path: Path,
) -> None:
    first = build_stack(tmp_path / "one")
    second = build_stack(tmp_path / "two")
    try:
        assert len(attention_tick.ALWAYS_STEPS) == 1
        assert len(attention_tick.EXTRA_STEPS) == 2
    finally:
        first.app.close()
        second.app.close()


def test_the_tick_recovers_operations_then_sweeps_and_maintains(stack: Stack) -> None:
    stack.start()
    names = [getattr(step, "__name__", "") for step in attention_tick.EXTRA_STEPS]
    assert names == ["sweep_everything", "maintain_writes"]
    assert attention_tick.ALWAYS_STEPS[0].__name__ == "recover_operations"
    stack.clock.advance(minutes=2)
    stack.ok("doctor")
    row = stack.app.context.db.execute(
        "SELECT value FROM system_state WHERE key = 'memory.last_sweep_at'"
    ).fetchone()
    assert row is not None


def test_t1_doctor_metrics_has_every_key_of_the_targets_contract(stack: Stack) -> None:
    text = (REPO / "docs" / "contracts" / "10-targets.md").read_text(encoding="utf-8")
    section = text.split("## `doctor --metrics` output (fixed keys)", 1)[1]
    documented = set(re.findall(r"`([a-z_0-9]+)`", section))
    assert documented == set(Metrics.model_fields)
    metrics = stack.ok("doctor", metrics=True)["metrics"]
    assert set(metrics) == documented


def test_the_largest_reply_is_measured_from_the_audit_log(stack: Stack) -> None:
    stack.start()
    stack.ok("doctor")
    metrics = stack.ok("doctor", metrics=True)["metrics"]
    assert 0 < metrics["largest_reply_chars_7d"] <= MAX_REPLY_CHARS
    assert metrics["guidance_total_chars"] > 0


def test_every_system_state_key_in_the_source_is_declared(stack: Stack) -> None:
    pattern = re.compile(r'"((?:attention|memory|writes|app)\.[a-z_]+)"')
    used: set[str] = set()
    for path in SRC.rglob("*.py"):
        used.update(pattern.findall(path.read_text(encoding="utf-8")))
    assert used <= SYSTEM_KEYS


def test_the_cli_tick_and_doctor_metrics_run_against_a_data_root(tmp_path: Path) -> None:
    root = str(tmp_path / "data")
    out = io.StringIO()
    assert main(["--data", root, "tick"], out) == 0
    assert json.loads(out.getvalue()) == {"ran": True, "wake": False}
    out = io.StringIO()
    assert main(["--data", root, "doctor", "--metrics"], out) == 0
    figures = json.loads(out.getvalue())["data"]["metrics"]
    assert "grants_awaiting_human" in figures


def test_the_audit_chain_verifies_after_wired_use(stack: Stack) -> None:
    stack.start()
    stack.ok("doctor")
    assert verify(stack.app.context.db).ok
