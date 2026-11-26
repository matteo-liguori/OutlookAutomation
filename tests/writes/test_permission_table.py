"""The permission table (05-grants.md G-12): the code, the docs and the viewer text agree."""

import itertools
import re
from pathlib import Path

from chamberlain.domain import permissions
from chamberlain.domain.calendar import EventFields
from chamberlain.domain.grant_rules import requirement_for, stricter
from chamberlain.domain.grants import Requirement as R
from chamberlain.domain.intents import (
    CalendarCreate,
    CalendarDelete,
    CalendarRespond,
    CalendarUpdate,
    MailMarkRead,
    MailSend,
    OutsideAction,
    WriteIntent,
)
from support.write_rig import CALENDAR, WriteRig

DOCS = Path(__file__).resolve().parents[2] / "docs"
CAL = "calendar:a"
HANDLE = "cev_0123456789abcdef"
ADDRESSES = ("a@x.org", "b@y.org", "c@x.org")


def subsets(items: tuple[str, ...]) -> list[tuple[str, ...]]:
    return [c for n in range(len(items) + 1) for c in itertools.combinations(items, n)]


def event(guests: tuple[str, ...]) -> dict[str, object]:
    return {
        "title": "t",
        "start": "2026-02-01T12:00:00Z",
        "end": "2026-02-01T13:00:00Z",
        "attendees": guests,
    }


def every_intent() -> list[WriteIntent]:
    intents: list[WriteIntent] = []
    for to in subsets(ADDRESSES):
        if to:
            intents.append(
                MailSend(source="mail:a", to=to, bcc=("c@x.org",), subject="s", body="b")
            )
            intents.append(MailSend(source="mail:a", to=to, subject="s", body="b"))
    for guests in subsets(ADDRESSES):
        intents.append(CalendarCreate.model_validate({"source": CAL, "event": event(guests)}))
        intents.append(
            CalendarUpdate.model_validate(
                {"source": CAL, "event_ref": HANDLE, "expected_etag": "e", "event": event(guests)}
            )
        )
    intents.append(CalendarDelete(source=CAL, event_ref=HANDLE, expected_etag="e"))
    for organiser in (None, *ADDRESSES):
        intents.append(
            CalendarRespond(
                source=CAL,
                event_ref=HANDLE,
                expected_etag="e",
                response="accepted",
                organiser=organiser,
            )
        )
    intents.append(MailMarkRead(source="mail:a", messages=("msg_0123456789abcdef",)))
    for category in ("form_fill", "download"):
        intents.append(
            OutsideAction.model_validate(
                {
                    "where": "portal",
                    "what": "do it",
                    "category": category,
                    "enters_password": False,
                    "pays": False,
                    "accepts_terms": False,
                }
            )
        )
    return intents


def plain_rule(
    intent: WriteIntent, known: frozenset[str], stored: frozenset[str], owner: frozenset[str]
) -> str:
    """The rule in words: who needs a click, otherwise which setting applies."""
    reached = permissions.recipients_of(intent)
    if isinstance(intent, MailSend):
        return "send_known" if reached <= known else "click"
    if isinstance(intent, CalendarCreate | CalendarUpdate):
        new = reached - stored
        if new and not new <= owner | known:
            return "click"
    return "other"


def test_the_table_decides_every_write_as_the_plain_rule_does() -> None:
    """Every kind of write against known, unknown, owner and already-invited addresses."""
    pools = [frozenset(s) for s in subsets(ADDRESSES)]
    owners = (frozenset(), frozenset({"b@y.org"}), frozenset({"c@x.org", "a@x.org"}))
    for intent in every_intent():
        for known, stored, owner in itertools.product(pools, pools, owners):
            for send_known, other in itertools.product(
                (R.AGENT_EVIDENCE, R.HUMAN_CONFIRMATION), repeat=2
            ):
                got = requirement_for(
                    intent,
                    known,
                    send_known=send_known,
                    other=other,
                    stored_attendees=stored,
                    owner_addresses=owner,
                )
                rule = plain_rule(intent, known, stored, owner)
                want = {
                    "click": R.HUMAN_CONFIRMATION,
                    "send_known": stricter(send_known, R.AGENT_EVIDENCE),
                    "other": stricter(other, R.AGENT_EVIDENCE),
                }[rule]
                assert got is want, (intent, known, stored, owner)


def test_the_docs_show_the_same_table_as_the_code() -> None:
    """permissions.md and the grants contract carry the generated text between markers."""
    for name in ("permissions.md", "contracts/05-grants.md"):
        text = (DOCS / name).read_text(encoding="utf-8")
        found = re.search(
            r"<!-- permission-table -->\n(.*?)\n<!-- /permission-table -->", text, re.S
        )
        assert found is not None, name
        assert found.group(1) == permissions.markdown(), f"{name}: regenerate the table"


def test_the_table_has_a_row_for_outside_actions_and_says_what_is_never_allowed() -> None:
    """One action by the owner's word; standing use is card-bound; three things never."""
    text = permissions.markdown()
    assert "Acting outside Chamberlain's tools" in text
    assert "`outside.action` | One card, one place" in text
    assert "never allowed, whatever is granted" in text
    assert "several calendars, or several mailboxes, with one click" in text


def test_the_viewer_explains_a_click_from_the_table() -> None:
    reason = permissions.click_reason()
    assert "Sending mail" in reason
    assert "invites a new address" in reason


def test_inviting_someone_the_owner_writes_to_in_any_mailbox_needs_only_the_owners_word(
    rig: WriteRig,
) -> None:
    """The table says "someone you have written to": known across every mail source (G-12a).

    Before, the engine looked only under the calendar's own id, so nobody was known for an event.
    """
    friend, stranger = "friend@elsewhere.example", "stranger@elsewhere.example"

    def needs(*guests: str) -> str:
        intent = CalendarCreate(source=rig_calendar, event=EventFields(**event(guests)))  # type: ignore[arg-type]
        return rig.engine().propose_intent(intent).requirement.value

    rig_calendar = CALENDAR
    assert needs(friend) == "human_confirmation"
    rig.know(friend)  # learnt from the mail source's Sent folder, not under the calendar's id
    assert needs(friend) == "agent_evidence"
    assert needs(friend, stranger) == "human_confirmation"
