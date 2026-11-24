"""Cards, actions and notes: bounded records that hold current state only.

Contract: docs/contracts/06-memory.md (Bounded records M-12 to M-16, lifetime
table) and 02-tools.md (`card`, T-12). Updating replaces fields; there is no
append and no history on the card (M-14).
"""

import sqlite3
from typing import Literal

from chamberlain.domain.cards import card_over_cap, card_size
from chamberlain.domain.handles import HandleKind, new_handle
from chamberlain.limits import (
    ACTION_OUTCOME_DAYS,
    ACTION_TITLE_MAX,
    CARD_GOAL_MAX,
    CARD_MAX_CHARS,
    CARD_OPEN_ACTIONS_MAX,
    CARD_OUTCOME_DAYS,
    CARD_RULES_MAX,
    CARD_TITLE_MAX,
    NOTE_MAX,
    NOTES_PER_ACTION_MAX,
    OUTCOME_MAX,
)
from chamberlain.memory import repo
from chamberlain.memory.caps import NORMAL, Caps, caps_for, status_of
from chamberlain.memory.timeutil import parse, plus, ts
from chamberlain.ports.clock import Clock
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.schema.tools import ActionView, CardView, NoteView
from chamberlain.store.db import transaction


def _field_caps(caps: Caps) -> dict[str, int]:
    return {
        "title": CARD_TITLE_MAX,
        "goal": CARD_GOAL_MAX,
        "position": caps.position,
        "rules": CARD_RULES_MAX,
    }


_FIELD_NAMES = ("title", "goal", "position", "rules")


def _over(field: str, size: int, limit: int) -> ChamberlainError:
    return ChamberlainError(
        ErrorCode.CARD_OVER_CAP,
        f"{field} is {size} characters; the limit is {limit}.",
        fix=f"Shorten {field} to {limit} characters; keep current facts only.",
        field=field,
    )


def _complex_state(state: str | None) -> Literal["pending", "approved", "declined"] | None:
    if state == "pending" or state == "approved" or state == "declined":
        return state
    return None


