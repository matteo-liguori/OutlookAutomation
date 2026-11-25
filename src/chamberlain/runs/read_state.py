"""After a pass's report is kept, mark the mail that pass handled as read (when the owner asks).

Contract: docs/contracts/04-writes.md (W-43) and 05-grants.md (G-24). Only with the profile option
`read_state.reporting_marks_read` AND the mailbox's confirmed standing grant. "Covered by the
report" means: shown to this pass's session in a mail batch, triaged by it, received (not sent)
mail, and shown before the report was kept. Mail that arrived later, or that no pass showed, is
never touched. Each message is tried once; a failed or uncertain write is not repeated by this code.
"""

import logging
from collections import defaultdict

from chamberlain.app.context import Context
from chamberlain.domain.intents import MailMarkRead
from chamberlain.limits import MARK_READ_MAX
from chamberlain.runs.models import RunRow
from chamberlain.schema.safelog import log_failure
from chamberlain.store.db import transaction
from chamberlain.store.migrate import stamp
from chamberlain.writes import read_state
from chamberlain.writes.engine import Engine

log = logging.getLogger("chamberlain.runs")


class _SystemEngine(Engine):
    def actor(self) -> str:
        return read_state.ACTOR


def mark_reported(ctx: Context, run: RunRow) -> None:
    """Mark read what this run's report covered. Never raises."""
    if run.session is None or not read_state.options(ctx.db).reporting_marks_read:
        return
    now = stamp(ctx.clock)
    rows = ctx.db.execute(
        "SELECT s.seq AS seq, s.source_id AS source, s.handle AS handle FROM mail_seen s "
        "WHERE s.delivered_session = ? AND s.first_seen_at <= ? AND s.sent = 0 "
        "AND s.triaged = 1 AND s.read_marked_at IS NULL ORDER BY s.seq",
        (run.session, now),
    ).fetchall()
    by_source: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for row in rows:
        by_source[str(row["source"])].append((int(row["seq"]), str(row["handle"])))
    if not by_source:
        return
    engine = _SystemEngine(ctx)
    waiting = [s for s in sorted(by_source) if read_state.active_grant(ctx.db, s) is None]
    if waiting:
        read_state.propose_missing(ctx.db, ctx.clock, ctx.services.audit, waiting)
    for source in sorted(set(by_source) - set(waiting)):
        items = by_source[source]
        for start in range(0, len(items), MARK_READ_MAX):
            chunk = items[start : start + MARK_READ_MAX]
            try:
                intent = MailMarkRead(source=source, messages=tuple(h for _, h in chunk))
                proposal = engine.propose_intent(intent)
                if proposal.ready and proposal.operation is not None:
                    engine.do(proposal.operation.handle)
            except Exception:
                log_failure(log, "marking reported mail read failed")
            with transaction(ctx.db):
                ctx.db.executemany(
                    "UPDATE mail_seen SET read_marked_at = ? WHERE seq = ?",
                    [(now, seq) for seq, _ in chunk],
                )
