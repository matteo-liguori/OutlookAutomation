"""The owner's read-state options and the standing grant they need.

Contract: docs/contracts/05-grants.md (G-24) and 04-writes.md (W-43). An option in the profile
(`read_state`) never authorises marking mail read by itself. It acts only through a standing grant
for the mailbox with `allow_mark_read`, which the owner confirms with a click in the viewer. The
harness proposes that grant once and never confirms it.
"""

import sqlite3

from chamberlain.domain.canonical import content_hash
from chamberlain.domain.grants import GrantKind, GrantState, StandingScope
from chamberlain.domain.intents import IntentKind
from chamberlain.domain.read_state import ReadStateConfig
from chamberlain.ports.clock import Clock
from chamberlain.store.schema import READ_STATE_KEY, get_state
from chamberlain.writes import repo
from chamberlain.writes.audit import AuditChain
from chamberlain.writes.grants import GrantService
from chamberlain.writes.models import GrantRow, WritePolicy

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


def active_grant(db: sqlite3.Connection, source: str) -> GrantRow | None:
    """A confirmed standing grant that lets this mailbox's mail be marked read."""
    for grant in repo.grants_in_states(db, (GrantState.ACTIVE,), source):
        scope = grant.scope
        if grant.kind is not GrantKind.STANDING or scope is None:
            continue
        if scope.allow_mark_read and IntentKind.MAIL_MARK_READ in scope.intent_kinds:
            return grant
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
