"""The `find` tool: handles and one-line hits, never a body.

Contract: docs/contracts/02-tools.md (T-8, T-9) and 06-memory.md (M-17 to M-20).
Remote text appears only inside `content` (E-7). Reads go through the source
router and never change read state (P-3).
"""

from datetime import timedelta

from chamberlain.app.context import Context, Result
from chamberlain.domain.calendar import CalendarQuery
from chamberlain.domain.contacts import ContactQuery
from chamberlain.domain.handles import HandleKind, handle_kind
from chamberlain.domain.mail import Folder, FolderRole, MailQuery, MessageSummary
from chamberlain.memory import refs, repo
from chamberlain.memory.artifacts import ArtifactService
from chamberlain.memory.cards import CardService
from chamberlain.memory.retrieval import (
    SNAPSHOT_MAX,
    Snapshot,
    first_page,
    next_page,
    one_line,
    query_hash,
)
from chamberlain.memory.timeutil import parse
from chamberlain.ports.page import Gap
from chamberlain.schema.envelope import ContentKind, CoverageGap
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.schema.tools import FindRequest, FindResponse, FindScope, Hit

_SOFT_FAILURES = frozenset(
    {ErrorCode.SOURCE_UNAVAILABLE, ErrorCode.SOURCE_BUSY, ErrorCode.SOURCE_STALE}
)
_MAX_MATCHES = 1000


def find(ctx: Context, request: FindRequest) -> Result:
    qhash = query_hash("find", request)
    if request.cursor is not None:
        page = next_page(ctx, "find", qhash, request.cursor, request.limit)
    else:
        snapshot = _within(ctx, request) if request.within else _gather(ctx, request)
        page = first_page(ctx, "find", qhash, snapshot, request.limit)
    return Result(FindResponse(hits=page.hits), coverage=page.coverage)


def _gather(ctx: Context, request: FindRequest) -> Snapshot:
    match request.scope:
        case FindScope.CARDS:
            return _cards(ctx, request)
        case FindScope.STORED:
            return _stored(ctx, request)
        case FindScope.GRANTS:
            return _grants(ctx, request)
        case FindScope.OPERATIONS:
            return _operations(ctx, request)
        case FindScope.MAIL:
            return _mail(ctx, request)
        case FindScope.CALENDAR:
            return _calendar(ctx, request)
        case FindScope.CONTACTS:
            return _contacts(ctx, request)


def _in_window(request: FindRequest, when: str) -> bool:
    if request.since is not None and when < request.since.strftime("%Y-%m-%dT%H:%M:%SZ"):
        return False
    return request.until is None or when <= request.until.strftime("%Y-%m-%dT%H:%M:%SZ")


def _cards(ctx: Context, request: FindRequest) -> Snapshot:
    hits: list[Hit] = []
    for row in repo.list_cards(ctx.db, request.query, SNAPSHOT_MAX):
        if not _in_window(request, str(row["updated_at"])):
            continue
        handle = str(row["handle"])
        title = str(row["title"])
        hits.append(
            Hit(
                handle=handle,
                kind="card",
                when=parse(str(row["updated_at"])),
                summary=f"{row['state']} card, version {row['version']}",
                content=one_line(title, ContentKind.STORED_TEXT, handle),
            )
        )
    return Snapshot(hits=tuple(hits))


def _stored(ctx: Context, request: FindRequest) -> Snapshot:
    found: list[tuple[str, Hit]] = []
    for row in repo.search_artifacts(ctx.db, request.query, SNAPSHOT_MAX):
        handle = str(row["handle"])
        found.append(
            (
                str(row["created_at"]),
                Hit(
                    handle=handle,
                    kind="artifact",
                    when=parse(str(row["created_at"])),
                    summary=f"{row['kind']}, {row['size']} bytes",
                    content=one_line(str(row["reason"]), ContentKind.STORED_TEXT, handle),
                ),
            )
        )
    for row in repo.search_evidence(ctx.db, request.query, SNAPSHOT_MAX):
        handle = str(row["handle"])
        found.append(
            (
                str(row["created_at"]),
                Hit(
                    handle=handle,
                    kind="evidence",
                    when=parse(str(row["created_at"])),
                    summary="evidence",
                    content=one_line(str(row["text"]), ContentKind.STORED_TEXT, handle),
                ),
            )
        )
    for row in repo.search_notes(ctx.db, request.query, SNAPSHOT_MAX):
        handle = str(row["handle"])
        found.append(
            (
                str(row["created_at"]),
                Hit(
                    handle=handle,
                    kind="note",
                    when=parse(str(row["created_at"])),
                    summary="note",
                    content=one_line(str(row["text"]), ContentKind.STORED_TEXT, handle),
                ),
            )
        )
    found = [(when, hit) for when, hit in found if _in_window(request, when)]
    found.sort(key=lambda pair: (pair[0], pair[1].handle), reverse=True)
    return Snapshot(hits=tuple(hit for _, hit in found[:SNAPSHOT_MAX]))


