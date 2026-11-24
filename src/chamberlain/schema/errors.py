"""The error taxonomy: categories, the closed code list and the one exception.

Contract: docs/contracts/03-envelope-errors.md (E-10 to E-15).
"""

from enum import StrEnum
from typing import NamedTuple, Self

from pydantic import Field, JsonValue, model_validator

from chamberlain.limits import FIX_MAX
from chamberlain.schema.base import Model


class ErrorCategory(StrEnum):
    INVALID_REQUEST = "invalid_request"
    NOT_CONFIGURED = "not_configured"
    UNSUPPORTED = "unsupported"
    NOT_AUTHORISED = "not_authorised"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"
    UNCERTAIN = "uncertain"
    INTERNAL = "internal"


class ErrorCode(StrEnum):
    # invalid_request
    REQUEST_INVALID = "request.invalid"
    REQUEST_UNKNOWN_TOOL = "request.unknown_tool"
    REQUEST_UNKNOWN_HANDLE = "request.unknown_handle"
    REQUEST_WRONG_KIND = "request.wrong_kind"
    REQUEST_BAD_CURSOR = "request.bad_cursor"
    REQUEST_TOO_LARGE = "request.too_large"
    CARD_OVER_CAP = "card.over_cap"
    EVIDENCE_OVER_CAP = "evidence.over_cap"
    NOTE_OVER_CAP = "note.over_cap"
    ARTIFACT_NO_OWNER = "artifact.no_owner"
    ARTIFACT_OWNER_ENDED = "artifact.owner_ended"
    ARTIFACT_HASH_MISMATCH = "artifact.hash_mismatch"
    STORAGE_OVER_BUDGET = "storage.over_budget"
    # not_configured
    SOURCE_NOT_CONFIGURED = "source.not_configured"
    SESSION_REQUIRED = "session.required"
    CONFIG_INVALID = "config.invalid"
    # unsupported
    CAPABILITY_UNSUPPORTED = "capability.unsupported"
    TOOL_NOT_IMPLEMENTED = "tool.not_implemented"
    # not_authorised
    GRANT_REQUIRED = "grant.required"
    GRANT_HUMAN_CONFIRMATION_REQUIRED = "grant.human_confirmation_required"
    GRANT_NOT_ACTIVE = "grant.not_active"
    GRANT_SCOPE_MISMATCH = "grant.scope_mismatch"
    GRANT_CONTENT_MISMATCH = "grant.content_mismatch"
    GRANT_LIMIT_REACHED = "grant.limit_reached"
    RECIPIENT_UNKNOWN = "recipient.unknown"
    # conflict
    CARD_VERSION_CONFLICT = "card.version_conflict"
    OPERATION_ALREADY_SETTLED = "operation.already_settled"
    OPERATION_INTENT_CHANGED = "operation.intent_changed"
    REMOTE_CHANGED = "remote.changed"
    REMOTE_AMBIGUOUS = "remote.ambiguous"
    ARTIFACT_EXPIRED = "artifact.expired"
    CURSOR_EXPIRED = "cursor.expired"
    # unavailable
    SOURCE_UNAVAILABLE = "source.unavailable"
    SOURCE_SIGN_IN_NEEDED = "source.sign_in_needed"
    SOURCE_BUSY = "source.busy"
    SOURCE_STALE = "source.stale"
    # uncertain
    OPERATION_UNCERTAIN = "operation.uncertain"
    OPERATION_NEEDS_RECONCILE = "operation.needs_reconcile"
    # internal
    INTERNAL_UNEXPECTED = "internal.unexpected"
    REPLY_TOO_LARGE = "reply.too_large"
    STORE_MIGRATION_FAILED = "store.migration_failed"


class ErrorSpec(NamedTuple):
    category: ErrorCategory
    retryable: bool
    fix: str


_C = ErrorCategory
_E = ErrorCode

