"""Evidence (M-15) and refs (D-5, D-6, M-20, R4)."""

import json

import pytest
from memory_helpers import add_grant

from chamberlain.app.context import Context
from chamberlain.connectors.fake import FakeProvider
from chamberlain.domain.handles import HandleKind, ref_handle
from chamberlain.domain.mail import MailQuery
from chamberlain.memory import refs, repo
from chamberlain.memory.cards import CardService
from chamberlain.memory.evidence import EvidenceService
from chamberlain.memory.sweeper import sweep
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.errors import ChamberlainError, ErrorCode


@pytest.fixture
def evidence(ctx: Context) -> EvidenceService:
    return EvidenceService(ctx.db, ctx.clock)


def test_m3_m15_evidence_referrers_are_independent_and_the_and_3_related_checks(
    ctx: Context, cards: CardService, evidence: EvidenceService, clock: ManualClock
) -> None:
    # m3_evidence_referrers_are_independent_and_the_last_one_deletes_it
    first = cards.create(title="First thread")
    second = cards.create(title="Second thread")
    shared = evidence.add(text="The user approved the wording.", owner=first.handle, session=None)
    evidence.link(shared, second.handle)
    assert repo.evidence_referrers(ctx.db, shared) == 2
    cards.close(first.handle, None, "Done.")
    assert repo.get_evidence(ctx.db, shared) is not None  # the second thread still needs it
    cards.close(second.handle, None, "Done.")
    assert repo.get_evidence(ctx.db, shared) is None  # deleted at once, no sweep needed

    # m15_forget_drops_one_referrer_then_deletes
    one = cards.create(title="One")
    two = cards.create(title="Two")
    item = evidence.add(text="A fact.", owner=one.handle, session=None)
    evidence.link(item, two.handle)
    assert evidence.release(item, one.handle) == (False, 1)
    assert evidence.release(item, two.handle) == (True, 0)
    assert repo.get_evidence(ctx.db, item) is None

    # m15_expired_source_cannot_become_new_authority
    add_grant(ctx.db, clock, "grant_compacted0001", "spent")
    item = evidence.add(
        text="Approved by the user.", owner="grant_compacted0001", session="ses_aaaaaaaa0001"
    )
    assert evidence.created_in_session(item, "ses_aaaaaaaa0001")
    assert not evidence.created_in_session(item, "ses_aaaaaaaa0002")
    ctx.db.execute(
        "UPDATE grants SET compacted_at = ? WHERE handle = 'grant_compacted0001'",
        ("2026-01-06T00:00:00Z",),
    )
    sweep(ctx, clock.now())
    assert repo.get_evidence(ctx.db, item) is None
    assert not evidence.created_in_session(item, "ses_aaaaaaaa0001")
    with pytest.raises(ChamberlainError) as unknown:
        evidence.link(item, "grant_compacted0001")
    assert unknown.value.code is ErrorCode.REQUEST_UNKNOWN_HANDLE

    # m15_evidence_is_immutable
    card = cards.create(title="Immutable")
    item = evidence.add(text="Original statement.", owner=card.handle, session=None)
    before = repo.get_evidence(ctx.db, item)
    assert before is not None
    assert not hasattr(evidence, "update")
    again = repo.get_evidence(ctx.db, item)
    assert again is not None
    assert tuple(before) == tuple(again)


