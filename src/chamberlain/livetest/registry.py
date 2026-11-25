"""The scenario registry: what exists, what each one needs, how it expands over sources.

Contract: docs/live-test.md (Scenarios). A scenario is a function of a `Lab` and a `Target`.
It declares the capabilities it needs; a missing source, capability or setting is a skip with
a reason, never a silent pass.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from chamberlain.livetest.lab import Lab


class Scope(StrEnum):
    MAILBOX = "mailbox"
    """Once per configured mailbox."""
    PAIR = "pair"
    """Once per configured cross-account pair."""
    CALENDAR = "calendar"
    """Once per configured calendar."""
    GLOBAL = "global"
    """Once per run."""


@dataclass(frozen=True)
class Target:
    source: str = ""
    other: str = ""

    @property
    def suffix(self) -> str:
        if self.source and self.other:
            return f"[{self.source}>{self.other}]"
        return f"[{self.source}]" if self.source else ""

    @property
    def tag(self) -> str:
        """A short, subject-safe name for the source (`mail:work` becomes `work`)."""
        return self.source.partition(":")[2]

    @property
    def other_tag(self) -> str:
        return self.other.partition(":")[2]


@dataclass(frozen=True)
class ScenarioSpec:
    id: str
    group: str
    title: str
    scope: Scope
    run: Callable[[Lab, Target], None]
    needs: tuple[str, ...] = ()
    """Capability names the primary source must declare."""
    needs_other: tuple[str, ...] = ()
    """Capability names the other source of a pair must declare."""
    manual: tuple[str, ...] = ()
    """Steps for a scenario the owner does by hand; it is always skipped."""
    last: bool = False
    """Runs after every other scenario and after the final cleanup audit."""
    alone: bool = False
    """Runs by itself, after the scenarios that run side by side (a fault hook, a budget check)."""


@dataclass(frozen=True)
class Planned:
    """One scenario expanded for one target, with a reason if it cannot run here."""

    id: str
    spec: ScenarioSpec
    target: Target
    skip: str = ""


SPECS: list[ScenarioSpec] = []


def scenario(
    id: str,
    group: str,
    title: str,
    scope: Scope,
    *,
    needs: tuple[str, ...] = (),
    needs_other: tuple[str, ...] = (),
    manual: tuple[str, ...] = (),
    last: bool = False,
    alone: bool = False,
) -> Callable[[Callable[[Lab, Target], None]], Callable[[Lab, Target], None]]:
    def register(run: Callable[[Lab, Target], None]) -> Callable[[Lab, Target], None]:
        SPECS.append(
            ScenarioSpec(
                id=id,
                group=group,
                title=title,
                scope=scope,
                run=run,
                needs=needs,
                needs_other=needs_other,
                manual=manual,
                last=last,
                alone=alone,
            )
        )
        return run

    return register