ERROR_TABLE: dict[ErrorCode, ErrorSpec] = {
    _E.REQUEST_INVALID: ErrorSpec(
        _C.INVALID_REQUEST, False, "Correct the named field; see the example."
    ),
    _E.REQUEST_UNKNOWN_TOOL: ErrorSpec(
        _C.INVALID_REQUEST, False, "Use one of the twelve listed tools."
    ),
    _E.REQUEST_UNKNOWN_HANDLE: ErrorSpec(
        _C.INVALID_REQUEST, False, "Use a handle from a recent reply; find it again if needed."
    ),
    _E.REQUEST_WRONG_KIND: ErrorSpec(
        _C.INVALID_REQUEST, False, "Pass a handle of the kind this field expects."
    ),
    _E.REQUEST_BAD_CURSOR: ErrorSpec(
        _C.INVALID_REQUEST, False, "Repeat the original call without a cursor."
    ),
    _E.REQUEST_TOO_LARGE: ErrorSpec(
        _C.INVALID_REQUEST, False, "Send less in one call; store large content with keep."
    ),
    _E.CARD_OVER_CAP: ErrorSpec(
        _C.INVALID_REQUEST, False, "Shorten the named field to its limit; keep current facts only."
    ),
    _E.EVIDENCE_OVER_CAP: ErrorSpec(
        _C.INVALID_REQUEST, False, "State the fact briefly; store long content as an artifact."
    ),
    _E.NOTE_OVER_CAP: ErrorSpec(
        _C.INVALID_REQUEST, False, "Shorten the note, or replace an older note on this action."
    ),
    _E.ARTIFACT_NO_OWNER: ErrorSpec(
        _C.INVALID_REQUEST, False, "Name an open action, operation or session as the owner."
    ),
    _E.ARTIFACT_OWNER_ENDED: ErrorSpec(
        _C.INVALID_REQUEST, False, "That owner has finished; name a live owner."
    ),
    _E.ARTIFACT_HASH_MISMATCH: ErrorSpec(
        _C.INVALID_REQUEST, False, "The stored content changed; fetch or keep it again."
    ),
    _E.STORAGE_OVER_BUDGET: ErrorSpec(
        _C.INVALID_REQUEST, False, "Forget the largest items you no longer need, then retry."
    ),
    _E.SOURCE_NOT_CONFIGURED: ErrorSpec(
        _C.NOT_CONFIGURED, False, "Tell the user this source is not set up."
    ),
    _E.SESSION_REQUIRED: ErrorSpec(_C.NOT_CONFIGURED, False, "Call start first."),
    _E.CONFIG_INVALID: ErrorSpec(
        _C.NOT_CONFIGURED, False, "Tell the user the configuration needs correcting; run doctor."
    ),
    _E.CAPABILITY_UNSUPPORTED: ErrorSpec(
        _C.UNSUPPORTED, False, "No enabled source can do this. Tell the user; do not improvise."
    ),
    _E.TOOL_NOT_IMPLEMENTED: ErrorSpec(
        _C.UNSUPPORTED, False, "This tool is not built yet. Tell the user."
    ),
    _E.GRANT_REQUIRED: ErrorSpec(
        _C.NOT_AUTHORISED, False, "Propose the write and obtain the user's permission."
    ),
    _E.GRANT_HUMAN_CONFIRMATION_REQUIRED: ErrorSpec(
        _C.NOT_AUTHORISED, False, "Ask the user to confirm this in the Chamberlain viewer."
    ),
    _E.GRANT_NOT_ACTIVE: ErrorSpec(
        _C.NOT_AUTHORISED, False, "That permission is not active. Propose again if still wanted."
    ),
    _E.GRANT_SCOPE_MISMATCH: ErrorSpec(
        _C.NOT_AUTHORISED, False, "The permission does not cover this. Propose it separately."
    ),
    _E.GRANT_CONTENT_MISMATCH: ErrorSpec(
        _C.NOT_AUTHORISED, False, "The content differs from what was approved. Propose the new one."
    ),
    _E.GRANT_LIMIT_REACHED: ErrorSpec(
        _C.NOT_AUTHORISED, False, "A limit was reached. Tell the user; do not work around it."
    ),
    _E.RECIPIENT_UNKNOWN: ErrorSpec(
        _C.NOT_AUTHORISED, False, "A recipient is new. The user must confirm in the viewer."
    ),
    _E.CARD_VERSION_CONFLICT: ErrorSpec(
        _C.CONFLICT, False, "Open the card again and reapply your change to the current version."
    ),
    _E.OPERATION_ALREADY_SETTLED: ErrorSpec(
        _C.CONFLICT, False, "This operation is finished. Open it to see the outcome."
    ),
    _E.OPERATION_INTENT_CHANGED: ErrorSpec(
        _C.CONFLICT, False, "The request differs from the stored one. Propose a new operation."
    ),
    _E.REMOTE_CHANGED: ErrorSpec(
        _C.CONFLICT, False, "The item changed at the source. Read it again, then decide."
    ),
    _E.REMOTE_AMBIGUOUS: ErrorSpec(
        _C.CONFLICT, False, "More than one item matches. Find the exact one and use its handle."
    ),
    _E.ARTIFACT_EXPIRED: ErrorSpec(
        _C.CONFLICT, False, "That stored item has expired. Fetch it again if still needed."
    ),
    _E.CURSOR_EXPIRED: ErrorSpec(_C.CONFLICT, False, "Repeat the original call without a cursor."),
    _E.SOURCE_UNAVAILABLE: ErrorSpec(
        _C.UNAVAILABLE, True, "Nothing was changed. Try again later; tell the user if it persists."
    ),
    _E.SOURCE_SIGN_IN_NEEDED: ErrorSpec(
        _C.UNAVAILABLE, False, "Nothing was changed. Ask the user to sign in to this source."
    ),
    _E.SOURCE_BUSY: ErrorSpec(_C.UNAVAILABLE, True, "Nothing was changed. Try again shortly."),
    _E.SOURCE_STALE: ErrorSpec(
        _C.UNAVAILABLE, True, "The source is not up to date. Say coverage is incomplete."
    ),
    _E.OPERATION_UNCERTAIN: ErrorSpec(
        _C.UNCERTAIN, False, "This may have happened. Do not repeat it. Call reconcile."
    ),
    _E.OPERATION_NEEDS_RECONCILE: ErrorSpec(
        _C.UNCERTAIN, False, "An earlier attempt is unsettled. Call reconcile; never resend."
    ),
    _E.INTERNAL_UNEXPECTED: ErrorSpec(
        _C.INTERNAL, False, "A harness fault. Tell the user and run doctor."
    ),
    _E.REPLY_TOO_LARGE: ErrorSpec(
        _C.INTERNAL, False, "A harness fault. Ask for less at once and tell the user."
    ),
    _E.STORE_MIGRATION_FAILED: ErrorSpec(
        _C.INTERNAL, False, "The database could not be upgraded. Tell the user; nothing was lost."
    ),
}


