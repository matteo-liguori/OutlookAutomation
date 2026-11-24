"""The schedule record: which passes are due, when, in which zone.

Contract: docs/contracts/07-attention.md (`schedule.pass_due`) and docs/contracts/09-storage.md.
The operating system scheduler only wakes `chamberlain tick`; what is due is decided here,
so a missed wake-up shows as a missed run rather than silently disappearing.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from chamberlain.app.context import Context
from chamberlain.runs import repo
from chamberlain.runs.models import PassKind, ScheduleRow
from chamberlain.runs.tuning import WEEKDAYS_ALL
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.store.db import transaction
from chamberlain.store.migrate import stamp

_NAME = re.compile(r"^[a-z0-9_.-]{1,40}$")
_TIME = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


@dataclass(frozen=True)
class ScheduleSpec:
    """One pass the person wants: `kind` at `at_local` on `weekdays` (ISO, Monday is 1)."""

    name: str
    kind: PassKind
    at_local: str
    zone: str
    weekdays: frozenset[int] = frozenset(range(1, 8))
    enabled: bool = True


def _invalid(message: str, field: str) -> ChamberlainError:
    return ChamberlainError(ErrorCode.CONFIG_INVALID, message, field=field)


def validate(spec: ScheduleSpec) -> ScheduleSpec:
    """The spec, or `config.invalid` naming the field that is wrong."""
    if not _NAME.match(spec.name):
        raise _invalid("A schedule name is 1 to 40 of a-z, 0-9, dot, dash, underscore.", "name")
    if not _TIME.match(spec.at_local):
        raise _invalid("A schedule time is HH:MM on a 24-hour clock.", "at_local")
    if not spec.weekdays or not spec.weekdays <= frozenset(range(1, 8)):
        raise _invalid("Weekdays are numbers 1 (Monday) to 7 (Sunday).", "weekdays")
    try:
        ZoneInfo(spec.zone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise _invalid("That time zone is not known.", "zone") from None
    return spec


def _weekdays_text(days: Iterable[int]) -> str:
    return ",".join(str(day) for day in sorted(set(days))) or WEEKDAYS_ALL


def set_schedule(ctx: Context, spec: ScheduleSpec) -> ScheduleRow:
    """Add or replace one schedule."""
    valid = validate(spec)
    with transaction(ctx.db):
        repo.upsert_schedule(
            ctx.db,
            name=valid.name,
            kind=valid.kind.value,
            at_local=valid.at_local,
            weekdays=_weekdays_text(valid.weekdays),
            zone=valid.zone,
            enabled=valid.enabled,
            now=stamp(ctx.clock),
        )
    return next(row for row in repo.schedules(ctx.db) if row.name == valid.name)


def apply_schedules(ctx: Context, specs: Sequence[ScheduleSpec]) -> list[ScheduleRow]:
    """Make the record equal to `specs`: the configuration's schedule is the only source.

    Validation happens first, so a bad entry changes nothing. A schedule that is no longer
    named is removed together with its runs (the foreign key cascades).
    """
    valid = [validate(spec) for spec in specs]
    names = {spec.name for spec in valid}
    if len(names) != len(valid):
        raise _invalid("Two schedules have the same name.", "name")
    with transaction(ctx.db):
        for spec in valid:
            set_schedule(ctx, spec)
        for row in repo.schedules(ctx.db):
            if row.name not in names:
                repo.delete_schedule(ctx.db, row.name)
    return repo.schedules(ctx.db)


def remove_schedule(ctx: Context, name: str) -> bool:
    with transaction(ctx.db):
        return repo.delete_schedule(ctx.db, name) > 0


def list_schedules(ctx: Context) -> list[ScheduleRow]:
    return repo.schedules(ctx.db)
