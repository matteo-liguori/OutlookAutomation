"""The permission table: the one place that says what each write needs.

Two plain tables, read top to bottom:

- `RULES`: what it takes to activate a one-off permission. The first row that fits wins.
- `STANDING`: what a standing permission may cover, per kind of write.

`grant_rules.requirement_for` and `scope_mismatch` read these tables and nothing else.
`docs/permissions.md` and the grants contract (`05-grants.md`) show the same rows between
`<!-- permission-table -->` markers; `markdown()` makes that text and a test keeps the files
in step. The viewer's explanation (`click_reason`) comes from the same rows.

Contract: docs/contracts/05-grants.md (Who may activate a grant, G-12).
Nothing here reads a clock, a store or a connector.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from chamberlain.domain.intents import (
    CalendarCreate,
    CalendarRespond,
    CalendarUpdate,
    IntentKind,
    MailSend,
    OutsideAction,
    WriteIntent,
)


class Needs(StrEnum):
    """Who must say yes."""

    CLICK = "click"
    """You, in the viewer. Config can never loosen it."""
    WORD_SEND_KNOWN = "word:grants.send_known"
    """Your word in chat; config may tighten it to a click."""
    WORD_OTHER = "word:grants.other"
    """Your word in chat; config may tighten it to a click."""


def recipients_of(intent: WriteIntent) -> frozenset[str]:
    """Every address the write would reach (to, cc, bcc; event attendees)."""
    match intent:
        case MailSend():
            return frozenset((*intent.to, *intent.cc, *intent.bcc))
        case CalendarCreate() | CalendarUpdate():
            return frozenset(intent.event.attendees)
        case CalendarRespond():
            return frozenset() if intent.organiser is None else frozenset({intent.organiser})
        case _:
            return frozenset()


@dataclass(frozen=True)
class Facts:
    """What the table asks about a write."""

    intent: WriteIntent
    known: frozenset[str]
    """Recipients the owner has written to from this source."""
    stored: frozenset[str]
    """Guests already on the event an update acts on."""
    owner: frozenset[str]
    """The owner's own addresses."""

    def strangers(self) -> frozenset[str]:
        """Addresses a send reaches that the owner has never written to."""
        return recipients_of(self.intent) - self.known

    def new_guests(self) -> frozenset[str]:
        """Event guests that are new to the event and to the owner."""
        return recipients_of(self.intent) - self.stored - self.owner - self.known


@dataclass(frozen=True)
class Rule:
    situation: str
    needs: Needs
    applies: Callable[[Facts], bool]


STANDING_PERMISSION = Rule("Any standing permission", Needs.CLICK, lambda _facts: False)
"""Not a write: activating a standing permission always needs the click. Shown in the docs."""

RULES: tuple[Rule, ...] = (
    STANDING_PERMISSION,
    Rule(
        "Sending mail to anyone you have not written to before",
        Needs.CLICK,
        lambda f: isinstance(f.intent, MailSend) and bool(f.strangers()),
    ),
    Rule(
        "Creating or changing an event that invites a new address (not one of yours, "
        "not already a guest of the event, not someone you have written to)",
        Needs.CLICK,
        lambda f: isinstance(f.intent, CalendarCreate | CalendarUpdate) and bool(f.new_guests()),
    ),
    Rule(
        "Sending mail only to people you have written to",
        Needs.WORD_SEND_KNOWN,
        lambda f: isinstance(f.intent, MailSend),
    ),
    Rule(
        "Acting outside Chamberlain's tools for you: a form, a portal, a website "
        "(never a password, a payment or accepting terms)",
        Needs.WORD_OTHER,
        lambda f: isinstance(f.intent, OutsideAction),
    ),
    Rule(
        "Everything else: drafts, filing, trash, mark read, other calendar writes",
        Needs.WORD_OTHER,
        lambda _facts: True,
    ),
)


def needs_for(facts: Facts) -> Needs:
    """The first row that fits."""
    return next(rule.needs for rule in RULES if rule.applies(facts))


# --- what a standing permission may cover ---------------------------------------------


@dataclass(frozen=True)
class Cover:
    kinds: tuple[IntentKind, ...]
    covers: str
    exact_recipients: bool = False
    """Reaches people: only addresses the scope lists exactly count."""
    domains: bool = False
    """Reaches people: a listed domain also counts."""
    never: bool = False
    """No standing permission covers it (it acts on an event the scope cannot see)."""
    folders: bool = False
    card_bound: bool = False
    """Only a permission bound to one card may cover it."""


STANDING: tuple[Cover, ...] = (
    Cover(
        (IntentKind.MAIL_SEND,),
        "Exact addresses or whole domains, and a daily limit",
        exact_recipients=True,
        domains=True,
    ),
    Cover(
        (IntentKind.CALENDAR_CREATE, IntentKind.CALENDAR_RESPOND),
        "Exact addresses only, never a domain",
        exact_recipients=True,
    ),
    Cover(
        (IntentKind.CALENDAR_UPDATE, IntentKind.CALENDAR_DELETE),
        "Never covered",
        never=True,
    ),
    Cover((IntentKind.MAIL_MOVE, IntentKind.MAIL_TRASH), "Named folders", folders=True),
    Cover(
        (IntentKind.OUTSIDE_ACTION,),
        "One card, one place and the kinds of action named; never a password, payment or terms",
        card_bound=True,
    ),
    Cover(
        (
            IntentKind.MAIL_MARK_READ,
            IntentKind.MAIL_DRAFT_SAVE,
            IntentKind.MAIL_DRAFT_UPDATE,
            IntentKind.MAIL_DRAFT_DISCARD,
        ),
        "The account and the kind of write",
    ),
)


def cover_for(kind: IntentKind) -> Cover:
    return next(c for c in STANDING if kind in c.kinds)


# --- the text the docs and the viewer show ------------------------------------------------

_NEEDS_WORDS = {
    Needs.CLICK: "Your click in the viewer",
    Needs.WORD_SEND_KNOWN: "Your word in chat (setting `grants.send_known`, may ask for a click)",
    Needs.WORD_OTHER: "Your word in chat (setting `grants.other`, may ask for a click)",
}


NEVER_AND_SEVERAL = (
    "One standing permission can cover several calendars, or several mailboxes, with one click. "
    "Entering a password, paying and accepting terms are never allowed, whatever is granted."
)


def markdown() -> str:
    """The table as the docs show it (between the permission-table markers)."""
    lines = ["| When | What it takes |", "| --- | --- |"]
    lines += [f"| {r.situation} | {_NEEDS_WORDS[r.needs]} |" for r in RULES]
    lines += ["", "| A standing permission for | May cover |", "| --- | --- |"]
    lines += [f"| {', '.join(f'`{k.value}`' for k in c.kinds)} | {c.covers} |" for c in STANDING]
    lines += ["", NEVER_AND_SEVERAL]
    return "\n".join(lines)


def click_reason() -> str:
    """Why a click is needed, for the viewer: every row that asks for one."""
    return "; ".join(r.situation for r in RULES if r.needs is Needs.CLICK)
