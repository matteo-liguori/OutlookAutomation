"""Looking for a send in the mailbox of a recipient who is also the owner (W-37).

Read-only. The sender's own Sent, Outbox and Drafts are read by the connector's `reconcile`;
this module covers the one place that is not the sender's mailbox: a recipient address that is
an owner address of another configured source, where the message would arrive in the Inbox.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from chamberlain.app.context import Context
from chamberlain.domain.intents import MailSend
from chamberlain.domain.mail import FolderRole, MailQuery, MessageSummary
from chamberlain.domain.operations import ProviderReceipt, Verification
from chamberlain.ports.mail import MailPort
from chamberlain.schema.safelog import log_failure
from chamberlain.writes import repo
from chamberlain.writes.models import OperationRow

log = logging.getLogger("chamberlain.writes")
_SLACK = timedelta(hours=1)
"""A Date header is the sender's clock: look this far before the send."""
_PAGE = 100
"""How many Inbox messages one look reads. A longer Inbox since the send is reported unread."""


@dataclass(frozen=True)
class RecipientLook:
    """What the look found and where it went (place names only, never message text)."""

    receipt: ProviderReceipt | None
    looked: tuple[str, ...]


def _words(text: str) -> str:
    return " ".join(text.split()).casefold()


def sender_places(source: str) -> str:
    return f"Sent, Outbox and Drafts of {source}"


def look_in_recipient_mailboxes(ctx: Context, op: OperationRow, intent: MailSend) -> RecipientLook:
    """Find the message in the Inbox of every other source whose owner address was a recipient."""
    wanted = {a.lower() for a in (*intent.to, *intent.cc, *intent.bcc)}
    senders = repo.own_addresses(ctx.db, intent.source)
    since = (op.dispatched_at or op.created_at) - _SLACK
    looked: list[str] = []
    for source in sorted(ctx.sources.mail):
        if source == intent.source or not (repo.own_addresses(ctx.db, source) & wanted):
            continue
        place = f"Inbox of {source}"
        try:
            found = _search_inbox(ctx, source, intent, op, senders, since)
        except Exception:
            log_failure(log, "recipient mailbox look failed")
            looked.append(f"{place} (could not be read)")
            continue
        if found == "incomplete":
            looked.append(f"{place} (only partly read)")
        elif found:
            return RecipientLook(
                ProviderReceipt(
                    verification=Verification(client_verified=True, server_verified=True)
                ),
                (*looked, place),
            )
        else:
            looked.append(place)
    return RecipientLook(None, tuple(looked))


def _search_inbox(
    ctx: Context,
    source: str,
    intent: MailSend,
    op: OperationRow,
    senders: frozenset[str],
    since: datetime,
) -> bool | str:
    port = ctx.sources.mail_port(source)
    inbox = next((f for f in port.folders(source) if f.role is FolderRole.INBOX), None)
    if inbox is None:
        raise LookupError("no inbox")
    query = MailQuery.model_validate({"folder_id": inbox.id, "since": since, "limit": _PAGE})
    page = port.search(source, query)
    for item in page.items:
        if _is_ours(port, source, item, intent, op, senders):
            return True
    return False if page.complete else "incomplete"


def _is_ours(
    port: MailPort,
    source: str,
    item: MessageSummary,
    intent: MailSend,
    op: OperationRow,
    senders: frozenset[str],
) -> bool:
    if op.reconcile_token is not None and item.internet_message_id == op.reconcile_token:
        return True
    if _words(item.subject) != _words(intent.subject):
        return False
    if senders and item.sender.address.lower() not in senders:
        return False
    body = port.read(source, item.locator).body_text
    return _words(body) == _words(intent.body)
