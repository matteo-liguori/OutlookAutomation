"""Larger limits for a genuinely demanding card, only with the owner's approval.

Contract: docs/contracts/06-memory.md (M-36 to M-38). The agent can only `request`; approving,
declining and revoking are called by the viewer alone (never from a tool handler), so the agent
cannot grant itself a larger card. Every step writes one audit row.
"""

import sqlite3

from chamberlain.app.services import AuditLog
from chamberlain.domain.cards import card_size
from chamberlain.limits import COMPLEX_CARDS_MAX, COMPLEX_DECLINE_DAYS
from chamberlain.memory import repo
from chamberlain.memory.caps import NORMAL
from chamberlain.memory.cards import CardService
from chamberlain.memory.timeutil import plus, ts
from chamberlain.ports.clock import Clock
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.store.db import transaction

OWNER_ACTOR = "human:viewer"
"""Who the audit log names for an owner's decision (as the viewer's other decisions)."""


def _refuse(message: str, fix: str) -> ChamberlainError:
    return ChamberlainError(ErrorCode.REQUEST_INVALID, message, fix=fix, field="complex_reason")


class ComplexCards:
    def __init__(self, db: sqlite3.Connection, clock: Clock, audit: AuditLog) -> None:
        self._db = db
        self._clock = clock
        self._audit = audit

    # --- the agent's side: asking -------------------------------------------------------

    def request(self, card: str, reason: str, actor: str) -> str:
        """Record a pending request. Returns `pending` (new or already waiting) or `approved`."""
        row = repo.get_card(self._db, card)
        if row is None:
            raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "No such card.")
        if str(row["state"]) != "open":
            raise ChamberlainError(ErrorCode.REQUEST_INVALID, "That card is closed.", field="card")
        now = self._clock.now()
        existing = repo.get_complex(self._db, card)
        if existing is not None:
            state = str(existing["state"])
            if state in ("pending", "approved"):
                return state
            blocked = existing["blocked_until"]
            if blocked is not None and str(blocked) > ts(now):
                raise _refuse(
                    "The owner declined a larger limit for this card on "
                    f"{str(existing['decided_at'])[:10]}.",
                    f"Work within the normal limit. You may ask again after {str(blocked)[:10]}.",
                )
        approved = [str(r["card"]) for r in repo.complex_rows(self._db, "approved")]
        if len(approved) >= COMPLEX_CARDS_MAX:
            raise _refuse(
                f"{COMPLEX_CARDS_MAX} cards already have a larger limit: {', '.join(approved)}.",
                "Work within the normal limit. The owner can take a limit back in the viewer.",
            )
        with transaction(self._db):
            repo.set_complex(
                self._db,
                card=card,
                state="pending",
                reason=reason,
                requested_at=ts(now),
                decided_at=None,
                blocked_until=None,
            )
            self._audit.append(
                actor=actor,
                kind="card.complex_requested",
                subject=card,
                summary=f"Asked the owner for a larger limit on {card}: {reason}"[:300],
            )
        return "pending"

    # --- the owner's side: only the viewer calls these -------------------------------------

    def approve(self, card: str) -> None:
        row = self._pending(card)
        approved = [str(r["card"]) for r in repo.complex_rows(self._db, "approved")]
        if len(approved) >= COMPLEX_CARDS_MAX:
            raise ChamberlainError(
                ErrorCode.REQUEST_INVALID,
                f"{COMPLEX_CARDS_MAX} cards already have a larger limit. Take one back first.",
            )
        with transaction(self._db):
            repo.set_complex(
                self._db,
                card=card,
                state="approved",
                reason=str(row["reason"]),
                requested_at=str(row["requested_at"]),
                decided_at=ts(self._clock.now()),
                blocked_until=None,
            )
            self._audit.append(
                actor=OWNER_ACTOR,
                kind="card.complex_approved",
                subject=card,
                summary=f"Owner approved a larger limit on {card}.",
            )

    def decline(self, card: str) -> None:
        row = self._pending(card)
        now = self._clock.now()
        with transaction(self._db):
            repo.set_complex(
                self._db,
                card=card,
                state="declined",
                reason=str(row["reason"]),
                requested_at=str(row["requested_at"]),
                decided_at=ts(now),
                blocked_until=plus(now, days=COMPLEX_DECLINE_DAYS),
            )
            self._audit.append(
                actor=OWNER_ACTOR,
                kind="card.complex_declined",
                subject=card,
                summary=f"Owner declined a larger limit on {card}.",
            )

    def revoke(self, card: str) -> bool:
        """Back to the normal limits. Returns whether the card is now over them."""
        row = repo.get_complex(self._db, card)
        if row is None or str(row["state"]) != "approved":
            raise ChamberlainError(
                ErrorCode.REQUEST_INVALID, "That card does not have a larger limit."
            )
        with transaction(self._db):
            repo.delete_complex(self._db, card)
            over = self._flag_if_over(card)
            self._audit.append(
                actor=OWNER_ACTOR,
                kind="card.complex_revoked",
                subject=card,
                summary=f"Owner took back the larger limit on {card}"
                + ("; the card is over the normal limit and is due for trimming." if over else "."),
            )
        return over

    def _pending(self, card: str) -> repo.Row:
        row = repo.get_complex(self._db, card)
        if row is None or str(row["state"]) != "pending":
            raise ChamberlainError(ErrorCode.REQUEST_INVALID, "There is no request to decide.")
        return row

    def _flag_if_over(self, card: str) -> bool:
        """The existing over-cap flow takes over: flagged, and due for review now (M-13)."""
        view = CardService(self._db, self._clock).view(card)
        over = (
            view.over_cap or len(view.position) > NORMAL.position or card_size(view) > NORMAL.card
        )
        if over and view.state == "open":
            repo.set_card_review(self._db, card, ts(self._clock.now()), True)
        return over and view.state == "open"
