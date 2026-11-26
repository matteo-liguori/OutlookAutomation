# pyright: reportAttributeAccessIssue=false, reportOptionalMemberAccess=false, reportArgumentType=false, reportUnknownMemberType=false
"""One triage item per source, with suggestions only: the agent decides every card link.

Contract: docs/contracts/07-attention.md. Owner ruling: the harness never writes `card_links`.
"""

from typing import Any

from chamberlain.app.context import Context
from chamberlain.app.core import App
from chamberlain.attention import tick
from chamberlain.connectors.fake import FakeProvider
from chamberlain.ports.clock import ManualClock
from chamberlain.writes.repo import known_recipients

SOURCE = "mail:test"


def link(ctx: Context, card: str, message_id: str) -> None:
    ctx.db.execute(
        "INSERT INTO card_links (card, ref, internet_message_id, created_at) "
        "VALUES (?, ?, ?, '2026-01-01T00:00:00Z')",
        (card, f"msg_{abs(hash(message_id)):020x}"[:24], message_id),
    )


def suggestions(ctx: Context) -> dict[str, str | None]:
    rows = ctx.db.execute("SELECT locator, suggested_card FROM mail_seen")
    return {r["locator"]: r["suggested_card"] for r in rows}


def go(ctx: Context, clock: ManualClock) -> None:
    clock.advance(seconds=61)
    tick.tick(ctx, force=True)


def test_a_many_new_messages_are_one_work_item_per_source_and_1_related_checks(
    ctx: Context, seed: Any, fake: FakeProvider, app: App, clock: ManualClock
) -> None:
    # a_many_new_messages_are_one_work_item_per_source
    for n in range(12):
        fake.mail.add_message(SOURCE, internet_message_id=f"<n{n}@x>", sender=f"s{n}@example.org")
    reply = app.call("start", {})
    mail = [w for w in reply.data.work if w.kind.value.startswith("mail.")]
    assert len(mail) == 1
    assert mail[0].subject == SOURCE
    assert "12 new mail" in mail[0].summary
    assert mail[0].next.tool.value == "find"
    assert mail[0].next.args["source"] == SOURCE
    assert "folder" not in mail[0].next.args, "mail may be filed anywhere, so list every folder"

    # a_a_reply_to_a_linked_message_suggests_that_card
    card = seed.card("Roof repair")
    other = seed.card("Taxes")
    link(ctx, card, "<orig@x>")
    reply = fake.mail.add_message(
        SOURCE, internet_message_id="<r1@x>", in_reply_to="<orig@x>", subject="Re: whatever"
    )
    thread = fake.mail.add_message(
        SOURCE, internet_message_id="<r2@x>", references=("<orig@x>", "<mid@x>"), subject="x"
    )
    unrelated = fake.mail.add_message(SOURCE, internet_message_id="<r3@x>", subject="Roof repair")
    go(ctx, clock)
    found = suggestions(ctx)
    assert found[reply] == card
    assert found[thread] == card
    assert found[unrelated] is None, "a matching subject alone suggests nothing"
    assert other != card


def test_a_a_sender_who_writes_to_exactly_one_card_is_suggested_and_two_cards_is_not(
    ctx: Context, seed: Any, fake: FakeProvider, clock: ManualClock
) -> None:
    one = seed.card("One")
    two = seed.card("Two")
    pairs = ((one, "ann@example.org"), (one, "bob@example.org"), (two, "bob@example.org"))
    for index, (card, to) in enumerate(pairs):
        ctx.db.execute(
            "INSERT INTO operations (handle, idempotency_key, kind, source_id, intent_sha256, "
            "intent_json, card, created_at, updated_at) VALUES (?, ?, 'mail.send', ?, ?, ?, ?, "
            "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
            (f"op_{index:016x}", f"k{index}", SOURCE, "a" * 64, f'{{"to": ["{to}"]}}', card),
        )
    ann = fake.mail.add_message(SOURCE, internet_message_id="<a@x>", sender="ann@example.org")
    bob = fake.mail.add_message(SOURCE, internet_message_id="<b@x>", sender="bob@example.org")
    go(ctx, clock)
    found = suggestions(ctx)
    assert found[ann] == one
    assert found[bob] is None


