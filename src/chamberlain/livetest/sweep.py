"""The sweep: remove marked items left by a crashed or `--keep` run.

Contract: docs/live-test.md (Cleanup). Only an item whose subject or title opens with
`CHAMBERLAIN LIVE TEST <run id>` (after any `Re:`) is touched, and the message headers are
read through `open` before each trash. Nothing else is matched, whatever a search returns.
"""

from dataclasses import dataclass, field

from chamberlain.app.core import App
from chamberlain.livetest.authority import require_grants
from chamberlain.livetest.config import LiveTestConfig
from chamberlain.livetest.env import Environment
from chamberlain.livetest.lab import RunStopped, ScenarioFailure, ScenarioSkip
from chamberlain.livetest.markers import MARKER, new_run_id
from chamberlain.livetest.runner import RunOptions, open_lab
from chamberlain.schema.errors import ChamberlainError


@dataclass
class SweepResult:
    mail_removed: int = 0
    events_removed: int = 0
    problems: list[str] = field(default_factory=lambda: list[str]())
    notes: list[str] = field(default_factory=lambda: list[str]())
    stopped: str = ""

    def lines(self) -> list[str]:
        if self.stopped:
            return [self.stopped]
        return [
            f"Removed {self.mail_removed} marked message(s) and {self.events_removed} event(s).",
            *self.notes,
            *[f"Could not remove: {p}" for p in self.problems],
        ]


def sweep(app: App, env: Environment, config: LiveTestConfig, options: RunOptions) -> SweepResult:
    """Find and remove marked items of any earlier run, in the configured sources only."""
    result = SweepResult()
    run_id = new_run_id(env.clock.now(), "swep")
    lab = open_lab(app, env, config, run_id, options, sweeping=True)
    env.faults.disarm()
    try:
        require_grants(lab, list(config.mail))
    except RunStopped as stop:
        result.stopped = str(stop)
        return result
    for source in config.mail:
        try:
            result.mail_removed += lab.remove_mail(source, MARKER)
        except (ChamberlainError, ScenarioFailure, ScenarioSkip) as error:
            result.problems.append(f"{source}: {str(error)[:100]}")
    for source in config.calendars:
        if not lab.approve_edits:
            result.notes.append(
                f"Events in {source} were not swept: calendar approval is off "
                "(live_test.approve_calendar_edits or --approve-calendar-edits)."
            )
            continue
        try:
            result.events_removed += lab.remove_events(source, MARKER)
        except (ChamberlainError, ScenarioFailure, ScenarioSkip) as error:
            result.problems.append(f"{source}: {str(error)[:100]}")
    return result
