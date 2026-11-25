"""`chamberlain export thread`: one clean conversation, copies merged, attachments once."""

import json
import zipfile
from datetime import timedelta

import pytest

from chamberlain.app.context import Context
from chamberlain.connectors.fake import FakeProvider
from chamberlain.export.thread import EXPORTS_DIR, export_thread, parse_ids, trim_quotes
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.errors import ChamberlainError

SOURCE = "mail:test"


def seed(fake: FakeProvider, clock: ManualClock) -> None:
    start = clock.now() - timedelta(days=2)
    report = b"%PDF quarterly report"
    first = {
        "internet_message_id": "<one@x>",
        "subject": "Quarterly plan",
        "sender": "alice@example.org",
        "to": ("owner@example.org",),
        "cc": ("bob@example.org",),
        "body": "Here is the plan.\nSee attached.",
        "date": start,
        "attachments": (("report.pdf", report),),
    }
    fake.mail.add_message(SOURCE, folder_id="f-inbox", **first)  # type: ignore[arg-type]
    fake.mail.add_message(SOURCE, folder_id="f-archive", **first)  # type: ignore[arg-type]
    fake.mail.add_message(
        SOURCE,
        folder_id="f-sent",
        internet_message_id="<two@x>",
        subject="Re: Quarterly plan",
        sender="owner@example.org",
        to=("alice@example.org",),
        body=(
            "Thanks, looks good.\n\nOn Mon, Alice Smith wrote:\n"
            "> Here is the plan.\n> See attached."
        ),
        date=start + timedelta(hours=3),
        attachments=(("report.pdf", report),),
    )


def test_a_thread_exports_once_per_message_in_order_with_people_and_tagged_files(
    ctx: Context, fake: FakeProvider, clock: ManualClock
) -> None:
    seed(fake, clock)
    result = export_thread(ctx, ["two@x", "<one@x>", "one@x", "missing@x"], title="Plan")
    assert result.path.parent == ctx.data_root / EXPORTS_DIR
    assert (result.messages, result.attachments, result.missing) == (2, 1, ["missing@x"])
    with zipfile.ZipFile(result.path) as archive:
        text = archive.read("conversation.txt").decode()
        manifest = json.loads(archive.read("manifest.json"))
        files = [n for n in archive.namelist() if n.startswith("attachments/")]
    assert files == ["attachments/A001 - report.pdf"], "the same file twice is stored once"
    assert text.index("Here is the plan.") < text.index("Thanks, looks good.")
    assert text.count("[A001] report.pdf") == 2
    assert "alice@example.org (wrote 1)" in text
    assert "bob@example.org" in text
    assert "> Here is the plan." not in text, "quoted history is trimmed"
    assert "wrote:" not in text
    assert "Not found in any mailbox:" in text
    assert "missing@x" in text
    assert [m["message_id"] for m in manifest["messages"]] == ["one@x", "two@x"]


def test_quotes_can_be_kept_and_nothing_found_is_an_error(
    ctx: Context, fake: FakeProvider, clock: ManualClock
) -> None:
    seed(fake, clock)
    kept = export_thread(ctx, ["two@x"], keep_quotes=True)
    with zipfile.ZipFile(kept.path) as archive:
        assert "> Here is the plan." in archive.read("conversation.txt").decode()
    with pytest.raises(ChamberlainError):
        export_thread(ctx, ["nowhere@x"])


def test_ids_come_from_arguments_or_file_lines_once_each() -> None:
    assert parse_ids(["<a@x>, b@x", "# a comment", "a@x", "msg_0123456789abcdef0123"]) == [
        "a@x",
        "b@x",
        "msg_0123456789abcdef0123",
    ]


def test_a_top_posted_outlook_reply_keeps_only_its_own_words() -> None:
    body = (
        "Fine by me.\n\n"
        "From: Alice Smith <alice@example.org>\nSent: Monday\nTo: Owner\nSubject: Plan\n\n"
        "Here is the plan."
    )
    assert trim_quotes(body) == "Fine by me."
