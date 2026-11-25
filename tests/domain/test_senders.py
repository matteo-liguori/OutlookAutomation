"""Authorised senders and the provider's Authentication-Results (domain/senders.py)."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from chamberlain.domain.senders import (
    DEFAULT_AUTHSERV_IDS,
    NO_AUTHSERV_ID,
    AuthorisedSenders,
    Refusal,
    check_authentication,
    evaluate,
    instruction_ref,
    parse_instruction_ref,
)

NOW = datetime(2026, 11, 24, 12, tzinfo=UTC)
TRUSTED = DEFAULT_AUTHSERV_IDS


def passed(header: str, sender: str = "me@example.org", trusted: tuple[str, ...] = TRUSTED) -> bool:
    return check_authentication((header,), sender, trusted).passed


def test_dkim_pass_needs_a_signing_domain_aligned_with_from() -> None:
    assert passed("mx.google.com; dkim=pass header.d=example.org")
    assert passed("mx.google.com; dkim=pass header.d=mail.example.org")
    assert passed("mx.google.com; dkim=pass header.d=example.org", "me@news.example.org")
    assert not passed("mx.google.com; dkim=pass header.d=evil.example")
    assert not passed("mx.google.com; dkim=pass header.d=org")
    assert not passed("mx.google.com; dkim=pass")


def test_dmarc_pass_needs_the_same_from_domain() -> None:
    assert passed("mx.google.com; dmarc=pass (p=NONE) header.from=example.org")
    assert not passed("mx.google.com; dmarc=pass header.from=evil.example")
    assert not passed("mx.google.com; dmarc=pass (p=NONE)"), "a pass that names no domain"
    assert not passed("mx.google.com; dmarc=fail header.from=example.org; spf=pass")


def test_only_the_topmost_header_is_read() -> None:
    forged = "mx.google.com; dkim=pass header.d=example.org"
    real = "mx.google.com; dkim=fail header.d=example.org"
    assert not check_authentication((real, forged), "me@example.org", TRUSTED).passed
    assert check_authentication((forged, real), "me@example.org", TRUSTED).passed


def test_the_authenticating_server_must_be_trusted_and_none_is_opt_in() -> None:
    assert not passed("attacker.example; dkim=pass header.d=example.org")
    assert passed("mail-x.protection.outlook.com; dkim=pass header.d=example.org")
    exchange = "spf=pass smtp.mailfrom=example.org; dkim=pass header.d=example.org"
    assert not passed(exchange)
    assert passed(exchange, trusted=(NO_AUTHSERV_ID,))


def test_no_header_or_an_unreadable_sender_fails() -> None:
    assert not check_authentication((), "me@example.org", TRUSTED).passed
    assert not passed(
        "mx.google.com; dkim=pass header.d=unknown.invalid", "unknown@unknown.invalid"
    )


def test_the_config_rejects_unknown_scopes_and_duplicate_addresses() -> None:
    with pytest.raises(ValidationError):
        AuthorisedSenders.model_validate(
            {"others": [{"address": "pa@example.net", "scope": ["mail.explode"]}]}
        )
    with pytest.raises(ValidationError):
        AuthorisedSenders.model_validate(
            {"others": [{"address": "pa@example.net", "scope": ["ask_only", "mail.move"]}]}
        )
    with pytest.raises(ValidationError):
        AuthorisedSenders.model_validate(
            {
                "owner": ["a@example.org"],
                "others": [{"address": "a@example.org", "scope": ["ask_only"]}],
            }
        )


def test_evaluate_covers_owner_other_and_ask_only() -> None:
    senders = AuthorisedSenders.model_validate(
        {
            "owner": ["Me@Example.org"],
            "others": [
                {"address": "pa@example.org", "scope": ["calendar.create"]},
                {"address": "ask@example.org", "scope": ["ask_only"]},
            ],
        }
    )
    good = ("mx.google.com; dkim=pass header.d=example.org",)
    owner = evaluate(senders, "me@example.org", good, NOW, NOW)
    assert owner.covers("mail.send")
    pa = evaluate(senders, "pa@example.org", good, NOW, NOW)
    assert pa.covers("calendar.create")
    assert not pa.covers("calendar.delete")
    ask = evaluate(senders, "ask@example.org", good, NOW, NOW)
    assert not ask.covers("calendar.create")
    with pytest.raises(Refusal):
        evaluate(senders, "x@example.org", good, NOW, NOW)
    with pytest.raises(Refusal):
        evaluate(senders, "me@example.org", good, NOW - timedelta(days=8), NOW)


def test_a_stored_ref_round_trips_and_other_refs_are_not_instructions() -> None:
    senders = AuthorisedSenders.model_validate(
        {"others": [{"address": "pa@example.org", "scope": ["mail.move", "calendar.create"]}]}
    )
    good = ("mx.google.com; dkim=pass header.d=example.org",)
    made = evaluate(senders, "pa@example.org", good, NOW, NOW)
    parsed = parse_instruction_ref(instruction_ref(made, "msg_abc"))
    assert parsed is not None
    assert parsed.who == "other"
    assert parsed.kinds == {"mail.move", "calendar.create"}
    assert parse_instruction_ref("msg_abc") is None
    assert parse_instruction_ref(None) is None
    assert parse_instruction_ref("instruction:boss::x") is None