def test_a_the_harness_never_writes_card_links(
    ctx: Context, seed: Any, fake: FakeProvider, clock: ManualClock, app: App
) -> None:
    card = seed.card("Roof repair")
    link(ctx, card, "<orig@x>")
    ctx.db.execute(
        "CREATE TEMP TRIGGER no_link_insert BEFORE INSERT ON card_links "
        "BEGIN SELECT RAISE(ABORT, 'harness wrote a card link'); END"
    )
    ctx.db.execute(
        "CREATE TEMP TRIGGER no_link_update BEFORE UPDATE ON card_links "
        "BEGIN SELECT RAISE(ABORT, 'harness wrote a card link'); END"
    )
    ctx.db.execute(
        "CREATE TEMP TRIGGER no_link_delete BEFORE DELETE ON card_links "
        "BEGIN SELECT RAISE(ABORT, 'harness wrote a card link'); END"
    )
    fake.mail.add_message(SOURCE, internet_message_id="<r1@x>", in_reply_to="<orig@x>")
    go(ctx, clock)
    reply = app.call("start", {})
    session = str(reply.data.session)
    item = next(w for w in reply.data.work if w.kind.value.startswith("mail."))
    app.call("next", {"session": session, "handled": [item.event]})
    go(ctx, clock)
    assert ctx.db.execute("SELECT COUNT(*) FROM card_links").fetchone()[0] == 1
    assert ctx.db.execute("SELECT COUNT(*) FROM mail_seen").fetchone()[0] == 1


def test_a_mail_that_arrives_after_the_batch_was_shown_and_2_related_checks(
    ctx: Context, seed: Any, fake: FakeProvider, clock: ManualClock, app: App
) -> None:
    # a_mail_that_arrives_after_the_batch_was_shown_surfaces_again_once_it_is_handled
    fake.mail.add_message(SOURCE, internet_message_id="<first@x>")
    reply = app.call("start", {})
    session = str(reply.data.session)
    item = next(w for w in reply.data.work if w.kind.value.startswith("mail."))
    fake.mail.add_message(SOURCE, internet_message_id="<late@x>")
    go(ctx, clock)
    after = app.call("next", {"session": session, "handled": [item.event]})
    go(ctx, clock)
    again = app.call("next", {"session": session})
    mail = [w for w in (*after.data.work, *again.data.work) if w.kind.value.startswith("mail.")]
    assert len(mail) == 1
    assert "1 new mail" in mail[0].summary

    # a_a_handled_batch_does_not_come_back
    fake.mail.add_message(SOURCE, internet_message_id="<first@x>")
    reply = app.call("start", {})
    session = str(reply.data.session)
    item = next(w for w in reply.data.work if w.kind.value.startswith("mail."))
    app.call("next", {"session": session, "handled": [item.event]})
    for _ in range(3):
        go(ctx, clock)
    rows = ctx.db.execute(
        "SELECT COUNT(*) FROM events WHERE kind LIKE 'mail.%' AND state IN ('new', 'delivered')"
    ).fetchone()[0]
    assert rows == 0

    # a_mail_the_owner_sent_teaches_recipients_and_never_becomes_new_mail
    card = seed.card("Roof repair")
    link(ctx, card, "<orig@x>")
    fake.mail.add_message(
        SOURCE,
        folder_id="f-sent",
        internet_message_id="<reply@x>",
        sender="owner@example.org",
        to=("bob@example.org",),
        in_reply_to="<orig@x>",
    )
    fake.mail.add_message(
        SOURCE,
        folder_id="f-sent",
        internet_message_id="<other@x>",
        sender="owner@example.org",
        to=("carol@example.org",),
    )
    fake.mail.add_message(SOURCE, internet_message_id="<in@x>")
    go(ctx, clock)
    learned = known_recipients(ctx.db, SOURCE)
    assert {"bob@example.org", "carol@example.org"} <= learned
    work = app.call("start", {}).data.work
    mail = [w for w in work if w.kind.value == "mail.new_unmatched"]
    assert len(mail) == 2, "one line for the new mail, one quiet line for the reply on a card"
    assert "1 new mail" in mail[0].summary, "sent mail is not counted as new mail"
    assert mail[1].next.args["folder"] == "sent"
    assert suggestions(ctx)["f-sent/<reply@x>"] == card
    assert ctx.db.execute("SELECT COUNT(*) FROM mail_seen WHERE sent = 1").fetchone()[0] == 2