def _grants(ctx: Context, request: FindRequest) -> Snapshot:
    hits = tuple(
        Hit(
            handle=str(row["handle"]),
            kind="grant",
            when=parse(str(row["proposed_at"])),
            summary=f"{row['kind']} grant, {row['state']}",
            content=one_line(str(row["title"]), ContentKind.STORED_TEXT, str(row["handle"])),
        )
        for row in repo.list_grants(ctx.db, request.query, SNAPSHOT_MAX)
        if _in_window(request, str(row["proposed_at"]))
    )
    return Snapshot(hits=hits)


def _operations(ctx: Context, request: FindRequest) -> Snapshot:
    hits = tuple(
        Hit(
            handle=str(row["handle"]),
            kind="operation",
            when=parse(str(row["created_at"])),
            summary=f"{row['kind']}, {row['state']}",
        )
        for row in repo.list_operations(ctx.db, request.query, SNAPSHOT_MAX)
        if _in_window(request, str(row["created_at"]))
    )
    return Snapshot(hits=hits)


def _sources(request: FindRequest, known: list[str]) -> list[str]:
    if request.source is not None:
        return [request.source]
    if not known:
        raise ChamberlainError(ErrorCode.SOURCE_NOT_CONFIGURED, "No source of that kind is set up.")
    return known


def _gap(source: str, reason: str) -> CoverageGap:
    return CoverageGap(source=source, reason=reason)


def _port_gaps(source: str, gaps: tuple[Gap, ...], more: bool) -> list[CoverageGap]:
    """Harness-written gaps: a port's own reason is remote-adjacent text and stays out (E-8)."""
    found = [_gap(g.source, "The source could not look at everything.") for g in gaps]
    if more and not found:
        found.append(_gap(source, "More matches exist than can be listed; narrow the search."))
    return found


def _mail(ctx: Context, request: FindRequest) -> Snapshot:
    found: list[tuple[str, Hit]] = []
    gaps: list[CoverageGap] = []
    for source in _sources(request, sorted(ctx.sources.mail)):
        port = ctx.sources.mail_port(source)
        try:
            folders = {f.id: f for f in port.folders(source)}
            folder_id = None
            if request.folder is not None:
                wanted = [f.id for f in folders.values() if f.role is request.folder]
                if not wanted:
                    gaps.append(_gap(source, "The source has no folder with that role."))
                    continue
                folder_id = wanted[0]
            page = port.search(
                source,
                MailQuery(
                    folder_id=folder_id,
                    text=request.query,
                    since=request.since,
                    until=request.until,
                    unread=request.unread,
                    limit=SNAPSHOT_MAX,
                ),
            )
        except ChamberlainError as error:
            if error.code not in _SOFT_FAILURES:
                raise
            gaps.append(_gap(source, "The source did not answer; nothing was changed."))
            continue
        gaps.extend(_port_gaps(source, page.gaps, page.cursor is not None))
        # One hit per message: copies in several folders share a Message-ID (P-7).
        copies: dict[str, list[MessageSummary]] = {}
        for item in page.items:
            copies.setdefault(item.internet_message_id or item.locator, []).append(item)
        shown = [_representative(group, folders) for group in copies.values()]
        handles = {
            item.locator: refs.remember(
                ctx.db,
                ctx.clock,
                HandleKind.MESSAGE,
                source,
                item.locator,
                item.internet_message_id,
            )
            for item in shown
        }
        suggestions = repo.mail_suggestions(ctx.db, source, list(handles.values()))
        for item in shown:
            handle = handles[item.locator]
            group = copies[item.internet_message_id or item.locator]
            flags = "unread" if any(c.unread for c in group) else "read"
            if item.has_attachments:
                flags += ", with attachments"
            suggestion = suggestions.get(handle)
            if suggestion is not None:
                flags += f"; may belong to card {suggestion[0]}: {suggestion[1][:50]}"
            names = _folder_names(group, folders)
            line = f"{item.sender.address}: {item.subject}"
            if len(names) > 1:
                line += f" [{', '.join(names)}]"
            found.append(
                (
                    item.date.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    Hit(
                        handle=handle,
                        kind="message",
                        when=item.date,
                        summary=flags[:160],
                        content=one_line(line, ContentKind.MAIL_LINE, handle),
                        suggested_card=None if suggestion is None else suggestion[0],
                    ),
                )
            )
    found.sort(key=lambda pair: (pair[0], pair[1].handle), reverse=True)
    return Snapshot(
        hits=tuple(hit for _, hit in found[:SNAPSHOT_MAX]),
        gaps=tuple(gaps),
        incomplete=bool(gaps),
    )


def _representative(group: list[MessageSummary], folders: dict[str, Folder]) -> MessageSummary:
    """The inbox copy when there is one, else the first listed."""
    for item in group:
        folder = folders.get(item.folder_id)
        if folder is not None and folder.role is FolderRole.INBOX:
            return item
    return group[0]


