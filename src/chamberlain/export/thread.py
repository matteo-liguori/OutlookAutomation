"""Export a conversation as one clean text file plus its attachments, in a zip.

An owner utility for use outside the harness (sending a thread to a solicitor, an insurer,
a case file). It only reads mail: nothing is marked read, moved or stored in the harness.
The zip lands in `<data root>/exports/`, and the sweeper deletes exports after
`EXPORT_DAYS`, so nothing builds up.

Improvements on the old harness's export:
- messages are named by Message-ID (or a message handle) and found in any mailbox, so one
  conversation across several of the owner's accounts is one export;
- one message kept once: copies in several folders or mailboxes (Gmail's labels, the
  sender's Sent and the receiver's Inbox) merge by Message-ID;
- everyone in the conversation is listed once at the top, with how often they wrote;
- quoted history ("> ..." and the "On ... wrote:" or Outlook header that introduces it) is
  trimmed, since the earlier messages are in the file in full;
- an attachment sent several times is stored once and keeps one tag `[A001]` everywhere;
- a `manifest.json` names every message and attachment hash, and anything not found.
"""

import hashlib
import json
import re
import zipfile
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Final

from chamberlain.app.context import Context
from chamberlain.domain.handles import HandleKind
from chamberlain.domain.mail import Address, FolderRole, MailQuery, Message
from chamberlain.limits import EXPORT_MAX_BYTES
from chamberlain.schema.errors import ChamberlainError, ErrorCode

EXPORTS_DIR: Final = "exports"
_UNWANTED_COPIES: Final = frozenset({FolderRole.TRASH, FolderRole.DRAFTS, FolderRole.JUNK})
_QUOTE_INTRO: Final = re.compile(
    r"^(on .{4,200}wrote:|le .{4,200}a écrit\s*:"
    r"|am .{4,200}schrieb .{1,100}:|il .{4,200}ha scritto:)\s*$",
    re.IGNORECASE,
)
_OUTLOOK_HEADER: Final = re.compile(
    r"^(-{2,}\s*original message\s*-{2,}|from:\s.+)$", re.IGNORECASE
)
_FILENAME_UNSAFE: Final = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass(frozen=True)
class Copy:
    source: str
    locator: str
    message_id: str


@dataclass
class ExportResult:
    path: Path
    messages: int
    attachments: int
    missing: list[str] = field(default_factory=lambda: list[str]())

    def lines(self) -> list[str]:
        lines = [
            f"Exported {self.messages} message(s) and {self.attachments} attachment(s) to:",
            f"  {self.path}",
            "It is deleted automatically after a week; copy it elsewhere to keep it.",
        ]
        if self.missing:
            lines.append(f"Not found ({len(self.missing)}): " + ", ".join(self.missing))
        return lines


def bare_id(value: str) -> str:
    return value.strip().removeprefix("<").removesuffix(">").strip()


def parse_ids(values: Iterable[str]) -> list[str]:
    """Message-IDs or message handles, from arguments or lines of a file; order kept, once each."""
    found: list[str] = []
    for value in values:
        if value.strip().startswith("#"):
            continue  # a comment line in an id file
        for part in re.split(r"[\s,;]+", value):
            part = part.strip()
            if part:
                found.append(part if part.startswith("msg_") else bare_id(part))
    return list(dict.fromkeys(found))


def _locate(ctx: Context, wanted: str) -> Copy | None:
    """The best copy of one message: a handle names it; a Message-ID is looked up everywhere."""
    if wanted.startswith(f"{HandleKind.MESSAGE.value}_"):
        try:
            source, locator = ctx.services.refs.locate(wanted)
        except ChamberlainError:
            return None
        message = ctx.sources.mail_port(source).read(source, locator)
        return Copy(source, locator, bare_id(message.internet_message_id))
    best: tuple[int, Copy] | None = None
    for source in sorted(ctx.sources.mail):
        port = ctx.sources.mail_port(source)
        try:
            roles = {f.id: f.role for f in port.folders(source)}
            page = port.search(source, MailQuery(internet_message_id=f"<{wanted}>", limit=50))
        except ChamberlainError:
            continue
        for item in page.items:
            if bare_id(item.internet_message_id) != wanted:
                continue
            rank = 1 if roles.get(item.folder_id) in _UNWANTED_COPIES else 0
            if best is None or rank < best[0]:
                best = (rank, Copy(source, item.locator, wanted))
    return None if best is None else best[1]


def trim_quotes(body: str) -> str:
    """The message's own words: quoted history goes, it is in the export already."""
    kept: list[str] = []
    lines = body.replace("\r\n", "\n").split("\n")
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        if _QUOTE_INTRO.match(stripped):
            following = next((x.strip() for x in lines[index + 1 :] if x.strip()), "")
            if following.startswith(">") or not following:
                continue
        if _OUTLOOK_HEADER.match(stripped):
            ahead = [x.strip().lower() for x in lines[index : index + 6]]
            sent = any(x.startswith("sent:") for x in ahead)
            if sent and any(x.startswith("to:") for x in ahead):
                break  # a top-posted reply: everything below is the earlier thread
        kept.append(line)
    text = "\n".join(kept).strip("\n")
    return re.sub(r"\n{3,}", "\n\n", text)


def _person(address: Address) -> str:
    return f"{address.name} <{address.address}>" if address.name else address.address


def _people_line(addresses: Sequence[Address]) -> str:
    return ", ".join(_person(a) for a in addresses)


def _safe_name(name: str) -> str:
    cleaned = _FILENAME_UNSAFE.sub("_", name).strip(" .")
    return cleaned[:120] or "attachment"


def _when(moment: datetime, zone: tzinfo | None) -> str:
    return moment.astimezone(zone).strftime("%a %d %b %Y, %H:%M")


