"""Writing, reading and trimming run reports; the table and the markdown.

Contract: docs/live-test.md (Reading the score). Reports live in `<data root>/logs/live-test/`.
The memory sweeper rotates only files at the top of `logs/`, so this module applies the same
lifetime rules (`LOG_DAYS`, `LOG_MAX_BYTES`) to its own folder.
"""

import json
import os
from datetime import datetime
from pathlib import Path

from chamberlain.limits import LOG_DAYS, LOG_MAX_BYTES
from chamberlain.livetest.markers import RUN_ID_PATTERN
from chamberlain.livetest.model import RunReport, Status

REPORT_DIR = ("logs", "live-test")


def report_dir(data_root: Path) -> Path:
    return data_root.joinpath(*REPORT_DIR)


def write_report(data_root: Path, report: RunReport, now: datetime) -> tuple[Path, Path]:
    """Write `<run id>.json` and `<run id>.md`; then trim old reports."""
    folder = report_dir(data_root)
    folder.mkdir(parents=True, exist_ok=True)
    json_path = folder / f"{report.run_id}.json"
    md_path = folder / f"{report.run_id}.md"
    for path, text in ((json_path, report.model_dump_json(indent=2)), (md_path, markdown(report))):
        pending = path.with_suffix(path.suffix + ".tmp")
        pending.write_text(text, encoding="utf-8")
        os.replace(pending, path)
    trim_reports(data_root, now)
    return json_path, md_path


def read_reports(data_root: Path, limit: int = 20) -> list[RunReport]:
    """The most recent reports, newest first. An unreadable file is skipped."""
    folder = report_dir(data_root)
    if not folder.is_dir():
        return []
    found: list[RunReport] = []
    for path in sorted(folder.glob("*.json"), reverse=True):
        if not RUN_ID_PATTERN.match(path.stem):
            continue
        try:
            found.append(RunReport.model_validate(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            continue
        if len(found) >= limit:
            break
    return found


def trim_reports(data_root: Path, now: datetime) -> int:
    """Delete reports older than `LOG_DAYS` and the oldest beyond `LOG_MAX_BYTES`."""
    folder = report_dir(data_root)
    if not folder.is_dir() or folder.is_symlink():
        return 0
    cutoff = now.timestamp() - LOG_DAYS * 86400
    files = sorted(
        (entry.stat().st_mtime, entry.stat().st_size, entry)
        for entry in folder.iterdir()
        if entry.is_file() and not entry.is_symlink()
    )
    total = sum(size for _, size, _ in files)
    removed = 0
    for mtime, size, entry in files:
        if mtime < cutoff or total > LOG_MAX_BYTES:
            try:
                entry.unlink()
            except OSError:
                continue
            total -= size
            removed += 1
    return removed


def table(report: RunReport) -> str:
    """The plain-text summary printed at the end of a run."""
    width = max((len(r.id) for r in report.results), default=10)
    lines = [f"Live test {report.run_id} ({report.mode})", ""]
    for result in report.results:
        mark = {Status.PASS: "PASS", Status.FAIL: "FAIL", Status.SKIPPED: "skip"}[result.status]
        extra = f"  {result.reason}" if result.reason else ""
        lines.append(f"  {mark}  {result.id:<{width}}  {result.seconds:6.1f}s{extra}")
    lines.append("")
    lines.append(score_line(report))
    if report.leftovers:
        lines.append(
            f"Items left behind: {len(report.leftovers)} (run `chamberlain live-test sweep`)"
        )
    if report.stopped:
        lines.append(report.stopped)
    return "\n".join(lines)


def score_line(report: RunReport) -> str:
    s = report.score
    return (
        f"Score {s.percent}%: {s.passed} passed, {s.failed} failed, {s.skipped} skipped "
        f"(coverage {s.coverage_percent}%, left behind {s.leftovers})."
    )


def markdown(report: RunReport) -> str:
    """The short report: handles, codes and timings, never subjects or text."""
    lines = [
        f"# Live test {report.run_id}",
        "",
        f"- Mode: {report.mode}{' (items kept)' if report.kept else ''}",
        f"- Started: {report.started_at}; finished: {report.finished_at}",
        f"- Sources: {', '.join(report.sources) or 'none'}",
        f"- {score_line(report)}",
    ]
    if report.stopped:
        lines.append(f"- Stopped: {report.stopped}")
    lines += ["", "| Scenario | Result | Seconds | Note |", "| --- | --- | ---: | --- |"]
    for r in report.results:
        note = r.reason or ("cleanup: " + r.cleanup if r.cleanup not in ("clean", "") else "")
        lines.append(f"| {r.id} | {r.status.value} | {r.seconds:.1f} | {note} |")
    for r in report.results:
        if r.manual:
            lines += [
                "",
                f"## Manual steps for {r.id}",
                *[f"{i}. {s}" for i, s in enumerate(r.manual, 1)],
            ]
    probed = [r for r in report.results if r.probe_checks]
    if probed:
        lines += ["", "## Checks that used the read-only probe"]
        for r in probed:
            lines.append(f"- {r.id}: {'; '.join(r.probe_checks)}")
    if report.leftovers:
        lines += ["", "## Left behind", *[f"- {h}" for h in report.leftovers]]
    lines += ["", "## Doctor", f"- Before: {', '.join(report.doctor_before) or 'not taken'}"]
    lines.append(f"- After: {', '.join(report.doctor_after) or 'not taken'}")
    handles = sorted({h for r in report.results for h in r.handles})
    if handles:
        lines += ["", "## Handles touched", ", ".join(handles)]
    return "\n".join(lines) + "\n"
