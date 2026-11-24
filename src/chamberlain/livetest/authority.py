"""The authority a live test needs: the test card and one standing grant per mailbox.

Contract: docs/live-test.md (Authority). `grant` only PROPOSES. A standing grant is always
confirmed by the owner in the viewer, so this module has no way to confirm anything; the run
only checks that the grants exist and are active, and stops with a clear message if not.
"""

from dataclasses import dataclass

from chamberlain.domain.capabilities import CapabilityName
from chamberlain.domain.grants import GrantState as GrantStateName
from chamberlain.domain.grants import StandingScope
from chamberlain.domain.intents import IntentKind
from chamberlain.domain.mail import Folder, FolderRole
from chamberlain.livetest.lab import Lab, RunStopped, text_of
from chamberlain.livetest.markers import CARD_TITLE, GRANT_TITLE_PREFIX, grant_title
from chamberlain.writes import repo

MAX_PER_DAY = 300
"""Every operation under the grant counts, not only sends: a run uses well under 100."""
_WRITE_KINDS: tuple[tuple[CapabilityName, IntentKind], ...] = (
    (CapabilityName.MAIL_SEND, IntentKind.MAIL_SEND),
    (CapabilityName.MAIL_DRAFT, IntentKind.MAIL_DRAFT_SAVE),
    (CapabilityName.MAIL_DRAFT, IntentKind.MAIL_DRAFT_UPDATE),
    (CapabilityName.MAIL_MOVE, IntentKind.MAIL_MOVE),
    (CapabilityName.MAIL_TRASH, IntentKind.MAIL_TRASH),
    (CapabilityName.MAIL_MARK_READ, IntentKind.MAIL_MARK_READ),
)
_STANDARD_ROLES = frozenset(
    {FolderRole.INBOX, FolderRole.SENT, FolderRole.DRAFTS, FolderRole.TRASH, FolderRole.ARCHIVE}
)


def scratch_folder(folders: list[Folder], configured: str | None) -> Folder | None:
    """The folder the file-and-back scenario uses: the configured one, else the archive."""
    if configured is not None:
        wanted = configured.casefold()
        for folder in folders:
            if wanted in (folder.id.casefold(), folder.name.casefold(), folder.path.casefold()):
                return folder
        return None
    return next((f for f in folders if f.role is FolderRole.ARCHIVE), None)


def standing_scope(lab: Lab, source: str, card: str, kinds: frozenset[IntentKind]) -> StandingScope:
    """The exact scope for one mailbox: the owned addresses, the test card, its own folders."""
    config = lab.config
    folders = lab.env.probe.folders(source)
    ids = {f.id for f in folders if f.role in _STANDARD_ROLES}
    extra = scratch_folder(folders, config.mail[source].scratch_folder)
    if extra is not None:
        ids.add(extra.id)
    return StandingScope(
        source=source,
        intent_kinds=kinds,
        title=grant_title(source),
        summary=(
            "Live test: send to the owner's own addresses, file, mark and trash marked "
            "mail, on the live-test card only."
        ),
        recipients=config.owned_addresses(),
        folders=frozenset(ids),
        allow_mark_read=True,
        allow_duplicate=True,
        card=card,
        max_per_day=MAX_PER_DAY,
    )


def kinds_for(lab: Lab, source: str) -> frozenset[IntentKind]:
    """The write kinds this mailbox can do (a source without a capability omits that kind)."""
    port = lab.app.context.sources.mail_port(source)
    names = set(port.capabilities(source).names())
    return frozenset(kind for name, kind in _WRITE_KINDS if name.value in names)


def ensure_card(lab: Lab) -> str:
    """The persistent test card (found by its exact title, created once)."""
    found = lab.ok("find", scope="cards", query=CARD_TITLE, limit=10)
    for hit in found.get("hits", []):
        if text_of(hit) == CARD_TITLE and str(hit.get("summary", "")).startswith("open"):
            return str(hit["handle"])
    created = lab.ok(
        "card",
        op="create",
        fields={
            "title": CARD_TITLE,
            "goal": "Holds the marked mail and events of live-test runs. Do not use.",
            "rules": "Only the live-test command works here.",
        },
    )
    return str(created["card"]["handle"])


@dataclass(frozen=True)
class GrantState:
    source: str
    state: str
    """`active`, `proposed` (waiting for the owner) or `missing`."""
    handle: str = ""


def grant_state(lab: Lab, source: str, scope: StandingScope) -> GrantState:
    """The best grant whose stored scope is exactly `scope` (same source, same card).

    The scope is compared field by field. Its content hash is not used: a set serialises in
    an order that depends on how it was built, so equal scopes can hash differently.
    """
    found = lab.ok("find", scope="grants", query=grant_title(source), limit=25)
    best = GrantState(source, "missing")
    for hit in found.get("hits", []):
        row = repo.get_grant(lab.app.context.db, str(hit["handle"]))
        if row is None or row.source_id != source or row.scope != scope or row.card != scope.card:
            continue
        if row.state is GrantStateName.ACTIVE:
            return GrantState(source, "active", row.handle)
        if row.state is GrantStateName.PROPOSED and best.state == "missing":
            best = GrantState(source, "proposed", row.handle)
    return best


def start_standing_proposals(lab: Lab, sources: list[str]) -> list[GrantState]:
    """Propose one standing grant per mailbox that has none. Never confirms."""
    lab.card = ensure_card(lab)
    results: list[GrantState] = []
    for source in sources:
        scope = standing_scope(lab, source, lab.card, kinds_for(lab, source))
        current = grant_state(lab, source, scope)
        if current.state == "missing":
            proposed = lab.ok(
                "propose",
                standing=scope.model_dump(mode="json", exclude_none=True),
                card=lab.card,
            )
            current = GrantState(source, "proposed", str(proposed["grant"]))
        results.append(current)
    return results


def require_grants(lab: Lab, sources: list[str]) -> None:
    """Stop the run, with the exact next step, unless every mailbox has an active grant."""
    lab.card = ensure_card(lab)
    missing: list[str] = []
    waiting: list[str] = []
    for source in sources:
        scope = standing_scope(lab, source, lab.card, kinds_for(lab, source))
        state = grant_state(lab, source, scope).state
        if state == "proposed":
            waiting.append(source)
        elif state != "active":
            missing.append(source)
    if missing:
        raise RunStopped(
            f"No confirmed live-test grant for {', '.join(missing)}. "
            "Run `chamberlain live-test grant`, then confirm it in the viewer."
        )
    if waiting:
        raise RunStopped(
            f"The live-test grant for {', '.join(waiting)} is waiting for you. "
            "Open the viewer (`chamberlain viewer`) and confirm it, then run again."
        )


__all__ = [
    "GRANT_TITLE_PREFIX",
    "GrantState",
    "ensure_card",
    "grant_state",
    "kinds_for",
    "require_grants",
    "scratch_folder",
    "standing_scope",
    "start_standing_proposals",
]