class _Attachments:
    """Each distinct file once, tagged A001, A002, ... in the order it first appears."""

    def __init__(self, archive: zipfile.ZipFile) -> None:
        self._archive = archive
        self.tags: dict[str, str] = {}
        self.listed: list[dict[str, object]] = []
        self.total = 0

    def add(self, name: str, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        if digest in self.tags:
            return self.tags[digest]
        self.total += len(data)
        if self.total > EXPORT_MAX_BYTES:
            raise ChamberlainError(
                ErrorCode.REQUEST_TOO_LARGE,
                "The attachments of this conversation are too large for one export.",
                fix="Export fewer messages at a time.",
            )
        tag = f"A{len(self.tags) + 1:03d}"
        member = f"attachments/{tag} - {_safe_name(name)}"
        self._archive.writestr(member, data, compress_type=zipfile.ZIP_DEFLATED)
        label = f"[{tag}] {name} ({_size(len(data))}) -> {member}"
        self.tags[digest] = label
        self.listed.append({"tag": tag, "name": name, "file": member, "sha256": digest})
        return label


def _size(count: int) -> str:
    if count < 1024:
        return f"{count} bytes"
    if count < 1024 * 1024:
        return f"{count / 1024:.0f} KB"
    return f"{count / (1024 * 1024):.1f} MB"


def export_thread(
    ctx: Context,
    ids: Sequence[str],
    *,
    title: str = "",
    keep_quotes: bool = False,
    zone: tzinfo | None = None,
) -> ExportResult:
    """Read the named messages, merge their copies, and write the zip under `exports/`."""
    ids = parse_ids(ids)
    if not ids:
        raise ChamberlainError(ErrorCode.REQUEST_INVALID, "Name at least one message.")
    copies: dict[str, Copy] = {}
    missing: list[str] = []
    for wanted in ids:
        found = _locate(ctx, wanted)
        if found is None:
            missing.append(wanted)
        elif found.message_id not in copies:
            copies[found.message_id] = found
    messages: list[tuple[Copy, Message]] = []
    for copy in copies.values():
        messages.append((copy, ctx.sources.mail_port(copy.source).read(copy.source, copy.locator)))
    if not messages:
        raise ChamberlainError(
            ErrorCode.REQUEST_UNKNOWN_HANDLE,
            "None of those messages was found in any mailbox.",
            detail=", ".join(missing)[:500],
        )
    messages.sort(key=lambda pair: (pair[1].date, pair[0].message_id))
    first = messages[0][1]
    heading = title.strip() or first.subject.strip() or "Conversation"
    folder = ctx.data_root / EXPORTS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    stamp = ctx.clock.now().strftime("%Y%m%d-%H%M%S")
    path = folder / f"{stamp} {_safe_name(heading)[:80]}.zip"
    staging = path.with_suffix(".zip.part")
    try:
        with zipfile.ZipFile(staging, "w", zipfile.ZIP_DEFLATED) as archive:
            attachments = _Attachments(archive)
            text = _conversation(ctx, heading, messages, attachments, keep_quotes, zone, missing)
            archive.writestr("conversation.txt", text)
            manifest = {
                "title": heading,
                "messages": [
                    {"message_id": c.message_id, "source": c.source, "date": m.date.isoformat()}
                    for c, m in messages
                ],
                "attachments": attachments.listed,
                "not_found": missing,
            }
            archive.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        staging.replace(path)
    finally:
        staging.unlink(missing_ok=True)
    return ExportResult(path, len(messages), len(attachments.tags), missing)


def _conversation(
    ctx: Context,
    heading: str,
    messages: Sequence[tuple[Copy, Message]],
    attachments: _Attachments,
    keep_quotes: bool,
    zone: tzinfo | None,
    missing: Sequence[str],
) -> str:
    wrote: Counter[str] = Counter()
    names: dict[str, str] = {}
    for _, message in messages:
        for person in (message.sender, *message.to, *message.cc):
            key = person.address.lower()
            if person.name and not names.get(key):
                names[key] = person.name
            names.setdefault(key, "")
        wrote[message.sender.address.lower()] += 1
    first, last = messages[0][1].date, messages[-1][1].date
    out = [
        heading,
        "=" * min(len(heading), 78),
        f"{len(messages)} message(s), {_when(first, zone)} to {_when(last, zone)}.",
        "",
        "People in this conversation:",
    ]
    for key in sorted(names, key=lambda k: (-wrote[k], k)):
        who = f"{names[key]} <{key}>" if names[key] else key
        out.append(f"  {who}" + (f" (wrote {wrote[key]})" if wrote[key] else ""))
    subject = ""
    for number, (copy, message) in enumerate(messages, 1):
        port = ctx.sources.mail_port(copy.source)
        out += ["", "-" * 78, f"[{number:03d}] {_when(message.date, zone)}"]
        out.append(f"From: {_person(message.sender)}")
        if message.to:
            out.append(f"To: {_people_line(message.to)}")
        if message.cc:
            out.append(f"Cc: {_people_line(message.cc)}")
        if message.subject.strip() != subject:
            subject = message.subject.strip()
            out.append(f"Subject: {subject}")
        labels = [
            attachments.add(meta.filename, port.attachment(copy.source, copy.locator, meta.id).data)
            for meta in message.attachments
        ]
        if labels:
            out.append("Attachments:")
            out += [f"  {label}" for label in labels]
        body = message.body_text if keep_quotes else trim_quotes(message.body_text)
        out += ["", body.strip() or "(no text)"]
    if missing:
        out += ["", "-" * 78, "Not found in any mailbox:", *(f"  {m}" for m in missing)]
    return "\n".join(out) + "\n"


__all__ = ["EXPORTS_DIR", "ExportResult", "export_thread", "parse_ids", "trim_quotes"]
