"""After a pass's report is kept, mark the mail that pass handled as read (when the owner asks).

Contract: docs/contracts/04-writes.md (W-43) and 05-grants.md (G-24). Only with the profile option
`read_state.reporting_marks_read` AND the mailbox's confirmed standing grant. "Covered by the
report" means: shown to this pass's session in a mail batch, triaged by it, received (not sent)
mail, and shown before the report was kept. Mail that arrived later, or that no pass showed, is
never touched. Only successful per-message results are stamped. Each finished run triggers this
pass once.
"""

import logging
from collections import defaultdict

from chamberlain.app.context import Context
from chamberlain.domain.grants import GrantState
from chamberlain.domain.intents import MailMarkRead
from chamberlain.domain.operations import OperationState
from chamberlain.limits import MARK_READ_MAX
from chamberlain.runs.models import RunRow
from chamberlain.schema.safelog import log_failure
from chamberlain.store.db import transaction
from chamberlain.store.migrate import stamp
from chamberlain.writes import read_state, repo
from chamberlain.writes.core import Engine

log = logging.getLogger("chamberlain.runs")


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
    engine = Engine(ctx)
    batches: list[tuple[str, list[tuple[int, str]]]] = []
    for source, items in by_source.items():
        candidates = repo.grants_in_states(ctx.db, (GrantState.ACTIVE,), source)
        if any(g.scope and g.scope.allow_mark_read and g.scope.folders for g in candidates):
            # A grant can cover one folder while another grant covers its neighbour.
            folders: dict[str, list[tuple[int, str]]] = defaultdict(list)
            port = ctx.sources.mail_port(source)
            for seq, handle in items:
                try:
                    folder = port.folder_of(source, ctx.services.refs.locate(handle)[1])
                    folders[folder].append((seq, handle))
                except Exception:
                    log_failure(log, "locating reported mail for a scoped read grant failed")
            batches.extend((source, group) for group in folders.values())
        else:
            batches.append((source, items))
    for source, items in batches:
        for start in range(0, len(items), MARK_READ_MAX):
            chunk = items[start : start + MARK_READ_MAX]
            try:
                intent = MailMarkRead(source=source, messages=tuple(h for _, h in chunk))
                grant = read_state.active_grant(ctx, intent)
                if grant is None:
                    read_state.propose_missing(
                        ctx.db, ctx.clock, ctx.services.audit, [source], policy=ctx.write_policy
                    )
                    continue
                operation = engine.run(intent, actor=read_state.ACTOR, grant=grant.handle)
                if operation is None or operation.state is not OperationState.CONFIRMED:
                    continue
                receipt = operation.receipt()
                if receipt is None:
                    continue
                marked = {item.locator for item in receipt.items if item.ok}
                refs = operation.refs()
                with transaction(ctx.db):
                    ctx.db.executemany(
                        "UPDATE mail_seen SET read_marked_at = ? WHERE seq = ?",
                        [(now, seq) for seq, handle in chunk if refs.get(handle) in marked],
                    )
            except Exception:
                log_failure(log, "marking reported mail read failed")