def test_m15_t10_d6_m20_card_dumps_and_mail_bodies_are_rejected_and_3_related_checks(
    cards: CardService, evidence: EvidenceService, action: str, ctx: Context, fake: FakeProvider
) -> None:
    # m15_card_dumps_and_mail_bodies_are_rejected
    card = cards.create(title="Guarded")
    dump = json.dumps({"title": "x", "goal": "y", "position": "z", "version": 3})
    mail = "From: a@example.org\nTo: b@example.org\nSubject: Hi\nDate: today\n\nBody text."
    quoted = "Thanks.\n\nOn Mon, 5 Jan 2026 at 09:00, Alice wrote:\n> older text"
    for text in (dump, mail, quoted):
        with pytest.raises(ChamberlainError) as refused:
            evidence.add(text=text, owner=card.handle, session=None)
        assert refused.value.code is ErrorCode.EVIDENCE_OVER_CAP
    evidence.add(
        text="User confirmed the delivery date on the phone.", owner=card.handle, session=None
    )

    # t10_evidence_owner_must_be_live_and_of_the_right_kind
    with pytest.raises(ChamberlainError) as bad:
        evidence.add(text="x", owner="ses_aaaaaaaa0001", session=None)
    assert bad.value.code is ErrorCode.ARTIFACT_NO_OWNER
    card = cards.create(title="Closing")
    cards.close(card.handle, None, "Done.")
    with pytest.raises(ChamberlainError) as ended:
        evidence.add(text="x", owner=card.handle, session=None)
    assert ended.value.code is ErrorCode.ARTIFACT_OWNER_ENDED
    evidence.add(text="Action level fact.", owner=action, session=None)

    # d6_ref_handles_are_deterministic_and_never_hold_content
    first = refs.remember(
        ctx.db, ctx.clock, HandleKind.MESSAGE, "mail:test", "f-inbox/<a@b>", "<a@b>"
    )
    again = refs.remember(ctx.db, ctx.clock, HandleKind.MESSAGE, "mail:test", "f-inbox/<a@b>")
    assert first == again == ref_handle(HandleKind.MESSAGE, "mail:test", "f-inbox/<a@b>")
    assert refs.locate(ctx.db, first) == ("mail:test", "f-inbox/<a@b>")
    assert "a@b" not in first
    with pytest.raises(ChamberlainError):
        refs.locate(ctx.db, "msg_00000000000000")

    # m20_physical_copies_of_one_message_keep_distinct_refs
    fake.mail.add_message("mail:test", internet_message_id="<same@x>", folder_id="f-inbox")
    fake.mail.add_message("mail:test", internet_message_id="<same@x>", folder_id="f-archive")
    page = fake.mail.search("mail:test", MailQuery())
    handles = {
        refs.remember(
            ctx.db,
            ctx.clock,
            HandleKind.MESSAGE,
            "mail:test",
            item.locator,
            item.internet_message_id,
        )
        for item in page.items
    }
    assert len(handles) == 2


def test_r4_repoint_keeps_the_handle_across_a_move(ctx: Context, cards: CardService) -> None:
    card = cards.create(title="Tracks a message")
    handle = refs.remember(ctx.db, ctx.clock, HandleKind.MESSAGE, "mail:test", "f-inbox/<m@x>")
    repo.link_card(ctx.db, card.handle, handle, "<m@x>", "2026-01-05T09:00:00Z")
    survivor = refs.repoint(
        ctx.db, ctx.clock, "f-inbox/<m@x>", "f-archive/<m@x>", source="mail:test"
    )
    assert survivor == handle
    assert refs.locate(ctx.db, handle) == ("mail:test", "f-archive/<m@x>")
    assert repo.cards_for_ref(ctx.db, handle) == [card.handle]
    assert refs.repoint(ctx.db, ctx.clock, "nothing/<here>", "x/y") is None


def test_r4_repoint_merges_when_the_new_locator_already_has_a_handle(
    ctx: Context, cards: CardService
) -> None:
    card = cards.create(title="Merged")
    old = refs.remember(ctx.db, ctx.clock, HandleKind.MESSAGE, "mail:test", "f-inbox/<m@x>")
    newer = refs.remember(ctx.db, ctx.clock, HandleKind.MESSAGE, "mail:test", "f-archive/<m@x>")
    repo.link_card(ctx.db, card.handle, newer, None, "2026-01-05T09:00:00Z")
    assert refs.repoint(ctx.db, ctx.clock, "f-inbox/<m@x>", "f-archive/<m@x>") == old
    assert repo.get(ctx.db, "refs", newer) is None
    assert repo.cards_for_ref(ctx.db, old) == [card.handle]
    again = refs.remember(ctx.db, ctx.clock, HandleKind.MESSAGE, "mail:test", "f-archive/<m@x>")
    assert again == old


def test_m3_refs_expire_30_days_after_last_seen_unless_a_live_record_holds_them(
    ctx: Context, cards: CardService, clock: ManualClock
) -> None:
    card = cards.create(title="Holds one ref")
    held = refs.remember(ctx.db, ctx.clock, HandleKind.MESSAGE, "mail:test", "f/<held>")
    loose = refs.remember(ctx.db, ctx.clock, HandleKind.MESSAGE, "mail:test", "f/<loose>")
    repo.link_card(ctx.db, card.handle, held, None, "2026-01-05T09:00:00Z")
    clock.advance(hours=24 * 31)
    sweep(ctx, clock.now())
    assert repo.get(ctx.db, "refs", held) is not None
    assert repo.get(ctx.db, "refs", loose) is None
