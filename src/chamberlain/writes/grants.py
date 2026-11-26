"""Grant lifecycle: status changes only, never deletion (G-2 to G-11).

A grant's kind, scope, content hash and dates never change once it is
active (G-3); a change is a new grant that supersedes the old one. Reading
or using a grant never renews it (G-7); only `keep` moves `review_at`.
"""

import sqlite3
from collections.abc import Iterable
from datetime import datetime, timedelta

from chamberlain.domain.canonical import canonical_json, content_hash
from chamberlain.domain.grant_rules import anniversary, next_review, suspend_at
from chamberlain.domain.grants import (
    GRANT_TRANSITIONS,
    ConfirmedVia,
    GrantKind,
    GrantState,
    Requirement,
    StandingScope,
)
from chamberlain.domain.handles import HandleKind, new_handle
from chamberlain.domain.operations import OperationState
from chamberlain.domain.timefmt import ts
from chamberlain.limits import (
    GRANT_COMPACT_DAYS,
    ONE_OFF_GRANT_DAYS,
    PROPOSAL_LAPSE_DAYS,
    STANDING_GRANT_MAX_DAYS,
)
from chamberlain.ports.clock import Clock
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.store.db import transaction
from chamberlain.store.migrate import stamp
from chamberlain.writes import repo
from chamberlain.writes.audit import AuditChain
from chamberlain.writes.models import GrantRow, WritePolicy
from chamberlain.writes.transitions import build_receipt, move_operation


