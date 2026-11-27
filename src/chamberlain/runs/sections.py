"""The sections of a pass report that the harness writes itself, from its own records.

Contract: docs/contracts/06-memory.md (the report section). Nothing here asks the assistant: sources
and their newest times, how far mail was read, the changes made to mail, calendar freshness,
upcoming important appointments and permission reviews due are facts the harness already holds.
Each builder returns markdown, or an empty string when there is nothing to say (the section is
then left out). All times are shown in the schedule's own zone.
"""

from collections import defaultdict
from datetime import timedelta
from zoneinfo import ZoneInfo

from chamberlain.app.context import Context
from chamberlain.attention import delivery_check
from chamberlain.attention import repo as attention_repo
from chamberlain.domain.grants import GrantState
from chamberlain.domain.operations import OUTSIDE_STATE_WORDS
from chamberlain.domain.timefmt import parse
from chamberlain.runs import appointments
from chamberlain.runs import report_repo as repo
from chamberlain.runs.models import RunRow
from chamberlain.store.migrate import stamp
from chamberlain.writes import repo as writes_repo

HEALTH_TEXT = {
    "stale": "its data may be out of date",
    "sign_in_needed": "it needs you to sign in again",
    "unavailable": "it could not be reached",
}
KIND_TEXT = {"mail.move": "moved", "mail.trash": "trashed", "mail.mark_read": "marked read"}
UNSETTLED = frozenset(
    {"proposed", "authorised", "prepared", "dispatched", "uncertain", "reconciling"}
)
BAD_END = frozenset({"failed_clean", "confirmed_absent", "cancelled", "abandoned"})


CATCHING_UP = timedelta(minutes=10)
"""A stale source whose unread span is no longer than this was only catching up at report time
(it is read at the next check): disclosed under verification, not raised as an access problem."""


def when(value: str | None, zone: ZoneInfo) -> str:
    """`Mon 05 Jan 09:00`, or `never` for a missing time."""
    if not value:
        return "never"
    moment = parse(value).astimezone(zone)
    return moment.strftime("%a %d %b %H:%M")


def _cell(text: str) -> str:
    """Text from cards, grants or sources, made safe to put in a table or a line of markdown:
    no bar, link, emphasis or code can come out of it."""
    plain = text.translate({ord(c): " " for c in "|[]()*`_<>#"})
    return " ".join(plain.split())


class Facts:
    """What a report is built from, read once."""

    def __init__(self, ctx: Context, run: RunRow, zone: ZoneInfo) -> None:
        self.ctx = ctx
        self.zone = zone
        self.now = stamp(ctx.clock)
        self.since = run.started_at or run.due_at
        self.sources = repo.source_facts(ctx.db, ctx.sources.test_only)
        self.filing = repo.filing_facts(ctx.db, self.since)
        self.outside = repo.outside_facts(ctx.db, self.since)
        self.unsynced: dict[str, list[str]] = {}
        for source, folder in attention_repo.sync_failures(ctx.db):
            self.unsynced.setdefault(source, []).append(folder.name)
        for names in self.unsynced.values():
            names.sort()
        self.checks = delivery_check.records(ctx.db)
        self.gaps: list[str] = []
        """Reasons the pass is only partly verified, filled by the builders."""

    def name(self, source: repo.SourceFact) -> str:
        return _cell(source.label or source.id)

    def failed_check(self, source: repo.SourceFact) -> str:
        """` The delivery check also failed.` when the fallback check ran lately and failed."""
        record = self.checks.get(source.id)
        if record is None or record.result not in ("missing", "not_sent") or not record.result_at:
            return ""
        recent = parse(self.now) - parse(record.result_at) < delivery_check.FAILED_NOTE_FOR
        return " The delivery check also failed." if recent else ""

    def confirmed_by_check(self, source: repo.SourceFact) -> str:
        """The time of the delivery check that made this mailbox `ok`, else empty."""
        record = self.checks.get(source.id)
        if record is None or record.result != "arrived" or record.result_at != source.fresh_as_of:
            return ""
        return when(record.result_at, self.zone)


def _gap(source: repo.SourceFact) -> timedelta | None:
    if not (source.gap_from and source.gap_to):
        return None
    return parse(source.gap_to) - parse(source.gap_from)


def _catching_up(source: repo.SourceFact) -> bool:
    """Only behind by a few minutes at report time, which the next check reads."""
    gap = _gap(source)
    short = gap is None or gap <= CATCHING_UP
    return short and source.health in ("ok", "stale") and (source.health == "stale" or bool(gap))


def access_problems(facts: Facts) -> str:
    lines: list[str] = []
    for source in facts.sources:
        who = f"**{facts.name(source)}** ({source.id})"
        checked = when(source.checked_at, facts.zone)
        if source.health is None:
            lines.append(f"- {who}: it has not been checked yet.")
            facts.gaps.append(f"{source.id} was not checked")
        elif source.kind == "mail" and _catching_up(source) and not facts.failed_check(source):
            facts.gaps.append(
                f"{source.id} was still catching up when this was written (read at the next check)"
            )
            continue
        elif source.health != "ok":
            fresh = when(source.fresh_as_of, facts.zone) if source.fresh_as_of else "not yet"
            lines.append(
                f"- {who}: {HEALTH_TEXT.get(source.health, source.health)}. Last checked "
                f"{checked}; last confirmed fresh {fresh}.{facts.failed_check(source)}"
            )
            facts.gaps.append(f"{source.id} is not fully readable")
        gap = _gap(source)
        if gap is not None and gap > timedelta(0):
            lines.append(
                f"- {who}: mail from {when(source.gap_from, facts.zone)} to "
                f"{when(source.gap_to, facts.zone)} has not been read."
            )
            facts.gaps.append(f"{source.id} has a gap in the mail read")
        folders = facts.unsynced.get(source.id)
        if folders:
            lines.append(
                f"- {who}: {len(folders)} folder(s) did not sync, so mail in them may be late or "
                f"missing: {', '.join(_cell(name) for name in folders)}. They are tried again "
                "at every check."
            )
            facts.gaps.append(f"{source.id} had folders that did not sync")
    return "\n".join(lines)


