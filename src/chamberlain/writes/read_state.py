"""The owner's read-state options and the standing grant they need.

Contract: docs/contracts/05-grants.md (G-24) and 04-writes.md (W-43). An option in the profile
(`read_state`) never authorises marking mail read by itself. It acts only through a standing grant
for the mailbox with `allow_mark_read`, which the owner confirms with a click in the viewer. The
harness proposes that grant once and never confirms it.
"""

import logging
import sqlite3
from datetime import timedelta

from chamberlain.app.context import Context
from chamberlain.domain.canonical import content_hash
from chamberlain.domain.grant_rules import intent_hash
from chamberlain.domain.grants import GrantKind, GrantState, StandingScope
from chamberlain.domain.intents import IntentKind, MailMarkRead
from chamberlain.domain.read_state import ReadStateConfig
from chamberlain.domain.timefmt import ts
from chamberlain.ports.clock import Clock
from chamberlain.store.schema import READ_STATE_KEY, get_state
from chamberlain.writes import repo
from chamberlain.writes.audit import AuditChain
from chamberlain.writes.authorise import Allow, OperationFacts, Usage, authorise
from chamberlain.writes.grants import GrantService
from chamberlain.writes.models import GrantRow, WritePolicy

log = logging.getLogger("chamberlain.writes")

ACTOR = "system:read_state"


def options(db: sqlite3.Connection) -> ReadStateConfig:
    """The options mirrored at the last start; both off when nothing was mirrored."""
    raw = get_state(db, READ_STATE_KEY)
    if raw is None:
        return ReadStateConfig()
    try:
        return ReadStateConfig.model_validate_json(raw)
    except ValueError:
        return ReadStateConfig()


def scope_for(source: str) -> StandingScope:
    return StandingScope(
        source=source,
        intent_kinds=frozenset({IntentKind.MAIL_MARK_READ}),
        title="Mark mail read after filing and reports",
        summary=(
            "You turned on read_state in your profile. This lets the harness mark filed and "
            "reported mail read in this mailbox. Without it mail stays unread."
        ),
        allow_mark_read=True,
    )


def active_grant(
    ctx: Context,
    intent: MailMarkRead,
    *,
    card: str | None = None,
) -> GrantRow | None:
    """Choose a standing grant by the same coverage, age and usage rules as dispatch."""
    pool = [
        g
        for g in repo.grants_in_states(ctx.db, (GrantState.ACTIVE,), intent.source)
        if g.kind is GrantKind.STANDING and g.scope and g.scope.allow_mark_read
    ]
    now = ctx.clock.now()
    folders: frozenset[str] = frozenset()
    if any(g.scope and g.scope.folders for g in pool):
        port = ctx.sources.mail_port(intent.source)
        folders = frozenset(
            port.folder_of(intent.source, ctx.services.refs.locate(h)[1]) for h in intent.messages
        )
    usage = Usage(
        mail_folders=folders,
        recent_uses=repo.grant_usage(
            ctx.db, [g.handle for g in pool], ts(now - timedelta(hours=24)), ""
        ).recent,
    )
    decision = authorise(
        intent, OperationFacts("", intent_hash(intent), card), pool, usage, ctx.write_policy, now
    )
    if isinstance(decision, Allow):
        return next(g for g in pool if g.handle == decision.grant)
    log.info(
        "No standing read-state grant covers %d message(s) on %s",
        len(intent.messages),
        intent.source,
    )
    return None


def propose_missing(
    db: sqlite3.Connection,
    clock: Clock,
    audit: AuditChain,
    sources: list[str],
    *,
    policy: WritePolicy,
) -> list[str]:
    """Propose the standing grant for each listed mailbox that has never had one proposed.

    Once only: any grant with this exact scope, in any state, means the owner has been asked
    (a declined or revoked one is not proposed again). Returns the handles proposed.
    """
    proposed: list[str] = []
    service = GrantService(db, clock, audit, policy)
    for source in sources:
        scope = scope_for(source)
        known = db.execute(
            "SELECT 1 FROM grants WHERE source_id = ? AND content_sha256 = ?",
            (source, content_hash(scope)),
        ).fetchone()
        if known is not None:
            continue
        grant = service.propose_standing(scope, card=None, supersedes=None, actor=ACTOR)
        proposed.append(grant.handle)
    return proposed
