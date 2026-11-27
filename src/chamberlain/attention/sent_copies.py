"""Doctor: a mailbox whose sends leave two Sent copies (docs/configuration.md, `sent_copy`).

Exchange and Office 365 save their own Sent copy when they accept a message; Thunderbird files
one too. Two copies with one Message-ID in the recent Sent listing suggest `sent_copy: server`.
The check reads one page of recent Sent mail per mailbox and changes nothing.
"""

import logging
from collections import Counter
from datetime import timedelta
from typing import Final

from chamberlain.app.context import Context
from chamberlain.domain.mail import FolderRole, MailQuery
from chamberlain.schema.safelog import log_failure
from chamberlain.schema.tools import CheckResult

LOOKBACK: Final = timedelta(days=14)
PAGE: Final = 100
log = logging.getLogger("chamberlain.attention")


def doubled_sent(ctx: Context, source: str) -> int | None:
    """How many recent sent messages have more than one copy in Sent; None if unreadable."""
    port = ctx.sources.mail_port(source)
    try:
        sent = next((f for f in port.folders(source) if f.role is FolderRole.SENT), None)
        if sent is None:
            return None
        since = ctx.clock.now() - LOOKBACK
        page = port.search(source, MailQuery(folder_id=sent.id, since=since, limit=PAGE))
    except Exception:
        log_failure(log, "could not list Sent for %s", source)
        return None
    counts = Counter(item.internet_message_id for item in page.items if item.internet_message_id)
    return sum(1 for n in counts.values() if n > 1)


def sent_copy_checks(ctx: Context) -> list[CheckResult]:
    doubled: list[str] = []
    for source in sorted(ctx.sources.daily_mail()):
        count = doubled_sent(ctx, source)
        if count:
            doubled.append(f"{source}: {count}")
    if not doubled:
        return [
            CheckResult(
                id="sources.sent_copies",
                status="ok",
                summary="No mailbox shows two Sent copies of one message.",
            )
        ]
    return [
        CheckResult(
            id="sources.sent_copies",
            status="warn",
            summary=(
                "Sent messages with two copies of one Message-ID, by mailbox: " + "; ".join(doubled)
            )[:160],
            fix=(
                "The server probably keeps its own Sent copy. Set `sent_copy: server` on that "
                "mail source in connections.yaml (see docs/configuration.md)."
            ),
        )
    ]
