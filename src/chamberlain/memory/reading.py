"""The `open` tool: one handle, one bounded view or range.

Contract: docs/contracts/02-tools.md (`open`, T-6, T-9) and 06-memory.md
(M-9, M-18, M-19). Default is the minimal record view; more is an explicit
`part`. Text written by others goes only into `content` (E-7).
"""

from chamberlain.app.context import Context, Result
from chamberlain.domain.artifacts import ArtifactKind
from chamberlain.domain.events import EventKind
from chamberlain.domain.grants import GrantKind, GrantState, Requirement
from chamberlain.domain.handles import HandleKind, handle_kind
from chamberlain.domain.intents import IntentKind
from chamberlain.domain.mail import FolderRole, Message
from chamberlain.domain.operations import MAY_HAVE_HAPPENED_STATES, OperationState, Verification
from chamberlain.memory import refs, repo
from chamberlain.memory.artifacts import ArtifactService
from chamberlain.memory.cards import CardService
from chamberlain.memory.retrieval import one_line
from chamberlain.memory.timeutil import parse
from chamberlain.schema.envelope import Content, ContentKind
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.schema.tools import (
    ArtifactView,
    AttachmentInfo,
    CalendarEventView,
    ContactView,
    EventState,
    EventView,
    EvidenceView,
    GrantView,
    Lease,
    MessageView,
    OpenPart,
    OpenRequest,
    OpenResponse,
    OperationView,
    Range,
)

_ATTACHMENTS_SHOWN = 10
_TEXT_TYPES = ("text/", "application/json", "application/xml")


def open_handle(ctx: Context, request: OpenRequest) -> Result:
    kind = handle_kind(request.handle)
    match kind:
        case HandleKind.CARD:
            card = CardService(ctx.db, ctx.clock).view(request.handle)
            return Result(OpenResponse(handle=request.handle, view=card))
        case HandleKind.ACTION:
            service = CardService(ctx.db, ctx.clock)
            row = repo.get_action(ctx.db, request.handle)
            if row is None:
                raise _unknown()
            return Result(OpenResponse(handle=request.handle, view=service.action_view(row)))
        case HandleKind.EVIDENCE:
            return Result(_evidence(ctx, request.handle))
        case HandleKind.ARTIFACT:
            return Result(_artifact(ctx, request))
        case HandleKind.GRANT:
            return Result(_grant(ctx, request.handle))
        case HandleKind.OPERATION:
            return Result(_operation(ctx, request.handle))
        case HandleKind.EVENT:
            return Result(_event(ctx, request.handle))
        case HandleKind.MESSAGE:
            return Result(_message(ctx, request))
        case HandleKind.CALENDAR_EVENT:
            return Result(_calendar_event(ctx, request))
        case HandleKind.CONTACT:
            source, _ = refs.locate(ctx.db, request.handle)
            return Result(
                OpenResponse(
                    handle=request.handle,
                    view=ContactView(handle=request.handle, source=source),
                )
            )
        case HandleKind.NOTE | HandleKind.SESSION:
            raise ChamberlainError(
                ErrorCode.REQUEST_WRONG_KIND,
                "That handle has no view of its own.",
                fix="Open the card that holds it.",
                field="handle",
            )


def _unknown() -> ChamberlainError:
    return ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "No such item.")


def _page(text: str, request: OpenRequest, kind: ContentKind) -> tuple[Content, Range]:
    """One bounded range of `text` (M-18)."""
    total = len(text)
    chunk = text[request.offset : request.offset + request.length]
    content = Content(
        origin=request.handle,
        kind=kind,
        text=chunk,
        truncated=request.offset + len(chunk) < total,
    )
    return content, Range(offset=request.offset, length=len(chunk), total=total)


def _evidence(ctx: Context, handle: str) -> OpenResponse:
    row = repo.get_evidence(ctx.db, handle)
    if row is None:
        raise _unknown()
    view = EvidenceView(
        handle=handle,
        text=str(row["text"]),
        ref=None if row["ref"] is None else str(row["ref"]),
        created_at=parse(str(row["created_at"])),
    )
    return OpenResponse(handle=handle, view=view)


def _artifact(ctx: Context, request: OpenRequest) -> OpenResponse:
    store = ArtifactService(ctx.db, ctx.clock, ctx.data_root)
    row = repo.get_artifact(ctx.db, request.handle)
    if row is None:
        raise _unknown()
    owners = tuple(str(o["owner"]) for o in repo.artifact_owners(ctx.db, request.handle))
    state = str(row["state"])
    view = ArtifactView(
        handle=request.handle,
        artifact_kind=ArtifactKind(str(row["kind"])).value,
        media_type=str(row["media_type"]),
        size=int(row["size"]),
        sha256=str(row["sha256"]),
        state="live" if state == "live" else ("released" if state == "released" else "deleted"),
        reason=str(row["reason"]),
        owners=owners,
        expires_at=parse(str(row["expires_at"])),
    )
    if request.part is OpenPart.FILE and request.lease:
        leased = store.lease(request.handle)
        return OpenResponse(
            handle=request.handle,
            view=view,
            lease=Lease(path=leased.path, expires_at=parse(leased.expires_at)),
        )
    if request.part is not OpenPart.BODY:
        return OpenResponse(handle=request.handle, view=view)
    media = str(row["media_type"])
    kind = str(row["kind"])
    if kind not in ("mail_body", "text") and not media.startswith(_TEXT_TYPES):
        raise ChamberlainError(
            ErrorCode.REQUEST_WRONG_KIND,
            "That stored item is a file, not text.",
            fix="Open it with part file and lease true to get a file path.",
            field="part",
        )
    text = store.read_bytes(request.handle).decode("utf-8", errors="replace")
    content_kind = {
        "mail_body": ContentKind.MAIL_BODY,
        "attachment": ContentKind.ATTACHMENT_TEXT,
    }.get(kind, ContentKind.STORED_TEXT)
    content, window = _page(text, request, content_kind)
    return OpenResponse(handle=request.handle, view=view, content=content, range=window)


