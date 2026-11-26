"""The wiring: real services, the tick's steps, `doctor --metrics` and the CLI faces."""

import io
import json
import re
from pathlib import Path

from tests.integration.conftest import Stack, build_stack

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


def test_the_app_binds_the_audit_and_memory_services(stack: Stack) -> None:
    services = stack.app.context.services
    assert isinstance(services.audit, AuditChain)
    assert isinstance(services.artifacts, ArtifactService)
    assert isinstance(services.evidence, EvidenceService)
    assert isinstance(services.refs, RefService)


def test_each_app_carries_its_own_tick_steps(tmp_path: Path) -> None:
    first = build_stack(tmp_path / "one")
    second = build_stack(tmp_path / "two")
    try:
        one, two = first.app.context.tick_steps, second.app.context.tick_steps
        assert len(one.always) == len(two.always) == 2
        assert len(one.extra) == len(two.extra) == 2
        assert one.extra[0] is not two.extra[0]
    finally:
        first.app.close()
        second.app.close()


def test_the_tick_recovers_operations_then_sweeps_and_maintains(stack: Stack) -> None:
    stack.start()
    steps = stack.app.context.tick_steps
    names = [getattr(step, "__name__", "") for step in steps.extra]
    assert names == ["sweep_everything", "maintain_writes"]
    always = [step.__name__ for step in steps.always]
    assert always == ["recover_operations", "plan_runs"]
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


def test_the_largest_reply_is_measured_from_the_audit_log_and_3_related_checks(
    stack: Stack, tmp_path: Path
) -> None:
    # the_largest_reply_is_measured_from_the_audit_log
    stack.start()
    stack.ok("doctor")
    metrics = stack.ok("doctor", metrics=True)["metrics"]
    assert 0 < metrics["largest_reply_chars_7d"] <= MAX_REPLY_CHARS
    assert metrics["guidance_total_chars"] > 0

    # every_system_state_key_in_the_source_is_declared
    pattern = re.compile(r'"((?:attention|memory|writes|app)\.[a-z_]+)"')
    used: set[str] = set()
    for path in SRC.rglob("*.py"):
        used.update(pattern.findall(path.read_text(encoding="utf-8")))
    assert used <= SYSTEM_KEYS

    # the_cli_tick_and_doctor_metrics_run_against_a_data_root
    root = str(tmp_path / "data")
    out = io.StringIO()
    assert main(["--data", root, "tick"], out) == 0
    assert json.loads(out.getvalue()) == {"ran": True, "wake": False, "due": []}
    out = io.StringIO()
    assert main(["--data", root, "doctor", "--metrics"], out) == 0
    figures = json.loads(out.getvalue())["data"]["metrics"]
    assert "grants_awaiting_human" in figures

    # the_audit_chain_verifies_after_wired_use
    stack.start()
    stack.ok("doctor")
    assert verify(stack.app.context.db).ok


def test_doctor_metrics_reports_stored_bytes_history(stack: Stack) -> None:
    stack.start()
    stack.clock.advance(minutes=2)
    first = stack.ok("doctor", metrics=True)["stored_bytes_history"]
    assert first.get("week_4") is None
    assert first["weeks_recorded"] >= 1
