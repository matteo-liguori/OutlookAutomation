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
    enabled: bool = False
    """Run the rules by themselves once a day, when an agent calls `start`. Off by default;
    `chamberlain cleanup plan|grant|run` work by hand either way."""
    rules: tuple[CleanupRule, ...] = ()


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


def candidates(
    ctx: Context,
    rule: CleanupRule,
    now: datetime,
    only: str | None = None,
    unsure: set[str] | None = None,
) -> Iterator[Candidate]:
    """Every message the rule would move (in mailbox `only`, if given). Reads only.

    Whole folders are read page by page, never one search per message. A mailbox whose
    keep-folders could not be read completely yields nothing and is added to `unsure`:
    when in doubt, keep everything.
    """
    cutoff = now - timedelta(days=rule.older_than_days)
    for source in sources_of(ctx, rule):
        if only is not None and source != only:
            continue
        port = ctx.sources.mail_port(source)
        try:
            folders = port.folders(source)
        except ChamberlainError:
            continue
        folder = find_folder(folders, rule.folder)
        if folder is None:
            continue
        old = [
            item
            for item in _all(port, source, MailQuery(folder_id=folder.id, until=cutoff, limit=200))
            if item.date < cutoff and item.folder_id == folder.id
        ]
        if not old:
            continue
        kept: set[str] = set()
        if rule.only_unfiled:
            # A copy carries the same date in every folder, so only old mail needs reading
            # (two days' margin for clocks and time zones).
            margin = cutoff + timedelta(days=2)
            found = _ids_in(port, source, own_folders(folders) - {folder.id}, margin)
            if found is None:
                if unsure is not None:
                    unsure.add(source)
                continue
            kept = found
        for item in old:
            if item.internet_message_id not in kept:
                yield Candidate(rule.name, source, folder.id, item)


def _all(port: MailPort, source: str, query: MailQuery) -> Iterator[MessageSummary]:
    cursor: str | None = None
    while True:
        page = port.search(source, query, cursor)
        yield from page.items
        if page.cursor is None:
            return
        cursor = page.cursor


def _ids_in(port: MailPort, source: str, folders: set[str], until: datetime) -> set[str] | None:
    """Message-IDs dated before `until` in these folders, one paged read each; None if any
    read was cut short."""
    ids: set[str] = set()
    for folder in sorted(folders):
        cursor: str | None = None
        while True:
            query = MailQuery(folder_id=folder, until=until, limit=200)
            page = port.search(source, query, cursor)
            if not page.complete:
                return None
            ids |= {item.internet_message_id for item in page.items}
            if page.cursor is None:
                break
            cursor = page.cursor
    return ids


__all__ = [
    "ALL_MAILBOXES",
    "Candidate",
    "CleanupConfig",
    "CleanupRule",
    "candidates",
    "find_folder",
    "own_folders",
]
