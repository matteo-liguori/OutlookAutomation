"""`open` on a message attachment: text from PDF, Word and plain files (T-9a).

Synthetic files are built here. The fake world only; nothing touches disk.
"""

import io
import zipfile
from typing import Any

import pytest
from memory_helpers import SESSION

from chamberlain.app.core import App
from chamberlain.connectors.fake import FakeProvider
from chamberlain.domain.mail import MailQuery
from chamberlain.limits import MAX_REPLY_CHARS
from chamberlain.memory import attachment_text
from chamberlain.schema.base import Model
from chamberlain.schema.envelope import Reply
from chamberlain.schema.tools import FindResponse, KeepResponse, OpenResponse

pytestmark = pytest.mark.usefixtures("session")


def make_pdf(pages: list[str]) -> bytes:
    """A small valid PDF: one page per entry; an empty string makes a page with no text."""
    objects: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(len(pages)))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    font = 3 + 2 * len(pages)
    for i, text in enumerate(pages):
        content = f"BT /F1 12 Tf 72 700 Td ({text}) Tj ET".encode() if text else b""
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * i} 0 R "
            f"/Resources << /Font << /F1 {font} 0 R >> >> >>".encode()
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    start = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, start)
    )
    return out.getvalue()


def make_docx(paragraphs: list[str], *, doctype: str = "") -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = (
        f'<?xml version="1.0"?>{doctype}<w:document xmlns:w='
        f'"http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>{body}'
        "</w:body></w:document>"
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", xml)
    return out.getvalue()


def call(app: App, **request: Any) -> Reply[Model]:
    reply = app.call("open", {"session": SESSION, **request})
    assert len(reply.model_dump_json(exclude_none=True)) <= MAX_REPLY_CHARS
    return reply


def test_t9a_open_reads_attachment_text_live_without_marking_the_mail_read(
    app: App, fake: FakeProvider, action: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    long_note = "Long line. " * 600  # more than one page of text
    fake.mail.add_message(
        "mail:test",
        internet_message_id="<files@x>",
        body="See the files",
        attachments=(
            ("terms.pdf", make_pdf(["Payment due in thirty days", "", "Page three text"])),
            ("brief.docx", make_docx(["Hello from Word", long_note])),
            ("notes.txt", b"plain \x01 text\n" + b"x" * 10),
            ("photo.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00 binary"),
            ("old.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1 old word"),
            ("evil.docx", make_docx(["x"], doctype='<!DOCTYPE w [<!ENTITY a "b">]>')),
            ("broken.pdf", b"%PDF-1.4 this is not a pdf"),
        ),
    )
    found = call_find(app)
    message = found.hits[0].handle

    def read(attachment: str, **more: Any) -> Reply[Model]:
        return call(app, handle=message, part="body", attachment=attachment, **more)

    pdf = read("a1")
    assert isinstance(pdf.data, OpenResponse)
    assert pdf.data.content is not None
    assert pdf.data.content.kind == "attachment.text"
    assert "Payment due in thirty days" in pdf.data.content.text
    assert "Page three text" in pdf.data.content.text
    info = pdf.data.text_info
    assert info is not None
    assert (info.format, info.pages, info.pages_without_text) == ("pdf", 3, (2,))

    word = read("a2")
    assert isinstance(word.data, OpenResponse)
    assert word.data.content is not None
    assert word.data.range is not None
    assert "Hello from Word" in word.data.content.text
    assert word.data.content.truncated
    second = read("a2", offset=len(word.data.content.text))
    assert isinstance(second.data, OpenResponse)
    assert second.data.range is not None
    assert second.data.range.offset > 0
    assert second.data.range.total == word.data.range.total

    text = read("a3")
    assert isinstance(text.data, OpenResponse)
    assert text.data.content is not None
    assert "\x01" not in text.data.content.text

    for refused in ("a4", "a5", "a6", "a7"):  # image, old .doc, hostile docx, damaged pdf
        assert read(refused).error is not None
    assert read("zz").error is not None
    assert call(app, handle=message, attachment="a1").error is not None  # needs part body

    monkeypatch.setattr(attachment_text, "ATTACHMENT_TEXT_MAX_PAGES", 2)
    cut = read("a1")
    assert isinstance(cut.data, OpenResponse)
    assert cut.data.text_info is not None
    assert cut.data.text_info.cut_short

    # A message read never marks it read, and a card is not a message.
    assert all(fake.mail.is_unread("mail:test", m.locator) for m in fake_messages(fake))
    card = call(app, handle="card_0123456789abcdef", attachment="a1")
    assert card.error is not None

    # The same bytes cached under an owner open the same way.
    kept = app.call(
        "keep",
        {
            "session": SESSION,
            "kind": "artifact",
            "owner": action,
            "reason": "the terms",
            "from_handle": message,
            "attachment": "a1",
        },
    )
    assert isinstance(kept.data, KeepResponse)
    cached = call(app, handle=kept.data.handle, part="body")
    assert isinstance(cached.data, OpenResponse)
    assert cached.data.content is not None
    assert "Payment due" in cached.data.content.text


def call_find(app: App) -> FindResponse:
    reply = app.call("find", {"session": SESSION, "scope": "mail"})
    assert isinstance(reply.data, FindResponse)
    return reply.data


def fake_messages(fake: FakeProvider) -> list[Any]:
    return list(fake.mail.search("mail:test", MailQuery()).items)
