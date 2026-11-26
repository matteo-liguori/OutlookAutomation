"""One calendar write lifecycle over provider-specific backends.

Contract: docs/contracts/08-ports.md (P-9 to P-12, P-16). A backend supplies
provider facts and one mutation; this port owns the decisions. Reconcile reads only.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import JsonValue

from chamberlain.connectors.calendar_support.http import write_may_have_landed
from chamberlain.domain.calendar import CalendarEvent, CalendarQuery, EventFields
from chamberlain.domain.capabilities import CapabilitySet, SourceHealth
from chamberlain.domain.intents import (
    CalendarCreate,
    CalendarDelete,
    CalendarIntent,
    CalendarUpdate,
    IntentKind,
)
from chamberlain.domain.operations import (
    DispatchOutcome,
    DispatchResult,
    Prepared,
    ProviderReceipt,
    ReconcileResult,
    ReconcileStatus,
)
from chamberlain.ports.page import Page
from chamberlain.schema.errors import ChamberlainError, ErrorCode


@dataclass(frozen=True)
class Current:
    """The resolved item, its concurrency token and provider-specific read-only facts."""

    locator: str
    etag: str
    part: Literal["series", "occurrence"] | None = None
    raw: object = None


@dataclass(frozen=True)
class WriteReply:
    """Accepted write response, including any provider-assigned identity/read-back."""

    hint: object = None


@dataclass(frozen=True)
class Observation:
    """Provider facts, not a state-machine decision. `complete=False` proves nothing."""

    present: bool
    complete: bool = True
    matches: bool = False
    identity_suffices: bool = False
    delete_confirmed: bool = False
    unchanged: bool = False
    absence_provable: bool = False
    receipt: ProviderReceipt | None = None


class WriteFailure(Exception):
    """Facts about a failed provider call, including whether a mutation could have landed."""

    def __init__(
        self,
        detail: str,
        *,
        sent: bool,
        mutation_possible: bool = True,
        status: int | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.sent = sent
        self.mutation_possible = mutation_possible
        self.status = status


class CalendarBackend(Protocol):
    """Provider reads, request encoding and one raw mutation; no settlement decisions."""

    def capabilities(self, source: str) -> CapabilitySet: ...

    def health(self, source: str) -> SourceHealth: ...

    def events(
        self, source: str, query: CalendarQuery, cursor: str | None = None
    ) -> Page[CalendarEvent]: ...

    def read(self, source: str, locator: str) -> CalendarEvent: ...

    def resolve(self, source: str, locator: str) -> Current: ...

    def validate(self, intent: CalendarIntent, current: Current | None) -> None: ...

    def encode(self, intent: CalendarIntent, current: Current | None) -> dict[str, JsonValue]: ...

    def preflight(self, prepared: Prepared) -> None: ...

    def write(self, prepared: Prepared) -> WriteReply: ...

    def observe(self, prepared: Prepared, reply: WriteReply | None) -> Observation: ...


def payload(prepared: Prepared) -> dict[str, JsonValue]:
    value = prepared.data.get("payload")
    if not isinstance(value, dict):
        raise ChamberlainError(ErrorCode.INTERNAL_UNEXPECTED, "The prepared payload is missing.")
    return value


def _text(prepared: Prepared, name: str) -> str:
    value = prepared.data.get(name)
    if not isinstance(value, str):
        raise ChamberlainError(ErrorCode.INTERNAL_UNEXPECTED, f"The prepared {name} is missing.")
    return value


def locator(prepared: Prepared) -> str:
    return _text(prepared, "locator")


def expected_etag(prepared: Prepared) -> str:
    return _text(prepared, "expected_etag")


def fields(prepared: Prepared) -> EventFields:
    return EventFields.model_validate(prepared.data.get("fields"))


class CalendarPort:
    """Common preparation, one-dispatch verification and read-only reconciliation."""

    def __init__(self, backend: CalendarBackend) -> None:
        self.backend = backend

    def capabilities(self, source: str) -> CapabilitySet:
        return self.backend.capabilities(source)

    def health(self, source: str) -> SourceHealth:
        return self.backend.health(source)

    def events(
        self, source: str, query: CalendarQuery, cursor: str | None = None
    ) -> Page[CalendarEvent]:
        return self.backend.events(source, query, cursor)

    def read(self, source: str, locator: str) -> CalendarEvent:
        return self.backend.read(source, locator)

    def prepare(self, intent: CalendarIntent, refs: Mapping[str, str]) -> Prepared:
        current: Current | None = None
        token: str | None = None
        if isinstance(intent, CalendarCreate):
            if intent.uid is None:
                raise ChamberlainError(
                    ErrorCode.REQUEST_INVALID, "The event has no uid.", field="uid"
                )
            token = intent.uid
        else:
            target = refs.get(intent.event_ref)
            if target is None:
                raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "That event is not known.")
            current = self.backend.resolve(intent.source, target)
            if current.etag != intent.expected_etag:
                raise ChamberlainError(
                    ErrorCode.REMOTE_CHANGED, "The event changed since it was read."
                )
            if isinstance(intent, CalendarUpdate | CalendarDelete) and intent.scope != current.part:
                raise ChamberlainError(
                    ErrorCode.CAPABILITY_UNSUPPORTED,
                    "A repeating event is written only with the scope that names it.",
                )
        self.backend.validate(intent, current)
        encoded = self.backend.encode(intent, current)
        data: dict[str, JsonValue] = {
            "payload": encoded,
            "locator": None if current is None else current.locator,
            "expected_etag": None if current is None else current.etag,
            "fields": intent.event.model_dump(mode="json")
            if isinstance(intent, CalendarCreate | CalendarUpdate)
            else None,
        }
        return Prepared(
            source=intent.source, intent_kind=intent.kind, reconcile_token=token, data=data
        )

    def dispatch(self, prepared: Prepared, key: str) -> DispatchResult:
        try:
            self.backend.preflight(prepared)
        except Exception as error:
            return DispatchResult(outcome=DispatchOutcome.NOT_APPLIED, detail=str(error)[:300])
        try:
            reply = self.backend.write(prepared)
        except WriteFailure as error:
            clean = not error.sent or not error.mutation_possible
            if error.status is not None and not write_may_have_landed(error.status):
                clean = True
            return DispatchResult(
                outcome=DispatchOutcome.NOT_APPLIED if clean else DispatchOutcome.UNKNOWN,
                detail=error.detail[:300],
            )
        except Exception as error:
            # Once a backend write has been called, an unclassified failure cannot prove absence.
            return DispatchResult(outcome=DispatchOutcome.UNKNOWN, detail=type(error).__name__)
        try:
            observation = self.backend.observe(prepared, reply)
        except Exception as error:
            return DispatchResult(outcome=DispatchOutcome.UNKNOWN, detail=type(error).__name__)
        done = (
            observation.delete_confirmed
            if prepared.intent_kind is IntentKind.CALENDAR_DELETE
            else observation.present and observation.matches
        )
        if observation.complete and done and observation.receipt is not None:
            return DispatchResult(outcome=DispatchOutcome.APPLIED, receipt=observation.receipt)
        return DispatchResult(
            outcome=DispatchOutcome.UNKNOWN, detail="read-back differs or is incomplete"
        )

    def reconcile(self, prepared: Prepared, key: str) -> ReconcileResult:
        try:
            observed = self.backend.observe(prepared, None)
        except Exception as error:
            return ReconcileResult(status=ReconcileStatus.UNKNOWN, detail=type(error).__name__)
        if not observed.complete:
            return ReconcileResult(status=ReconcileStatus.UNKNOWN, detail="read-back incomplete")
        kind = prepared.intent_kind
        if kind is IntentKind.CALENDAR_CREATE:
            if not observed.present:
                return ReconcileResult(
                    status=ReconcileStatus.ABSENT, proof=observed.absence_provable
                )
            if (observed.matches or observed.identity_suffices) and observed.receipt is not None:
                return ReconcileResult(status=ReconcileStatus.PRESENT, receipt=observed.receipt)
        elif kind is IntentKind.CALENDAR_DELETE and observed.delete_confirmed:
            if observed.receipt is not None:
                return ReconcileResult(status=ReconcileStatus.PRESENT, receipt=observed.receipt)
        elif observed.present:
            if observed.unchanged:
                return ReconcileResult(
                    status=ReconcileStatus.ABSENT, proof=observed.absence_provable
                )
            if (
                kind is not IntentKind.CALENDAR_DELETE
                and observed.matches
                and observed.receipt is not None
            ):
                return ReconcileResult(status=ReconcileStatus.PRESENT, receipt=observed.receipt)
        return ReconcileResult(status=ReconcileStatus.UNKNOWN, detail="changed otherwise")
