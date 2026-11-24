"""The owner's old report template becomes their private report layout (06-memory.md).

Synthetic old root only. The layout is a private file in the data root, written once and never
overwritten; the old wording for the assistant reaches the report outline.
"""

from pathlib import Path

from old_fixture import MemorySecrets

from chamberlain.importer.report import ImportReport
from chamberlain.importer.run import run_import
from chamberlain.ports.clock import ManualClock
from chamberlain.runs.compose import outline_text
from chamberlain.runs.models import RunRow, RunState
from chamberlain.runs.template import default_template, load_template, private_path


def test_the_old_report_template_becomes_a_private_layout(
    imported: ImportReport,
    old_root: Path,
    data_root: Path,
    secrets: MemorySecrets,
    clock: ManualClock,
) -> None:
    assert imported.report_template == "created"
    template, problem = load_template(data_root)
    assert problem is None
    by = {slot.id: slot for slot in template.slots}
    # The owner's headings and callout styles are kept; the standard layout is not used.
    assert template.title == "Pass summary {date}"
    assert [(s.id, s.by, s.style) for s in template.slots] == [
        ("access_problems", "harness", "red"),
        ("important_appointments", "harness", "orange"),
        ("open_items", "agent", "plain"),
        ("permission_reviews", "harness", "plain"),
        ("verification", "harness", "plain"),
    ]
    assert by["access_problems"].heading == "ACCESS PROBLEM - CHECK NEEDED"
    assert template.slots != default_template().slots
    # The owner's wording for the assistant's section survives, including the sentence that sat
    # under a section the harness fills; a line the new harness cannot fill is dropped and named.
    wording = by["open_items"].instructions
    assert "List what changed" in wording
    assert "calendar items only when action is needed" in wording
    assert "roundup" not in wording
    assert any("schedule.roundup_day" in w for w in imported.warnings)
    run = RunRow(
        "run_0123456789abcdef", "morning", "2026-01-05", RunState.RUNNING, "", None, None, None
    )
    outline = outline_text(template, run)
    assert "## Open items" in outline
    assert "List what changed" in outline
    assert (
        "ACCESS PROBLEM" in outline
    )  # named as the harness's, so the assistant does not repeat it
    # Nothing of it stays in the list of settings left unmapped; the others still do.
    assert imported.unmapped_settings["templates"] == {"morning": "Start the morning pass."}

    # A second import changes nothing, and an owner's later edit is never overwritten.
    before = private_path(data_root).read_bytes()
    again = run_import(old_root, data_root, clock=clock, secrets=secrets)
    assert again.report_template == "already identical"
    assert private_path(data_root).read_bytes() == before
    private_path(data_root).write_text(before.decode("utf-8") + "# edited\n", encoding="utf-8")
    third = run_import(old_root, data_root, clock=clock, secrets=secrets)
    assert third.report_template.startswith("kept existing")
    assert private_path(data_root).read_text(encoding="utf-8").endswith("# edited\n")