def _grant(ctx: Context, handle: str) -> OpenResponse:
    row = repo.get_grant(ctx.db, handle)
    if row is None:
        raise _unknown()
    view = GrantView(
        handle=handle,
        grant_kind=GrantKind(str(row["kind"])),
        state=GrantState(str(row["state"])),
        title=str(row["title"])[:80],
        summary=str(row["summary"])[:300],
        source=str(row["source_id"]),
        requirement=Requirement(str(row["requirement"])),
        content_sha256=str(row["content_sha256"]),
        card=None if row["card"] is None else str(row["card"]),
        granted_at=None if row["granted_at"] is None else parse(str(row["granted_at"])),
        expires_at=None if row["expires_at"] is None else parse(str(row["expires_at"])),
        review_at=None if row["review_at"] is None else parse(str(row["review_at"])),
    )
    return OpenResponse(handle=handle, view=view)


def _operation(ctx: Context, handle: str) -> OpenResponse:
    row = repo.get_operation(ctx.db, handle)
    if row is None:
        raise _unknown()
    state = OperationState(str(row["state"]))
    view = OperationView(
        handle=handle,
        intent_kind=IntentKind(str(row["kind"])),
        source=str(row["source_id"]),
        state=state,
        grant=None if row["grant"] is None else str(row["grant"]),
        verification=Verification(
            client_verified=bool(row["client_verified"]),
            server_verified=bool(row["server_verified"]),
        ),
        may_have_happened=state in MAY_HAVE_HAPPENED_STATES,
        error_code=None if row["error_code"] is None else ErrorCode(str(row["error_code"])),
        settled_at=None if row["settled_at"] is None else parse(str(row["settled_at"])),
    )
    return OpenResponse(handle=handle, view=view)


def _event(ctx: Context, handle: str) -> OpenResponse:
    row = repo.get_event(ctx.db, handle)
    if row is None:
        raise _unknown()
    subject = None if row["subject"] is None else str(row["subject"])
    view = EventView(
        handle=handle,
        event_kind=EventKind(str(row["kind"])),
        subject=subject,
        summary=str(row["summary"])[:160],
        state=EventState(str(row["state"])),
        detected_at=parse(str(row["detected_at"])),
    )
    return OpenResponse(handle=handle, view=view)


def _header_text(message: Message) -> str:
    def names(items: tuple[object, ...]) -> str:
        return ", ".join(str(getattr(item, "address", "")) for item in items)

    lines = [
        f"From: {message.sender.address}",
        f"To: {names(message.to)}",
        f"Cc: {names(message.cc)}",
        f"Subject: {message.subject}",
        f"Date: {message.date.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"Message-ID: {message.internet_message_id}",
    ]
    return "\n".join(lines)


def _message(ctx: Context, request: OpenRequest) -> OpenResponse:
    source, locator = refs.locate(ctx.db, request.handle)
    port = ctx.sources.mail_port(source)
    message = port.read(source, locator)
    role = FolderRole.OTHER
    for folder in port.folders(source):
        if folder.id == message.folder_id:
            role = folder.role
    view = MessageView(
        handle=request.handle,
        source=source,
        folder_role=role.value,
        when=message.date,
        unread=message.unread,
        attachment_count=len(message.attachments),
        raw_sha256=message.raw_sha256,
        cards=tuple(repo.cards_for_ref(ctx.db, request.handle)),
    )
    if request.part is OpenPart.BODY:
        content, window = _page(message.body_text, request, ContentKind.MAIL_BODY)
        return OpenResponse(handle=request.handle, view=view, content=content, range=window)
    if request.part is OpenPart.HEADERS:
        content, window = _page(_header_text(message), request, ContentKind.MAIL_HEADERS)
        return OpenResponse(handle=request.handle, view=view, content=content, range=window)
    if request.part is OpenPart.ATTACHMENTS:
        infos = tuple(
            AttachmentInfo(
                id=meta.id,
                media_type=meta.media_type,
                size=meta.size,
                content=one_line(meta.filename, ContentKind.ATTACHMENT_NAME, request.handle),
            )
            for meta in message.attachments[:_ATTACHMENTS_SHOWN]
        )
        return OpenResponse(handle=request.handle, view=view, attachments=infos)
    return OpenResponse(handle=request.handle, view=view)


def _calendar_event(ctx: Context, request: OpenRequest) -> OpenResponse:
    source, locator = refs.locate(ctx.db, request.handle)
    event = ctx.sources.calendar_port(source).read(source, locator)
    view = CalendarEventView(
        handle=request.handle,
        source=source,
        start=event.fields.start,
        end=event.fields.end,
        all_day=event.fields.all_day,
        status=event.status.value,
        etag=event.etag,
        attendee_count=len(event.fields.attendees),
    )
    if request.part is not OpenPart.BODY:
        return OpenResponse(handle=request.handle, view=view)
    fields = event.fields
    text = "\n".join(
        (
            fields.title,
            f"Where: {fields.location}",
            f"Who: {', '.join(fields.attendees)}",
            fields.description,
        )
    )
    content, window = _page(text, request, ContentKind.CALENDAR_EVENT)
    return OpenResponse(handle=request.handle, view=view, content=content, range=window)