class ErrorInfo(Model):
    """The `error` field of the reply envelope."""

    code: ErrorCode
    category: ErrorCategory
    retryable: bool
    may_have_happened: bool
    message: str = Field(max_length=FIX_MAX)
    fix: str = Field(min_length=1, max_length=FIX_MAX)
    field: str | None = None
    example: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.category is not ERROR_TABLE[self.code].category:
            raise ValueError(f"{self.code} belongs to {ERROR_TABLE[self.code].category}")
        if self.may_have_happened != (self.category is ErrorCategory.UNCERTAIN):
            raise ValueError("may_have_happened is true exactly for the uncertain category (E-11)")
        if self.may_have_happened and self.retryable:
            raise ValueError("an error that may have happened is never retryable (E-12)")
        return self


class ChamberlainError(Exception):
    """The one exception for every condition an agent can meet (E-14).

    `detail` is for the log only and never reaches a reply (E-15).
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str = "",
        *,
        fix: str | None = None,
        field: str | None = None,
        example: dict[str, JsonValue] | None = None,
        retryable: bool | None = None,
        detail: str = "",
    ) -> None:
        spec = ERROR_TABLE[code]
        self.code = code
        self.category = spec.category
        self.message = message or code.value
        self.fix = fix or spec.fix
        self.field = field
        self.example = example
        self.may_have_happened = spec.category is ErrorCategory.UNCERTAIN
        self.retryable = False if self.may_have_happened else _or(retryable, spec.retryable)
        self.detail = detail
        super().__init__(f"{code.value}: {self.message}")

    def to_info(self) -> ErrorInfo:
        return ErrorInfo(
            code=self.code,
            category=self.category,
            retryable=self.retryable,
            may_have_happened=self.may_have_happened,
            message=self.message[:FIX_MAX],
            fix=self.fix[:FIX_MAX],
            field=self.field,
            example=self.example,
        )


def _or(value: bool | None, default: bool) -> bool:
    return default if value is None else value