def important_appointments(facts: Facts) -> str:
    """One line per important date with a reminder due now; later dates summed up beside them."""
    lines: list[str] = []
    today = facts.ctx.clock.now().astimezone(facts.zone).date()
    found = appointments.dates(facts.ctx)
    for item in (d for d in found if d.due):
        start = parse(item.start).astimezone(facts.zone)
        days = (start.date() - today).days
        away = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
        lines.append(
            f"- **{_cell(item.title)}** - {start.strftime('%a %d %b %H:%M')} ({away}). "
            f"Card: {_cell(item.card_title)}. "
            "Acknowledgement needed: open the viewer and hold the button."
        )
    later = [d for d in found if not d.due and d.next_reminder]
    if lines and later:
        # Only beside a due one: on its own it would sit under "acknowledgement required".
        nxt = min(later, key=lambda d: d.next_reminder or d.start)
        lines.append(
            f"- {len(later)} more important date(s) coming up; the next reminder shows "
            f"{when(nxt.next_reminder, facts.zone)}: {_cell(nxt.title)}."
        )
    return "\n".join(lines)


def permission_reviews(facts: Facts) -> str:
    lines = [
        f"- **{_cell(g.title)}** ({g.handle}, {g.source}): review due since "
        f"{when(g.review_at, facts.zone)}. Keep, change or revoke it in the viewer under Grants."
        for g in repo.grants_due_for_review(facts.ctx.db, facts.now)
    ]
    waiting = len(writes_repo.grants_in_states(facts.ctx.db, [GrantState.PROPOSED]))
    if waiting:
        lines.append(
            f"- {waiting} proposed permission(s) wait for you to confirm them in the viewer."
        )
    return "\n".join(lines)


def _filing_line(source: str, items: list[repo.FilingFact], facts: Facts) -> str:
    done: dict[str, int] = defaultdict(int)
    unverified = unsettled = failed = 0
    for fact in items:
        if fact.state == "confirmed":
            done[fact.kind] += fact.count
            if not fact.verified:
                unverified += fact.count
        elif fact.state in UNSETTLED:
            unsettled += fact.count
        elif fact.state in BAD_END:
            failed += fact.count
    parts = [f"{KIND_TEXT[kind]} {done[kind]}" for kind in KIND_TEXT if done[kind]]
    extra: list[str] = []
    if unverified:
        extra.append(f"{unverified} not checked at the mail server")
        facts.gaps.append(f"{source} has filing changes that were not verified")
    if unsettled:
        extra.append(f"{unsettled} not settled")
        facts.gaps.append(f"{source} has filing changes that are not settled")
    if failed:
        extra.append(f"{failed} did not happen")
    summary = ", ".join(parts) or "nothing filed"
    return f"- {source}: {summary}" + (f" ({'; '.join(extra)})" if extra else "") + "."


def verification(facts: Facts) -> str:
    rows = [
        "| Source | State | Newest | Covered through |",
        "| --- | --- | --- | --- |",
    ]
    for source in facts.sources:
        state = "not checked" if source.health is None else source.health.replace("_", " ")
        if source.health == "ok" and (at := facts.confirmed_by_check(source)):
            state = f"fresh, confirmed by a delivery check at {at[-5:]}"
        if source.kind == "mail":
            newest = when(source.newest_mail, facts.zone) if source.newest_mail else "none seen"
            through = (
                when(source.covered_through, facts.zone) if source.covered_through else "not read"
            )
            if source.covered_through is None:
                facts.gaps.append(f"{source.id} has not been read")
            if facts.unsynced.get(source.id):
                state += f", {len(facts.unsynced[source.id])} folder(s) not synced"
                facts.gaps.append(f"{source.id} had folders that did not sync")
        else:
            newest = f"fresh as of {when(source.fresh_as_of, facts.zone)}"
            through = f"checked {when(source.checked_at, facts.zone)}"
        rows.append(f"| {facts.name(source)} | {state} | {newest} | {through} |")
    lines = rows
    by_source: dict[str, list[repo.FilingFact]] = defaultdict(list)
    for fact in facts.filing:
        by_source[fact.source].append(fact)
    if by_source:
        lines += ["", "**Filing during this pass**", ""]
        lines += [_filing_line(source, by_source[source], facts) for source in sorted(by_source)]
    if facts.outside:
        lines += ["", "**Outside actions during this pass**", ""]
        for item in facts.outside:
            card = f" Card: {_cell(item.card_title)}." if item.card_title else ""
            said = f" It said: {_cell(item.reported)}." if item.reported else ""
            lines.append(
                f"- {_cell(item.where)}: {_cell(item.what)} - "
                f"{OUTSIDE_STATE_WORDS.get(item.state, item.state)}.{said}{card}"
            )
        lines.append(
            "The assistant did these outside Chamberlain. Chamberlain did not do them and "
            "cannot check them."
        )
        facts.gaps.append("outside actions were reported by the assistant, not checked")
    lines.append("")
    if facts.gaps:
        reasons = "; ".join(dict.fromkeys(facts.gaps))
        lines.append(f"**Partially verified**: {reasons}.")
    else:
        lines.append("Every source was readable and every change was verified.")
    return "\n".join(lines)


BUILDERS = {
    "access_problems": access_problems,
    "important_appointments": important_appointments,
    "permission_reviews": permission_reviews,
    "verification": verification,
}