class GrantService:
    """Everything that changes a grant, each change audited in the same transaction."""

    def __init__(
        self, db: sqlite3.Connection, clock: Clock, audit: AuditChain, policy: WritePolicy
    ) -> None:
        self._db = db
        self._clock = clock
        self._audit = audit
        self._policy = policy

    # --- creating -----------------------------------------------------------------

    def propose_one_off(
        self,
        *,
        source_id: str,
        intent_sha256: str,
        title: str,
        summary: str,
        requirement: Requirement,
        card: str | None,
        actor: str,
    ) -> GrantRow:
        """A one-off grant bound to exactly one content hash (G-3)."""
        return self._create(
            GrantKind.ONE_OFF,
            source_id=source_id,
            content_sha256=intent_sha256,
            title=title,
            summary=summary,
            requirement=requirement,
            card=card,
            actor=actor,
        )

    def propose_standing(
        self,
        scope: StandingScope,
        *,
        card: str | None,
        supersedes: str | None,
        actor: str,
    ) -> GrantRow:
        """A standing grant always needs the human (G-12)."""
        if supersedes is not None:
            old = self.get(supersedes)
            if old.kind is not GrantKind.STANDING:
                raise ChamberlainError(ErrorCode.REQUEST_WRONG_KIND, "Only a standing grant.")
        return self._create(
            GrantKind.STANDING,
            source_id=scope.source,
            content_sha256=content_hash(scope),
            scope_json=canonical_json(scope),
            title=scope.title,
            summary=scope.summary,
            requirement=Requirement.HUMAN_CONFIRMATION,
            card=card if card is not None else scope.card,
            supersedes=supersedes,
            actor=actor,
        )

    def _create(
        self,
        kind: GrantKind,
        *,
        source_id: str,
        content_sha256: str,
        title: str,
        summary: str,
        requirement: Requirement,
        card: str | None,
        actor: str,
        scope_json: str | None = None,
        supersedes: str | None = None,
    ) -> GrantRow:
        handle = new_handle(HandleKind.GRANT)
        with transaction(self._db):
            repo.insert_grant(
                self._db,
                handle=handle,
                kind=kind,
                source_id=source_id,
                title=title[:80],
                summary=summary[:300],
                requirement=requirement,
                content_sha256=content_sha256,
                scope_json=scope_json,
                card=card,
                supersedes=supersedes,
                proposed_by=actor,
                proposed_at=stamp(self._clock),
            )
            self._audit.append(
                actor=actor,
                kind="grant.proposed",
                subject=handle,
                summary=f"{kind.value} grant proposed; needs {requirement.value}.",
                payload_sha256=content_sha256,
            )
        return self.get(handle)

    # --- reading ------------------------------------------------------------------

    def get(self, handle: str) -> GrantRow:
        grant = repo.get_grant(self._db, handle)
        if grant is None:
            raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "There is no such grant.")
        return grant

    # --- activation ---------------------------------------------------------------

    def activate(
        self,
        handle: str,
        *,
        via: ConfirmedVia,
        actor: str,
        requested_expiry: datetime | None = None,
        recipients: Iterable[str] = (),
    ) -> GrantRow:
        """`proposed -> active`. The caller proved the requirement; this checks it (G-14)."""
        now = self._clock.now()
        with transaction(self._db):
            grant = self.get(handle)
            if grant.state is GrantState.ACTIVE:
                return grant
            if grant.state is not GrantState.PROPOSED:
                raise ChamberlainError(ErrorCode.GRANT_NOT_ACTIVE, "That proposal has ended.")
            if not self._sweep_one(grant, now):
                if grant.requirement is Requirement.HUMAN_CONFIRMATION and (
                    via is not ConfirmedVia.HUMAN_VIEWER
                ):
                    raise ChamberlainError(ErrorCode.GRANT_HUMAN_CONFIRMATION_REQUIRED)
                limit = (
                    ONE_OFF_GRANT_DAYS
                    if grant.kind is GrantKind.ONE_OFF
                    else STANDING_GRANT_MAX_DAYS
                )
                expires = now + timedelta(days=limit)
                if requested_expiry is not None:
                    if requested_expiry <= now:
                        raise ChamberlainError(
                            ErrorCode.REQUEST_INVALID,
                            "The expiry must be in the future.",
                            field="expires_at",
                        )
                    expires = min(expires, requested_expiry)
                review = (
                    anniversary(now, 1, self._policy.tz)
                    if grant.kind is GrantKind.STANDING
                    else None
                )
                moved = repo.update_grant(
                    self._db,
                    grant.handle,
                    GrantState.PROPOSED,
                    state=GrantState.ACTIVE.value,
                    confirmed_via=via.value,
                    granted_at=ts(now),
                    expires_at=ts(expires),
                    review_at=None if review is None else ts(review),
                )
                if not moved:
                    raise ChamberlainError(ErrorCode.GRANT_NOT_ACTIVE, "That proposal changed.")
                self._audit.append(
                    actor=actor,
                    kind="grant.activated",
                    subject=grant.handle,
                    summary=f"{grant.kind.value} grant activated via {via.value}.",
                    payload_sha256=grant.content_sha256,
                )
                if via is ConfirmedVia.HUMAN_VIEWER:
                    covered = grant.scope.all_sources() if grant.scope else {grant.source_id}
                    for source in sorted(covered):
                        for address in recipients:
                            repo.add_known_recipient(
                                self._db, source, address.lower(), grant.handle, stamp(self._clock)
                            )
                if grant.supersedes is not None:
                    old = repo.get_grant(self._db, grant.supersedes)
                    if old is not None and old.state in (GrantState.ACTIVE, GrantState.SUSPENDED):
                        self._end(old, GrantState.REVOKED, actor=actor, reason="superseded")
                return self.get(grant.handle)
        # The lapse and cancellation must commit before reporting the refusal.
        raise ChamberlainError(ErrorCode.GRANT_NOT_ACTIVE, "That proposal has lapsed.")

    # --- ending -------------------------------------------------------------------

    def decline(self, handle: str, *, actor: str) -> GrantRow:
        """`proposed -> declined`; the operation waiting for it is cancelled."""
        with transaction(self._db):
            grant = self.get(handle)
            if grant.state is not GrantState.PROPOSED:
                raise ChamberlainError(
                    ErrorCode.GRANT_NOT_ACTIVE, "Only a proposal can be declined."
                )
            self._end(grant, GrantState.DECLINED, actor=actor, reason="declined")
            self._cancel_waiting(grant, actor)
            return self.get(handle)

    def revoke(self, handle: str, *, actor: str) -> GrantRow:
        """End a grant at the user's or agent's request. Idempotent on an ended grant (G-2)."""
        with transaction(self._db):
            grant = self.get(handle)
            if grant.state is GrantState.PROPOSED:
                self._end(grant, GrantState.DECLINED, actor=actor, reason="withdrawn")
                self._cancel_waiting(grant, actor)
            elif grant.state in (GrantState.ACTIVE, GrantState.SUSPENDED):
                self._end(grant, GrantState.REVOKED, actor=actor, reason="revoked")
            return self.get(handle)

    def keep(self, handle: str, *, actor: str) -> GrantRow:
        """The user's review: move `review_at` to the next anniversary and nothing else (G-7)."""
        now = self._clock.now()
        with transaction(self._db):
            grant = self.get(handle)
            if grant.kind is not GrantKind.STANDING or grant.state not in (
                GrantState.ACTIVE,
                GrantState.SUSPENDED,
            ):
                raise ChamberlainError(ErrorCode.GRANT_NOT_ACTIVE, "Only a live standing grant.")
            if grant.expires_at is not None and grant.expires_at <= now:
                raise ChamberlainError(ErrorCode.GRANT_NOT_ACTIVE, "That grant has expired.")
            following = next_review(grant.granted_at, now, self._policy.tz)
            moved = repo.update_grant(
                self._db,
                handle,
                grant.state,
                state=GrantState.ACTIVE.value,
                review_at=ts(following),
                last_reviewed_at=ts(now),
            )
            if not moved:
                raise ChamberlainError(ErrorCode.GRANT_NOT_ACTIVE, "That grant changed.")
            self._audit.append(
                actor=actor,
                kind="grant.kept",
                subject=handle,
                summary="Review done; the grant was kept unchanged.",
                payload_sha256=grant.content_sha256,
            )
            return self.get(handle)

    def mark_spent(self, handle: str, *, actor: str) -> None:
        """A one-off whose operation is confirmed becomes `spent` (G table, G-4)."""
        with transaction(self._db):
            grant = self.get(handle)
            if grant.kind is GrantKind.ONE_OFF and grant.state is GrantState.ACTIVE:
                self._end(grant, GrantState.SPENT, actor=actor, reason="spent")

    def _end(self, grant: GrantRow, to: GrantState, *, actor: str, reason: str) -> None:
        if to not in GRANT_TRANSITIONS[grant.state]:
            raise ChamberlainError(ErrorCode.INTERNAL_UNEXPECTED, "Illegal grant transition.")
        moved = repo.update_grant(
            self._db, grant.handle, grant.state, state=to.value, ended_at=stamp(self._clock)
        )
        if not moved:
            raise ChamberlainError(ErrorCode.GRANT_NOT_ACTIVE, "That grant changed.")
        self._audit.append(
            actor=actor,
            kind=f"grant.{to.value}",
            subject=grant.handle,
            summary=f"Grant {reason}.",
            payload_sha256=grant.content_sha256,
        )

    def _cancel_waiting(self, grant: GrantRow, actor: str) -> None:
        """The proposed operation a one-off was made for is cancelled with it."""
        if grant.kind is not GrantKind.ONE_OFF:
            return
        op = repo.operation_for_grant_hash(self._db, grant.source_id, grant.content_sha256)
        if op is None:
            return
        now = stamp(self._clock)
        move_operation(
            self._db,
            self._clock,
            self._audit,
            op,
            OperationState.CANCELLED,
            actor=actor,
            summary="The proposal ended without permission.",
            receipt_json=build_receipt(op, OperationState.CANCELLED, now),
            error_code=ErrorCode.GRANT_NOT_ACTIVE.value,
        )

    # --- time-driven changes (the sweep) -------------------------------------------

    def sweep(self) -> int:
        """Lapse, expire, suspend, close card-bound grants, compact. Returns changes made."""
        now = self._clock.now()
        changed = 0
        with transaction(self._db):
            for grant in repo.grants_in_states(
                self._db, (GrantState.PROPOSED, GrantState.ACTIVE, GrantState.SUSPENDED)
            ):
                changed += self._sweep_one(grant, now)
            before = ts(now - timedelta(days=GRANT_COMPACT_DAYS))
            for handle in repo.compactable_grants(self._db, before):
                grant = self.get(handle)
                repo.compact_grant(self._db, handle, stamp(self._clock), grant.title)
                changed += 1
        return changed

    def _sweep_one(self, grant: GrantRow, now: datetime) -> int:
        if grant.state is GrantState.PROPOSED:
            if now >= grant.proposed_at + timedelta(days=PROPOSAL_LAPSE_DAYS):
                self._end(grant, GrantState.LAPSED, actor="system:tick", reason="lapsed")
                self._cancel_waiting(grant, "system:tick")
                return 1
            return 0
        if grant.card is not None and repo.card_state(self._db, grant.card) == "closed":
            self._end(grant, GrantState.REVOKED, actor="system:tick", reason="card closed")
            return 1
        if grant.expires_at is not None and grant.expires_at <= now:
            self._end(grant, GrantState.EXPIRED, actor="system:tick", reason="expired")
            return 1
        if (
            grant.state is GrantState.ACTIVE
            and grant.kind is GrantKind.STANDING
            and grant.review_at is not None
            and now >= suspend_at(grant.review_at)
        ):
            moved = repo.update_grant(
                self._db, grant.handle, GrantState.ACTIVE, state=GrantState.SUSPENDED.value
            )
            if moved:
                self._audit.append(
                    actor="system:tick",
                    kind="grant.suspended",
                    subject=grant.handle,
                    summary="Review overdue; the grant authorises nothing until kept.",
                    payload_sha256=grant.content_sha256,
                )
                return 1
        return 0
