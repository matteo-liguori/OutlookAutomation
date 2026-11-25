"""Clean-up rules (`cleanup:` in connections.yaml) and what each would move to Trash.

A rule names mailboxes, a folder and an age. `only_unfiled` keeps a message that is also
filed in one of the owner's own folders (a Gmail label) or starred: on Gmail, Trash strips
every label, so such a message would vanish from that folder too.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import Field

from chamberlain.app.context import Context
from chamberlain.domain.mail import Folder, FolderRole, MailQuery, MessageSummary
from chamberlain.ports.mail import MailPort
from chamberlain.schema.base import Model
from chamberlain.schema.errors import ChamberlainError

ALL_MAILBOXES = "*"
_SYSTEM_ROLES = frozenset(
    {
        FolderRole.INBOX,
        FolderRole.ARCHIVE,
        FolderRole.SENT,
        FolderRole.DRAFTS,
        FolderRole.TRASH,
        FolderRole.JUNK,
    }
)


class CleanupRule(Model):
    name: str = Field(min_length=1, max_length=80)
    sources: tuple[str, ...] = (ALL_MAILBOXES,)
    """Mail sources, or `*` for every enabled mailbox."""
    folder: str = Field(min_length=1, max_length=300)
    """`inbox` (the mailbox's Inbox), or a folder path such as `Codex Triage/Newsletters`."""
    older_than_days: int = Field(ge=30, le=36500)
    only_unfiled: bool = False


class CleanupConfig(Model):
    rules: tuple[CleanupRule, ...] = ()
    per_run: int = Field(default=500, ge=1, le=5000)
    """At most this many messages go to Trash in one run; the rest wait for the next."""


@dataclass(frozen=True)
class Candidate:
    rule: str
    source: str
    folder_id: str
    summary: MessageSummary


def find_folder(folders: list[Folder], wanted: str) -> Folder | None:
    if wanted.strip().lower() == "inbox":
        return next((f for f in folders if f.role is FolderRole.INBOX), None)
    path = "/" + wanted.strip().strip("/")
    return next((f for f in folders if f.path.lower() == path.lower()), None)


def own_folders(folders: list[Folder]) -> set[str]:
    """The owner's own folders and labels, plus Starred: a copy there means "keep"."""
    keep = {f.id for f in folders if f.role not in _SYSTEM_ROLES and not f.path.startswith("/[")}
    keep |= {f.id for f in folders if f.path.lower().endswith("/starred")}
    return keep


def sources_of(ctx: Context, rule: CleanupRule) -> list[str]:
    if ALL_MAILBOXES in rule.sources:
        return sorted(ctx.sources.mail)
    return [s for s in rule.sources if s in ctx.sources.mail]


def candidates(ctx: Context, rule: CleanupRule, now: datetime) -> Iterator[Candidate]:
    """Every message the rule would move, oldest folder first. Reads only."""
    cutoff = now - timedelta(days=rule.older_than_days)
    for source in sources_of(ctx, rule):
        port = ctx.sources.mail_port(source)
        try:
            folders = port.folders(source)
        except ChamberlainError:
            continue
        folder = find_folder(folders, rule.folder)
        if folder is None:
            continue
        keep = own_folders(folders) - {folder.id}
        cursor: str | None = None
        while True:
            page = port.search(
                source, MailQuery(folder_id=folder.id, until=cutoff, limit=200), cursor
            )
            for item in page.items:
                if item.date >= cutoff or item.folder_id != folder.id:
                    continue
                if rule.only_unfiled and _filed(port, source, item, keep):
                    continue
                yield Candidate(rule.name, source, folder.id, item)
            if page.cursor is None:
                break
            cursor = page.cursor


def _filed(port: MailPort, source: str, item: MessageSummary, keep: set[str]) -> bool:
    page = port.search(source, MailQuery(internet_message_id=item.internet_message_id, limit=50))
    if not page.complete:
        return True  # unsure: keep it
    return any(copy.folder_id in keep for copy in page.items)


__all__ = [
    "ALL_MAILBOXES",
    "Candidate",
    "CleanupConfig",
    "CleanupRule",
    "candidates",
    "find_folder",
    "own_folders",
]
