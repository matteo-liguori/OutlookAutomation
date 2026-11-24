"""The batch's next call lists every new message with its suggested card; the agent links."""

import pytest
from tests.integration.conftest import MAIL, Stack

from chamberlain.domain.handles import HandleKind


@pytest.mark.parametrize("count", [1, 30])
def test_the_triage_item_lists_new_mail_with_the_suggested_card_and_its_title(
    stack: Stack, count: int
) -> None:
    stack.session = stack.start()["session"]
    card = stack.ok("card", op="create", fields={"title": "Roof repair"})["card"]["handle"]
    locator = stack.fake.mail.add_message(
        MAIL, internet_message_id="<orig@x>", sender="orig@example.org"
    )
    handle = stack.app.context.services.refs.remember(HandleKind.MESSAGE, MAIL, locator, "<orig@x>")
    stack.app.context.db.execute(
        "INSERT INTO card_links (card, ref, internet_message_id, created_at) "
        "VALUES (?, ?, '<orig@x>', '2026-01-01T00:00:00Z')",
        (card, handle),
    )
    stack.fake.mail.add_message(
        MAIL, internet_message_id="<r@x>", in_reply_to="<orig@x>", sender="new@example.org"
    )
    for n in range(count - 1):
        stack.fake.mail.add_message(
            MAIL, internet_message_id=f"<o{n}@x>", sender=f"o{n}@example.org"
        )
    stack.clock.advance(minutes=5)
    work = stack.ok("next")["work"]
    item = next(w for w in work if w["kind"].startswith("mail."))
    listed = stack.ok("find", **item["next"]["args"])["hits"]
    suggested = [h for h in listed if h.get("suggested_card")]
    assert suggested, "the reply to a linked message carries its card"
    assert {h["suggested_card"] for h in suggested} == {card}
    assert all("Roof repair" in h["summary"] for h in suggested)