def _folder_names(group: list[MessageSummary], folders: dict[str, Folder]) -> list[str]:
    names: list[str] = []
    for item in group:
        folder = folders.get(item.folder_id)
        name = folder.name if folder is not None else item.folder_id
        if name not in names:
            names.append(name)
    return names


def _calendar(ctx: Context, request: FindRequest) -> Snapshot:
    start = request.since or ctx.clock.now()
    end = request.until or start + timedelta(days=30)
    query = CalendarQuery(start=start, end=end, text=request.query, limit=250)
    found: list[Hit] = []
    gaps: list[CoverageGap] = []
    for source in _sources(request, sorted(ctx.sources.calendar)):
        port = ctx.sources.calendar_port(source)
        try:
            page = port.events(source, query)
        except ChamberlainError as error:
            if error.code not in _SOFT_FAILURES:
                raise
            gaps.append(_gap(source, "The source did not answer; nothing was changed."))
            continue
        gaps.extend(_port_gaps(source, page.gaps, page.cursor is not None))
        for item in page.items:
            handle = refs.remember(
                ctx.db, ctx.clock, HandleKind.CALENDAR_EVENT, source, item.locator
            )
            found.append(
                Hit(
                    handle=handle,
                    kind="calendar_event",
                    when=item.fields.start,
                    summary=item.status.value,
                    content=one_line(item.fields.title, ContentKind.CALENDAR_LINE, handle),
                )
            )
    found.sort(key=lambda hit: (hit.when or start, hit.handle))
    return Snapshot(hits=tuple(found[:SNAPSHOT_MAX]), gaps=tuple(gaps), incomplete=bool(gaps))


def _contacts(ctx: Context, request: FindRequest) -> Snapshot:
    if not request.query:
        raise ChamberlainError(
            ErrorCode.REQUEST_INVALID,
            "Searching contacts needs a query.",
            fix="Give the name or address to look for in query.",
            field="query",
        )
    query = ContactQuery(text=request.query, limit=50)
    found: list[Hit] = []
    gaps: list[CoverageGap] = []
    for source in _sources(request, sorted(ctx.sources.contacts)):
        port = ctx.sources.contacts_port(source)
        try:
            page = port.search(source, query)
        except ChamberlainError as error:
            if error.code not in _SOFT_FAILURES:
                raise
            gaps.append(_gap(source, "The source did not answer; nothing was changed."))
            continue
        gaps.extend(_port_gaps(source, page.gaps, page.cursor is not None))
        for item in page.items:
            handle = refs.remember(ctx.db, ctx.clock, HandleKind.CONTACT, source, item.locator)
            first = item.emails[0] if item.emails else ""
            found.append(
                Hit(
                    handle=handle,
                    kind="contact",
                    summary="contact",
                    content=one_line(f"{item.name} {first}", ContentKind.CONTACT, handle),
                )
            )
    return Snapshot(hits=tuple(found[:SNAPSHOT_MAX]), gaps=tuple(gaps), incomplete=bool(gaps))


# --- searching inside one handle (T-8) ----------------------------------------------------


def _within(ctx: Context, request: FindRequest) -> Snapshot:
    if not request.query:
        raise ChamberlainError(
            ErrorCode.REQUEST_INVALID,
            "Searching inside a handle needs a query.",
            fix="Give the text to look for in query.",
            field="query",
        )
    handle = request.within or ""
    kind = handle_kind(handle)
    gaps: list[CoverageGap] = []
    if kind is HandleKind.CARD:
        view = CardService(ctx.db, ctx.clock).view(handle)
        text = "\n".join((view.title, view.goal, view.position, view.rules))
        content_kind = ContentKind.STORED_TEXT
    elif kind is HandleKind.ARTIFACT:
        text = (
            ArtifactService(ctx.db, ctx.clock, ctx.data_root)
            .read_bytes(handle)
            .decode("utf-8", errors="replace")
        )
        content_kind = ContentKind.STORED_TEXT
    elif kind is HandleKind.MESSAGE:
        source, locator = refs.locate(ctx.db, handle)
        text = ctx.sources.mail_port(source).read(source, locator).body_text
        content_kind = ContentKind.MAIL_BODY
    else:
        raise ChamberlainError(
            ErrorCode.REQUEST_WRONG_KIND,
            "You can search inside a card, a stored item or a message.",
            field="within",
        )
    needle = request.query.casefold()
    hits: list[Hit] = []
    offset = 0
    for line in text.split("\n"):
        if needle in line.casefold():
            if len(hits) >= _MAX_MATCHES:
                raise ChamberlainError(
                    ErrorCode.REQUEST_TOO_LARGE,
                    "Too many lines match.",
                    fix="Search for something more specific.",
                    field="query",
                )
            hits.append(
                Hit(
                    handle=handle,
                    kind="match",
                    offset=offset,
                    content=one_line(line, content_kind, handle),
                )
            )
        offset += len(line) + 1
    return Snapshot(hits=tuple(hits), gaps=tuple(gaps))
