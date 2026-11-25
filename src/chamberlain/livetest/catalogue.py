"""The planned scenarios: the registry expanded over the configured sources.

Contract: docs/live-test.md (Scenarios, Options). A scenario that cannot run here is still
in the plan, with the reason; only `--only` removes scenarios from the plan.
"""

import fnmatch
import importlib
from collections.abc import Sequence

from chamberlain.app.context import SourceRouter
from chamberlain.domain.capabilities import HealthState
from chamberlain.livetest.config import LiveTestConfig
from chamberlain.livetest.registry import SPECS, Planned, ScenarioSpec, Scope, Target
from chamberlain.schema.errors import ChamberlainError

SCENARIO_MODULES = (
    "chamberlain.livetest.scenarios_mail",
    "chamberlain.livetest.scenarios_calendar",
    "chamberlain.livetest.scenarios_harness",
)
for _module in SCENARIO_MODULES:
    importlib.import_module(_module)


def matches(pattern: str, scenario_id: str) -> bool:
    """A glob when it has `*` or `?`, else a substring (so `reply` finds `mail.reply_thread`).

    Square brackets are ordinary characters: scenario ids end in `[source]`.
    """
    if "*" in pattern or "?" in pattern:
        glob = pattern.replace("[", "[[]")
        return fnmatch.fnmatchcase(scenario_id.lower(), glob.lower())
    return pattern.lower() in scenario_id.lower()


HEALTH_TRIES = 3
"""How often a mailbox found stale is checked again before its scenarios are skipped."""


def _capabilities(sources: SourceRouter, source: str) -> tuple[set[str], str]:
    """`(capability names, problem)`: the problem is empty when the source can be used."""
    try:
        if source.startswith("mail:"):
            if source not in sources.mail:
                return set(), f"{source} is not a configured, enabled source"
            port = sources.mail_port(source)
            health = port.health(source)
            for _ in range(HEALTH_TRIES - 1):
                # "stale" means two quick Inbox samples differed: mail arriving between them
                # (a test's own mail) is enough, so one stale answer does not skip a mailbox.
                if health.state is not HealthState.STALE:
                    break
                health = port.health(source)
            names = set(port.capabilities(source).names())
        else:
            if source not in sources.calendar:
                return set(), f"{source} is not a configured, enabled source"
            cal = sources.calendar_port(source)
            health = cal.health(source)
            names = set(cal.capabilities(source).names())
    except ChamberlainError as error:
        return set(), f"{source} could not be reached ({error.code})"
    if health.state is not HealthState.OK:
        return names, f"{source} is {health.state.value}"
    return names, ""


def _skip_reason(
    spec: ScenarioSpec,
    target: Target,
    sources: SourceRouter,
    cache: dict[str, tuple[set[str], str]],
) -> str:
    if spec.manual:
        return "manual scenario: it cannot be forced safely; the steps are in the report"
    for source, needs in ((target.source, spec.needs), (target.other, spec.needs_other)):
        if not source:
            continue
        if source not in cache:
            cache[source] = _capabilities(sources, source)
        names, problem = cache[source]
        if problem:
            return problem
        missing = [need for need in needs if need not in names]
        if "calendar.invite" in missing:
            return (
                f"{source} does not declare calendar.invite "
                "(invitations are not supported there; Owl and Exchange refuse them)"
            )
        if missing:
            return f"{source} does not declare {', '.join(missing)}"
    return ""


def expand(spec: ScenarioSpec, config: LiveTestConfig) -> list[Target]:
    match spec.scope:
        case Scope.MAILBOX:
            return [Target(source) for source in config.mail]
        case Scope.PAIR:
            return [Target(pair.source, pair.to) for pair in config.pairs]
        case Scope.CALENDAR:
            return [Target(source) for source in config.calendars]
        case Scope.GLOBAL:
            return [Target()]


_NOTHING = {
    Scope.MAILBOX: "live_test.mail lists no mailbox",
    Scope.PAIR: "live_test.pairs lists no cross-account pair",
    Scope.CALENDAR: "live_test.calendars lists no calendar",
}


def build_plan(
    config: LiveTestConfig,
    sources: SourceRouter,
    *,
    only: Sequence[str] = (),
    skip: Sequence[str] = (),
) -> list[Planned]:
    """Every scenario for every target, ordered: normal, then `last` ones."""
    cache: dict[str, tuple[set[str], str]] = {}
    planned: list[Planned] = []
    for spec in SPECS:
        targets = expand(spec, config)
        if not targets:
            planned.append(Planned(spec.id, spec, Target(), _NOTHING[spec.scope]))
            continue
        for target in targets:
            scenario_id = f"{spec.id}{target.suffix}"
            reason = _skip_reason(spec, target, sources, cache)
            if not reason and any(matches(p, scenario_id) for p in skip):
                reason = "skipped by --skip"
            planned.append(Planned(scenario_id, spec, target, reason))
    if only:
        planned = [p for p in planned if any(matches(pattern, p.id) for pattern in only)]
    return [p for p in planned if not p.spec.last] + [p for p in planned if p.spec.last]
