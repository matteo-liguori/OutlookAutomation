"""Seeding known recipients from a source's Sent folder (G-15, owner decision 2026-11-24).

Read-only: it lists the Sent folder through the connector, page by page to the end,
and records every To and Cc address with origin `sent-history`. It never changes mail
and never touches grants.
"""

import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from chamberlain.domain.mail import FolderRole, MailQuery
from chamberlain.ports.clock import Clock
from chamberlain.ports.mail import MailPort
from chamberlain.schema.errors import ChamberlainError
from chamberlain.store.db import transaction
from chamberlain.store.migrate import stamp
from chamberlain.writes import repo

DEFAULT_MONTHS = 24
MAX_PAGES = 2000
"""A guard against a connector that never ends its cursor chain."""


@dataclass(frozen=True)
class SeedReport:
    source: str
    added: int = 0
    scanned: int = 0
    known: int = 0
    """Recipients known for the source after seeding, all origins."""
    complete: bool = False
    reason: str = ""
    """Why the scan was not seen to the end, or why nothing was read."""

    def summary(self) -> dict[str, object]:
        return {
            "source": self.source,
            "added": self.added,
            "messages_scanned": self.scanned,
            "known_recipients": self.known,
            "complete": self.complete,
            "reason": self.reason,
        }


def seed_source(
    db: sqlite3.Connection, mail: MailPort, clock: Clock, source: str, *, months: int
) -> SeedReport:
    """Read the Sent folder of one mail source and add its recipients. Never raises."""
    found: set[str] = set()
    scanned = 0
    complete = False
    reason = ""
    try:
        sent = [f for f in mail.folders(source) if f.role is FolderRole.SENT]
        if not sent:
            reason = "The source has no Sent folder the connector can identify."
        since = clock.now() - timedelta(days=round(months * 30.4375))
        for folder in sent:
            cursor: str | None = None
            for _ in range(MAX_PAGES):
                page = mail.search(
                    source,
                    MailQuery(folder_id=folder.id, since=since, limit=200),
                    cursor,
                )
                for item in page.items:
                    scanned += 1
                    found.update(a.address for a in (*item.to, *item.cc))
                if page.cursor is None:
                    complete = page.complete
                    if not page.complete:
                        reason = "; ".join(g.reason for g in page.gaps) or "The scan was partial."
                    break
                cursor = page.cursor
            else:
                complete = False
                reason = "The Sent folder did not end within the page limit."
            if not complete:
                break
    except ChamberlainError as error:
        reason = f"{error.code.value}: {error.message}"
        complete = False
    own = repo.own_addresses(db, source)
    with transaction(db):
        added = repo.seed_known_recipients(
            db,
            source,
            (a for a in found if a not in own and not a.endswith(".invalid")),
            stamp(clock),
        )
        known = len(repo.known_recipients(db, source))
    return SeedReport(source, added, scanned, known, complete, reason)


def seed_all(
    db: sqlite3.Connection, mail_ports: dict[str, MailPort], clock: Clock, *, months: int
) -> list[SeedReport]:
    return [
        seed_source(db, mail_ports[source], clock, source, months=months)
        for source in sorted(mail_ports)
    ]
