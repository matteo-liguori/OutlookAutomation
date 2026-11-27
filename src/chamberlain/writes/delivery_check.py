"""The standing grant behind the fallback delivery check (docs/permissions.md).

When the normal freshness check says a mailbox is `stale`, the harness may send one marked
message from that mailbox to itself, see it arrive, and move it to Trash. That needs one
standing grant per mailbox: send to the mailbox's own address only, and trash from its Inbox
and Sent folders. The harness proposes the grant once and never confirms it.
"""

import logging
import sqlite3

from chamberlain.app.context import Context
from chamberlain.domain.canonical import content_hash
from chamberlain.domain.grants import GrantKind, GrantState, StandingScope
from chamberlain.domain.intents import IntentKind
from chamberlain.domain.mail import FolderRole
from chamberlain.writes import repo
from chamberlain.writes.grants import GrantService
from chamberlain.writes.models import GrantRow

log = logging.getLogger("chamberlain.writes")

ACTOR = "system:delivery-check"
GRANT_TITLE = "Delivery check: a marked test message to yourself, then to Trash"
MAX_PER_DAY = 50
"""One probe is a send and up to two trashes, at most every 3 hours: far below this."""


def own_address(db: sqlite3.Connection, source: str) -> str | None:
    """The mailbox's own address (lower case), or None when the source names none."""
    found = sorted(repo.own_addresses(db, source))
    return found[0] if found else None


def scope_for(ctx: Context, source: str) -> StandingScope | None:
    """The scope for one mailbox, or None when its address or Inbox/Sent folders are unknown."""
    address = own_address(ctx.db, source)
    if address is None:
        return None
    roles = {f.role: f.id for f in ctx.sources.mail_port(source).folders(source)}
    inbox, sent = roles.get(FolderRole.INBOX), roles.get(FolderRole.SENT)
    if inbox is None or sent is None:
        return None
    return StandingScope(
        source=source,
        intent_kinds=frozenset({IntentKind.MAIL_SEND, IntentKind.MAIL_TRASH}),
        title=GRANT_TITLE,
        summary=(
            "Only when the normal freshness check fails, at most every 3 hours: send a marked "
            "message from this mailbox to itself, see it arrive, then move it to Trash."
        ),
        recipients=frozenset({address}),
        folders=frozenset({inbox, sent}),
        max_per_day=MAX_PER_DAY,
    )


def active_grant(ctx: Context, scope: StandingScope) -> GrantRow | None:
    """The mailbox's active standing grant with exactly this scope. The write engine still
    decides coverage, age and use limits for each send and trash."""
    for grant in repo.grants_in_states(ctx.db, (GrantState.ACTIVE,), scope.source):
        if grant.kind is GrantKind.STANDING and grant.scope == scope:
            return grant
    log.info("No standing delivery-check grant is active for %s", scope.source)
    return None


def propose_missing(ctx: Context, scope: StandingScope) -> str | None:
    """Propose the grant when this mailbox has never had one with this scope, in any state
    (a declined or revoked one is not proposed again). Returns the handle proposed."""
    known = ctx.db.execute(
        "SELECT 1 FROM grants WHERE source_id = ? AND content_sha256 = ?",
        (scope.source, content_hash(scope)),
    ).fetchone()
    if known is not None:
        return None
    service = GrantService(ctx.db, ctx.clock, ctx.services.audit, ctx.write_policy)
    return service.propose_standing(scope, card=None, supersedes=None, actor=ACTOR).handle
