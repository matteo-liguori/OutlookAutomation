"""Error taxonomy: docs/contracts/03-envelope-errors.md (E-10 to E-15)."""

import re

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from chamberlain.limits import FIX_MAX
from chamberlain.schema.errors import (
    ERROR_TABLE,
    ChamberlainError,
    ErrorCategory,
    ErrorCode,
    ErrorInfo,
)


def test_e10_every_code_has_a_spec_and_a_dotted_lowercase_name() -> None:
    assert set(ERROR_TABLE) == set(ErrorCode)
    for code in ErrorCode:
        assert re.fullmatch(r"[a-z_]+\.[a-z_]+", code.value), code


def test_e13_every_default_fix_is_one_short_sentence() -> None:
    for code, spec in ERROR_TABLE.items():
        assert 0 < len(spec.fix) <= FIX_MAX, code
        assert "{" not in spec.fix, code


def test_every_category_is_used() -> None:
    assert {spec.category for spec in ERROR_TABLE.values()} == set(ErrorCategory)


@given(st.sampled_from(sorted(ErrorCode)), st.text(max_size=400), st.booleans())
def test_error_info_round_trips_through_json(code: ErrorCode, message: str, retry: bool) -> None:
    info = ChamberlainError(code, message, retryable=retry, field="to").to_info()
    assert ErrorInfo.model_validate_json(info.model_dump_json()) == info
    assert info.category is ERROR_TABLE[code].category


@given(st.sampled_from(sorted(ErrorCode)), st.booleans())
def test_e11_e12_only_uncertain_may_have_happened_and_is_never_retryable(
    code: ErrorCode, retry: bool
) -> None:
    info = ChamberlainError(code, retryable=retry).to_info()
    assert info.may_have_happened == (info.category is ErrorCategory.UNCERTAIN)
    assert not (info.may_have_happened and info.retryable)


def test_e11_the_model_rejects_an_inconsistent_error() -> None:
    good = ChamberlainError(ErrorCode.SOURCE_UNAVAILABLE).to_info().model_dump()
    with pytest.raises(ValidationError):
        ErrorInfo.model_validate({**good, "may_have_happened": True})
    with pytest.raises(ValidationError):
        ErrorInfo.model_validate({**good, "category": "conflict"})
    uncertain = ChamberlainError(ErrorCode.OPERATION_UNCERTAIN).to_info().model_dump()
    with pytest.raises(ValidationError):
        ErrorInfo.model_validate({**uncertain, "retryable": True})


def test_e15_detail_stays_out_of_the_reply() -> None:
    error = ChamberlainError(ErrorCode.SOURCE_UNAVAILABLE, detail="raw client text: secret")
    assert "secret" not in error.to_info().model_dump_json()