class CardService:
    def __init__(self, db: sqlite3.Connection, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    # --- reading ----------------------------------------------------------------

    def view(self, handle: str) -> CardView:
        row = self._card(handle)
        actions = tuple(self.action_view(a) for a in repo.open_actions(self._db, handle))
        return CardView(
            handle=handle,
            title=str(row["title"]),
            goal=str(row["goal"]),
            position=str(row["position"]),
            rules=str(row["rules"]),
            state="open" if str(row["state"]) == "open" else "closed",
            version=int(row["version"]),
            over_cap=bool(row["over_cap"]),
            review_at=None if row["review_at"] is None else parse(str(row["review_at"])),
            outcome=None if row["outcome"] is None else str(row["outcome"]),
            actions=actions,
            context=repo.card_context_artifact(self._db, handle, ts(self._clock.now())),
            complex=_complex_state(status_of(self._db, handle)),
        )

    def action_view(self, row: repo.Row) -> ActionView:
        notes = tuple(
            NoteView(handle=str(n["handle"]), text=str(n["text"]))
            for n in repo.notes_of(self._db, str(row["handle"]))
        )
        state = str(row["state"])
        return ActionView(
            handle=str(row["handle"]),
            card=str(row["card"]),
            title=str(row["title"]),
            state="open" if state == "open" else ("done" if state == "done" else "dropped"),
            due_at=None if row["due_at"] is None else parse(str(row["due_at"])),
            snooze_until=None if row["snooze_until"] is None else parse(str(row["snooze_until"])),
            outcome=None if row["outcome"] is None else str(row["outcome"]),
            notes=notes,
        )

    def _card(self, handle: str) -> repo.Row:
        row = repo.get_card(self._db, handle)
        if row is None:
            raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "No such card.")
        return row

    def _open_card(self, handle: str, expected_version: int | None) -> repo.Row:
        row = self._card(handle)
        if str(row["state"]) != "open":
            raise ChamberlainError(
                ErrorCode.REQUEST_INVALID,
                "That card is closed.",
                fix="Reopen the card first, then change it.",
                field="card",
            )
        if expected_version is not None and expected_version != int(row["version"]):
            raise ChamberlainError(
                ErrorCode.CARD_VERSION_CONFLICT,
                f"The card is at version {row['version']}, not {expected_version}.",
            )
        return row

    def _action(self, handle: str) -> repo.Row:
        row = repo.get_action(self._db, handle)
        if row is None:
            raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "No such action.")
        return row

    # --- cards --------------------------------------------------------------------

    def create(
        self,
        *,
        title: str,
        goal: str = "",
        position: str = "",
        rules: str = "",
        imported: bool = False,
    ) -> CardView:
        """Create a card. An imported one over a cap is stored flagged (M-13)."""
        fields = {"title": title, "goal": goal, "position": position, "rules": rules}
        flagged = self._fields_over(fields, NORMAL)
        if flagged and not imported:
            name, size, limit = flagged[0]
            raise _over(name, size, limit)
        handle = new_handle(HandleKind.CARD)
        now = self._clock.now()
        draft = CardView(
            handle=handle,
            title=title,
            goal=goal,
            position=position,
            rules=rules,
            state="open",
            version=1,
        )
        over = bool(flagged) or card_over_cap(draft)
        if over and not imported:
            raise _over("card", card_size(draft), CARD_MAX_CHARS)
        with transaction(self._db):
            repo.insert_card(
                self._db,
                handle=handle,
                title=title,
                goal=goal,
                position=position,
                rules=rules,
                over_cap=over,
                review_at=ts(now) if over else None,
                now=ts(now),
            )
        return self.view(handle)

    def update(
        self, handle: str, expected_version: int | None, fields: dict[str, str | None]
    ) -> CardView:
        """Replace the named fields. A card flagged over cap must come under (M-13)."""
        row = self._open_card(handle, expected_version)
        caps = caps_for(self._db, handle)
        merged = {name: str(row[name]) for name in _FIELD_NAMES}
        for name, value in fields.items():
            if value is not None:
                merged[name] = value
        flagged = self._fields_over(merged, caps)
        if flagged:
            name, size, limit = flagged[0]
            raise _over(name, size, limit)
        now = self._clock.now()
        with transaction(self._db):
            repo.update_card(
                self._db,
                handle=handle,
                title=merged["title"],
                goal=merged["goal"],
                position=merged["position"],
                rules=merged["rules"],
                over_cap=False,
                review_at=None,
                now=ts(now),
            )
            view = self.view(handle)
            if card_over_cap(view, caps.card):
                raise _over("card", card_size(view), caps.card)
        return self.view(handle)

    def close(self, handle: str, expected_version: int | None, outcome: str) -> CardView:
        """Compact to the outcome line and delete everything the card held (lifetime table)."""
        self._open_card(handle, expected_version)
        outcome = self._outcome(outcome)
        now = self._clock.now()
        with transaction(self._db):
            actions = repo.action_handles(self._db, handle, open_only=False)
            for action in repo.action_handles(self._db, handle, open_only=True):
                repo.end_action(
                    self._db,
                    action,
                    "dropped",
                    "Closed with the card.",
                    ts(now),
                    plus(now, days=ACTION_OUTCOME_DAYS),
                )
            repo.remove_evidence_refs_of(self._db, [handle, *actions])
            self._release_owned([*actions, handle])
            repo.delete_complex(self._db, handle)
            repo.delete_unreferenced_evidence(self._db)
            repo.delete_card_links(self._db, handle)
            repo.close_card(self._db, handle, outcome, ts(now), plus(now, days=CARD_OUTCOME_DAYS))
        return self.view(handle)

    def reopen(self, handle: str, expected_version: int | None) -> CardView:
        """A reopened card starts empty: nothing cached comes back (lifetime table)."""
        row = self._card(handle)
        if str(row["state"]) != "closed":
            raise ChamberlainError(ErrorCode.REQUEST_INVALID, "That card is already open.")
        if expected_version is not None and expected_version != int(row["version"]):
            raise ChamberlainError(ErrorCode.CARD_VERSION_CONFLICT, "The card has changed.")
        with transaction(self._db):
            repo.reopen_card(self._db, handle, ts(self._clock.now()))
        return self.view(handle)

    # --- actions -----------------------------------------------------------------------

    def add_action(
        self,
        card: str,
        expected_version: int | None,
        *,
        title: str,
        due_at: str | None,
        snooze_until: str | None,
    ) -> CardView:
        self._open_card(card, expected_version)
        if len(title) > ACTION_TITLE_MAX:
            raise ChamberlainError(
                ErrorCode.CARD_OVER_CAP,
                "The action title is too long.",
                fix=f"Shorten the action title to {ACTION_TITLE_MAX} characters.",
                field="title",
            )
        if len(repo.open_actions(self._db, card)) >= CARD_OPEN_ACTIONS_MAX:
            raise ChamberlainError(
                ErrorCode.CARD_OVER_CAP,
                f"The card already has {CARD_OPEN_ACTIONS_MAX} open actions.",
                fix="Finish or merge an open action before adding another.",
                field="actions",
            )
        now = self._clock.now()
        with transaction(self._db):
            repo.insert_action(
                self._db,
                handle=new_handle(HandleKind.ACTION),
                card=card,
                title=title,
                due_at=due_at,
                snooze_until=snooze_until,
                now=ts(now),
            )
            repo.bump_card(self._db, card, ts(now))
            view = self.view(card)
            limit = caps_for(self._db, card).card
            if card_over_cap(view, limit):
                raise _over("card", card_size(view), limit)
        return self.view(card)

    def update_action(
        self,
        action: str,
        expected_version: int | None,
        *,
        title: str | None,
        due_at: str | None,
        snooze_until: str | None,
        set_due: bool,
        set_snooze: bool,
    ) -> CardView:
        """Replace the named fields of an open action."""
        row = self._action(action)
        card = str(row["card"])
        self._open_card(card, expected_version)
        if str(row["state"]) != "open":
            raise ChamberlainError(ErrorCode.REQUEST_INVALID, "That action is finished.")
        now = self._clock.now()
        with transaction(self._db):
            repo.update_action(
                self._db,
                handle=action,
                title=title if title is not None else str(row["title"]),
                due_at=due_at if set_due else row["due_at"],
                snooze_until=snooze_until if set_snooze else row["snooze_until"],
                now=ts(now),
            )
            repo.bump_card(self._db, card, ts(now))
            view = self.view(card)
            limit = caps_for(self._db, card).card
            if card_over_cap(view, limit):
                raise _over("card", card_size(view), limit)
        return self.view(card)

    def finish_action(self, action: str, expected_version: int | None, outcome: str) -> CardView:
        """Finish: notes deleted, holds released, outcome line kept 90 days."""
        row = self._action(action)
        card = str(row["card"])
        self._open_card(card, expected_version)
        if str(row["state"]) != "open":
            raise ChamberlainError(ErrorCode.REQUEST_INVALID, "That action is already finished.")
        outcome = self._outcome(outcome)
        now = self._clock.now()
        with transaction(self._db):
            repo.end_action(
                self._db, action, "done", outcome, ts(now), plus(now, days=ACTION_OUTCOME_DAYS)
            )
            repo.remove_evidence_refs_of(self._db, [action])
            self._release_owned([action])
            repo.delete_unreferenced_evidence(self._db)
            repo.bump_card(self._db, card, ts(now))
        return self.view(card)

    def _release_owned(self, actions: list[str]) -> None:
        """Notes go with their actions; artifacts they held are released (M-10)."""
        for action in actions:
            repo.delete_notes_of(self._db, action)
        for artifact in repo.remove_holds_of(self._db, actions):
            repo.release_if_unheld(self._db, artifact)

    @staticmethod
    def _outcome(outcome: str) -> str:
        if len(outcome) > OUTCOME_MAX:
            raise ChamberlainError(
                ErrorCode.CARD_OVER_CAP,
                "The outcome line is too long.",
                fix=f"Shorten the outcome to {OUTCOME_MAX} characters.",
                field="outcome",
            )
        return outcome

    @staticmethod
    def _fields_over(fields: dict[str, str], caps: Caps) -> list[tuple[str, int, int]]:
        return [
            (name, len(fields[name]), limit)
            for name, limit in _field_caps(caps).items()
            if len(fields[name]) > limit
        ]

    # --- notes ---------------------------------------------------------------------------

    def add_note(self, action: str, text: str) -> str:
        """A note belongs to one open action and dies with it (T-10)."""
        row = repo.get_action(self._db, action)
        if row is None:
            raise ChamberlainError(
                ErrorCode.ARTIFACT_NO_OWNER, "A note belongs to an open action.", field="owner"
            )
        if str(row["state"]) != "open":
            raise ChamberlainError(
                ErrorCode.ARTIFACT_OWNER_ENDED, "That action has finished.", field="owner"
            )
        if len(text) > NOTE_MAX:
            raise ChamberlainError(
                ErrorCode.NOTE_OVER_CAP,
                f"The note is {len(text)} characters; the limit is {NOTE_MAX}.",
                fix=f"Shorten the note to {NOTE_MAX} characters.",
                field="text",
            )
        if repo.count_notes(self._db, action) >= NOTES_PER_ACTION_MAX:
            raise ChamberlainError(
                ErrorCode.NOTE_OVER_CAP,
                f"The action already has {NOTES_PER_ACTION_MAX} notes.",
                fix="Forget an older note on this action, then add the new one.",
                field="owner",
            )
        handle = new_handle(HandleKind.NOTE)
        with transaction(self._db):
            repo.insert_note(self._db, handle, action, text, ts(self._clock.now()))
        return handle

    def forget_note(self, handle: str) -> bool:
        with transaction(self._db):
            return repo.delete_note(self._db, handle) > 0
