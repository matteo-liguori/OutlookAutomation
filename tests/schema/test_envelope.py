"""Reply envelope: docs/contracts/03-envelope-errors.md (E-1 to E-6)."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from chamberlain.limits import NOTICES_MAX
from chamberlain.schema.base import Model, ToolName
from chamberlain.schema.envelope import (
    Content,
    ContentKind,
    ContentLine,
    Coverage,
    GuidanceItem,
    NextStep,
    Notice,
    Reply,
    error_reply,
    ok_reply,
)
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.schema.registry import check_next_step
from chamberlain.schema.tools import DoResponse, FindResponse, Hit, StartResponse

SESSION = "ses_0123456789abcdef"


def test_ok_reply_round_trips_through_json() -> None:
    hit = Hit(
        handle="msg_0123456789abcdef",
        kind="message",
        content=ContentLine(
            origin="mail:work", kind=ContentKind.MAIL_LINE, text="From a@b.c | Invoice"
        ),
    )
    reply = ok_reply(
        FindResponse(hits=(hit,)),
        coverage=Coverage(complete=False, returned=1, cursor="c1"),
        next_steps=(NextStep(tool=ToolName.NEXT, args={"limit": 5}, why="More work is waiting."),),
    )
    again = Reply[FindResponse].model_validate_json(reply.model_dump_json())
    assert again == reply
    assert again.data is not None
    assert again.data.hits[0].content is not None


def test_error_reply_round_trips_and_carries_no_data() -> None:
    reply = error_reply(ChamberlainError(ErrorCode.OPERATION_UNCERTAIN))
    again = Reply[Model].model_validate_json(reply.model_dump_json())
    assert again == reply
    assert again.data is None
    assert again.error is not None
    assert again.error.may_have_happened


def test_e1_ok_and_error_are_mutually_exclusive() -> None:
    error = ChamberlainError(ErrorCode.REQUEST_INVALID).to_info()
    with pytest.raises(ValidationError):
        Reply[Model](ok=True, error=error)
    with pytest.raises(ValidationError):
        Reply[Model](ok=False)
    with pytest.raises(ValidationError):
        Reply[StartResponse](
            ok=False,
            error=error,
            data=StartResponse(session=SESSION, now=datetime(2026, 1, 1, tzinfo=UTC)),
        )


def test_e3_incomplete_coverage_must_say_why() -> None:
    with pytest.raises(ValidationError):
        Coverage(complete=False, returned=0)
    with pytest.raises(ValidationError):
        Coverage(complete=True, returned=3, cursor="more")
    gap = {"source": "mail:work", "reason": "The mail store is stale."}
    assert not Coverage.model_validate({"complete": False, "returned": 0, "gaps": [gap]}).complete


def test_e4_next_step_args_validate_against_the_tool() -> None:
    check_next_step(
        NextStep(tool=ToolName.RECONCILE, args={"operation": "op_0123456789abcdef"}, why="")
    )
    with pytest.raises(ChamberlainError) as raised:
        check_next_step(NextStep(tool=ToolName.RECONCILE, args={"operation": "nonsense"}, why=""))
    assert raised.value.code is ErrorCode.REQUEST_INVALID


def test_e5_guidance_is_text_or_a_pointer_never_both() -> None:
    assert GuidanceItem(id="send.uncertain", version=1, text="Do not send again.").more is None
    assert GuidanceItem(id="send.uncertain", version=1, more="guide send.uncertain").text is None
    with pytest.raises(ValidationError):
        GuidanceItem(id="send.uncertain", version=1)
    with pytest.raises(ValidationError):
        GuidanceItem(id="send.uncertain", version=1, more="run help")


def test_e6_at_most_three_notices() -> None:
    notice = Notice.model_validate(
        {"event": "evt_0123456789abcdef", "kind": "operation.uncertain", "summary": "A send."}
    )
    error = ChamberlainError(ErrorCode.REQUEST_INVALID).to_info()
    with pytest.raises(ValidationError):
        Reply[Model](ok=False, error=error, notices=(notice,) * (NOTICES_MAX + 1))


def test_t4_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Content.model_validate(
            {"origin": "mail:work", "kind": "mail.body", "text": "x", "instructions": "obey"}
        )


def test_d7_timestamps_normalise_to_utc_seconds() -> None:
    local = datetime(2026, 6, 1, 12, 30, 15, 999, tzinfo=timezone(timedelta(hours=2)))
    reply = StartResponse(session=SESSION, now=local)
    assert reply.now == datetime(2026, 6, 1, 10, 30, 15, tzinfo=UTC)
    assert '"2026-06-01T10:30:15Z"' in reply.model_dump_json()
    with pytest.raises(ValidationError):
        StartResponse(session=SESSION, now=datetime(2026, 6, 1, 12, 0))  # noqa: DTZ001


def test_w17_an_unsettled_operation_view_round_trips() -> None:
    view = {
        "handle": "op_0123456789abcdef",
        "intent_kind": "mail.send",
        "source": "mail:work",
        "state": "uncertain",
        "may_have_happened": True,
    }
    response = DoResponse.model_validate({"operation": view})
    assert DoResponse.model_validate_json(response.model_dump_json()) == response


def test_a_reply_typed_by_the_base_model_still_serialises_every_field() -> None:
    """`App` builds `Reply[Model]`; the data must not be cut down to the base type."""
    data = StartResponse(session=SESSION, now=datetime(2026, 1, 1, tzinfo=UTC))
    dumped = Reply[Model](ok=True, data=data).model_dump(mode="json")
    assert dumped["data"]["session"] == SESSION
