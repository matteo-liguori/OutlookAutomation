"""The owner's side of larger card limits: see the requests, approve, decline or take one back.

Contract: docs/contracts/06-memory.md (M-36 to M-38). Kept apart from the other viewer pages so a
merge with other viewer work stays easy. The routes are:

- `GET /api/complex`: pending requests and the cards that hold a larger limit.
- `POST /api/complex/<card>/approve|decline|revoke`: the owner's one-click answers.

They sit behind the viewer's usual gate (token, Origin, JSON body). The agent's tools never reach
this module: only the viewer's WSGI app calls it, so the agent cannot approve its own request.
"""

import re
from typing import Any

from chamberlain.app.context import Context
from chamberlain.limits import COMPLEX_CARD_MAX_CHARS, COMPLEX_POSITION_MAX
from chamberlain.memory import repo as memory
from chamberlain.memory.complex_cards import ComplexCards
from chamberlain.schema.errors import ChamberlainError, ErrorCode

ROUTE = re.compile(r"^/api/complex/(card_[a-z0-9]{8,32})/(approve|decline|revoke)$")


def _line(row: Any) -> dict[str, Any]:
    return {
        "card": str(row["card"]),
        "title": str(row["title"]),
        "reason": str(row["reason"]),
        "requested_at": str(row["requested_at"]),
        "decided_at": None if row["decided_at"] is None else str(row["decided_at"]),
    }


def overview(ctx: Context) -> dict[str, Any]:
    """Waiting requests first, then the cards that now hold a larger limit."""
    return {
        "pending": [_line(r) for r in memory.complex_rows(ctx.db, "pending")],
        "approved": [_line(r) for r in memory.complex_rows(ctx.db, "approved")],
        "position_max": COMPLEX_POSITION_MAX,
        "card_max": COMPLEX_CARD_MAX_CHARS,
    }


def with_complex(ctx: Context, page: dict[str, Any]) -> dict[str, Any]:
    """Mark each card of the cards page that is (or asked to be) complex, and why."""
    states = {
        str(r["card"]): (str(r["state"]), str(r["reason"]))
        for state in ("pending", "approved")
        for r in memory.complex_rows(ctx.db, state)
    }
    for item in page["cards"]:
        found = states.get(item["handle"])
        item["complex"] = None if found is None else {"state": found[0], "reason": found[1]}
    return page


def decide(ctx: Context, path: str) -> dict[str, Any]:
    """Apply one answer. The audit row is written by the service, in the same transaction."""
    match = ROUTE.match(path)
    if match is None:
        raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "No such endpoint.")
    card, action = match.group(1), match.group(2)
    service = ComplexCards(ctx.db, ctx.clock, ctx.services.audit)
    if action == "approve":
        service.approve(card)
        return {"card": card, "state": "approved"}
    if action == "decline":
        service.decline(card)
        return {"card": card, "state": "declined"}
    return {"card": card, "state": "revoked", "over_normal_limit": service.revoke(card)}
