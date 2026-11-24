"""The data behind the viewer's Live test page.

Contract: docs/live-test.md (Reading the score). The viewer (`surface/viewer/app.py`) calls
`recent_runs` for `GET /api/live-test`; the page itself is `static/livetest.js`. Reports hold
handles, codes and timings only, so nothing here can show a subject or an address.
"""

from pathlib import Path
from typing import Any

from chamberlain.livetest.model import Status
from chamberlain.livetest.report import read_reports

RUNS_SHOWN = 10


def recent_runs(data_root: Path) -> dict[str, Any]:
    """The latest reports, newest first, in the shape the page draws."""
    runs: list[dict[str, Any]] = []
    for report in read_reports(data_root, RUNS_SHOWN):
        runs.append(
            {
                "run_id": report.run_id,
                "started_at": report.started_at,
                "mode": report.mode,
                "kept": report.kept,
                "stopped": report.stopped,
                "score": report.score.model_dump(mode="json"),
                "leftovers": len(report.leftovers),
                "results": [
                    {
                        "id": result.id,
                        "status": result.status.value,
                        "reason": result.reason,
                        "seconds": result.seconds,
                    }
                    for result in report.results
                    if result.status is not Status.SKIPPED or result.reason
                ],
            }
        )
    return {"runs": runs}
