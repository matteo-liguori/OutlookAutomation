"""Text out of an attachment's bytes, in memory only.

Contract: docs/contracts/02-tools.md (T-9a). The kind is found from the bytes,
never from the file name or the media type the sender gave. Nothing is written
to disk. The result is untrusted text like a mail body (E-7).
"""

import io
import re
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree

from pypdf import PdfReader

from chamberlain.limits import (
    ATTACHMENT_TEXT_MAX_BYTES,
    ATTACHMENT_TEXT_MAX_CHARS,
    ATTACHMENT_TEXT_MAX_PAGES,
)
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.schema.tools import TextInfo

_WORD_BODY = "word/document.xml"
_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DOCX_XML_MAX = 20 * 1024 * 1024
_ZIP_ENTRIES_MAX = 2_000
_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff￾￿]")
_EMPTY_PAGES_SHOWN = 20


@dataclass(frozen=True)
class Extracted:
    text: str
    info: TextInfo


def readable(media_type: str, data: bytes) -> bool:
    """True when `extract` can read these bytes."""
    return _kind(media_type, data) is not None


def extract(media_type: str, data: bytes) -> Extracted:
    """The text of a PDF, a Word .docx or a plain text file; a plain error otherwise."""
    if len(data) > ATTACHMENT_TEXT_MAX_BYTES:
        raise _unreadable("That attachment is too large to read as text.", fix=_FILE_FIX)
    kind = _kind(media_type, data)
    if kind == "pdf":
        return _pdf(data)
    if kind == "docx":
        return _docx(data)
    if kind == "text":
        return _plain(data)
    raise _unreadable(
        "Only PDF, Word .docx and plain text attachments can be read as text.", fix=_FILE_FIX
    )


_FILE_FIX = "Keep it as an artifact, then open it with part file and lease true for a file path."


def _unreadable(message: str, *, fix: str) -> ChamberlainError:
    return ChamberlainError(ErrorCode.REQUEST_WRONG_KIND, message, fix=fix, field="attachment")


def _kind(media_type: str, data: bytes) -> str | None:
    head = data[:1024]
    if b"%PDF-" in head:
        return "pdf"
    if data[:4] == b"PK\x03\x04":
        return "docx" if _is_docx(data) else None
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" or head.startswith(b"{\\rtf"):
        return None
    if b"\x00" in head:
        return None
    textual = media_type.startswith("text/") or media_type in (
        "application/json",
        "application/xml",
        "",
        "application/octet-stream",
    )
    return "text" if textual else None


def _is_docx(data: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return _WORD_BODY in archive.namelist()
    except (zipfile.BadZipFile, OSError, ValueError):
        return False


def clean(text: str) -> str:
    """Drop control characters and lone surrogates; they bloat the reply when escaped."""
    return _CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))


def _cap(text: str) -> tuple[str, bool]:
    text = clean(text)
    if len(text) > ATTACHMENT_TEXT_MAX_CHARS:
        return text[:ATTACHMENT_TEXT_MAX_CHARS], True
    return text, False


def _plain(data: bytes) -> Extracted:
    text, cut = _cap(data.decode("utf-8", errors="replace"))
    return Extracted(text, TextInfo(format="text", cut_short=cut))


def _pdf(data: bytes) -> Extracted:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise _unreadable(
                "That PDF is password protected.", fix="Ask the owner for an unlocked copy."
            )
        total = len(reader.pages)
        chunks: list[str] = []
        empty: list[int] = []
        used = 0
        read = 0
        cut = total > ATTACHMENT_TEXT_MAX_PAGES
        for number in range(min(total, ATTACHMENT_TEXT_MAX_PAGES)):
            page_text = clean(reader.pages[number].extract_text() or "").strip()
            read += 1
            if not page_text:
                empty.append(number + 1)
                continue
            chunks.append(page_text)
            used += len(page_text)
            if used > ATTACHMENT_TEXT_MAX_CHARS:
                cut = True
                break
    except ChamberlainError:
        raise
    except Exception as error:
        raise _unreadable(
            "That PDF could not be read.", fix="It may be damaged. Ask the owner for another copy."
        ) from error
    text, over = _cap("\n\n".join(chunks))
    info = TextInfo(
        format="pdf",
        pages=total,
        pages_read=read,
        pages_without_text=tuple(empty[:_EMPTY_PAGES_SHOWN]),
        cut_short=cut or over,
    )
    return Extracted(text, info)


def _docx(data: bytes) -> Extracted:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if len(archive.infolist()) > _ZIP_ENTRIES_MAX:
                raise _unreadable("That Word file is not readable.", fix=_FILE_FIX)
            entry = archive.getinfo(_WORD_BODY)
            if entry.file_size > _DOCX_XML_MAX:
                raise _unreadable("That Word file is too large to read as text.", fix=_FILE_FIX)
            xml = archive.read(entry)
        if b"<!DOCTYPE" in xml[:4096].upper() or b"<!ENTITY" in xml.upper():
            raise _unreadable("That Word file is not readable.", fix=_FILE_FIX)
        root = ElementTree.fromstring(xml)
    except ChamberlainError:
        raise
    except Exception as error:
        raise _unreadable(
            "That Word file could not be read.", fix="It may be damaged or an old .doc file."
        ) from error
    lines: list[str] = []
    for paragraph in root.iter(f"{_WORD_NS}p"):
        parts: list[str] = []
        for node in paragraph.iter():
            if node.tag == f"{_WORD_NS}t" and node.text:
                parts.append(node.text)
            elif node.tag == f"{_WORD_NS}tab":
                parts.append("\t")
            elif node.tag in (f"{_WORD_NS}br", f"{_WORD_NS}cr"):
                parts.append("\n")
        lines.append("".join(parts))
    text, cut = _cap("\n".join(lines).strip())
    return Extracted(text, TextInfo(format="docx", cut_short=cut))
