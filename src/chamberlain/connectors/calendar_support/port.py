"""One calendar write lifecycle over provider-specific backends.

Contract: docs/contracts/08-ports.md (P-9 to P-12, P-16). A backend supplies
provider facts and one mutation; this port owns the decisions. Reconcile reads only.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Literal, Self

from pydantic import JsonValue

from chamberlain.connectors.calendar_support.http import (
    HttpReply,
    TransportFailure,
    write_may_have_landed,
)
from chamberlain.domain.calendar import EventFields
from chamberlain.domain.capabilities import (
    Capability,
    CapabilityName,
    CapabilitySet,
    VerificationLevel,
)
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
    Verification,
)
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

    @classmethod
    def missing(
        cls, *, deleted: str | None = None, server: bool = True, proof: bool = False
    ) -> Self:
        receipt = (
            ProviderReceipt(
                locator=deleted,
                verification=Verification(
                    client_verified=True,
                    server_verified=server,
                ),
            )
            if deleted is not None
            else None
        )
        return cls(
            present=False,
            delete_confirmed=receipt is not None,
            absence_provable=proof,
            receipt=receipt,
        )

    def verified(self, kind: IntentKind, *, recovery: bool = False) -> ProviderReceipt | None:
        if not self.complete:
            return None
        if kind is IntentKind.CALENDAR_DELETE:
            return self.receipt if self.delete_confirmed else None
        by_identity = recovery and kind is IntentKind.CALENDAR_CREATE and self.identity_suffices
        return self.receipt if self.present and (self.matches or by_identity) else None


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


def payload(prepared: Prepared) -> dict[str, JsonValue]:
    value = prepared.data.get("payload")
    if not isinstance(value, dict):
        raise ChamberlainError(ErrorCode.INTERNAL_UNEXPECTED, "The prepared payload is missing.")
    return value


def prepared_text(data: Mapping[str, JsonValue], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str):
        raise ChamberlainError(ErrorCode.INTERNAL_UNEXPECTED, f"The prepared {name} is missing.")
    return value


def locator(prepared: Prepared) -> str:
    return prepared_text(prepared.data, "locator")


def expected_etag(prepared: Prepared) -> str:
    return prepared_text(prepared.data, "expected_etag")


def conditional_headers(prepared: Prepared, *, exclusive_create: bool = False) -> dict[str, str]:
    if prepared.intent_kind is IntentKind.CALENDAR_CREATE:
        return {"If-None-Match": "*"} if exclusive_create else {}
    return {"If-Match": expected_etag(prepared)}


def fields(prepared: Prepared) -> EventFields:
    return EventFields.model_validate(prepared.data.get("fields"))


def binding_for[T](bindings: Mapping[str, T], source: str) -> T:
    binding = bindings.get(source)
    if binding is None:
        raise ChamberlainError(ErrorCode.SOURCE_NOT_CONFIGURED, "That source is not set up.")
    return binding


def require_event[T](event: T | None, *, occurrence: bool = False) -> T:
    if event is None:
        item = "occurrence" if occurrence else "event"
        raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, f"That {item} is not there.")
    return event


def select_unique[T](records: Iterable[T]) -> tuple[T | None, bool]:
    """Take the first candidate and establish whether a second one makes it ambiguous."""
    entries = iter(records)
    return next(entries, None), next(entries, None) is None


def calendar_capabilities(
    source: str, *, server: bool = False, invitations: bool = False, recurrence: bool | None = None
) -> CapabilitySet:
    """`recurrence` defaults to `server`: a calendar that proves absence writes series."""
    level = VerificationLevel.IMPLEMENTED
    proof = (
        frozenset(kind.value for kind in IntentKind if kind.value.startswith("calendar."))
        if server
        else frozenset[str]()
    )
    items = [
        Capability(name=CapabilityName.CALENDAR_READ, level=level, limits={"page_size": 250}),
        Capability(name=CapabilityName.CALENDAR_WRITE, level=level, absence_provable=proof),
    ]
    if invitations:
        items.append(Capability(name=CapabilityName.CALENDAR_INVITE, level=level))
    if server if recurrence is None else recurrence:
        items.extend(
            Capability(name=name, level=level, absence_provable=proof)
            for name in (CapabilityName.CALENDAR_RECURRENCE, CapabilityName.CALENDAR_OCCURRENCE)
        )
    return CapabilitySet(source=source, capabilities=tuple(items))


def commit_http(
    build: Callable[[], tuple[Callable[[], HttpReply], tuple[int, ...]]], provider: str
) -> WriteReply:
    """Build the one request (a failure here sent nothing), then classify its transport and
    response before reading back."""
    try:
        send, accepted = build()
    except Exception as error:
        raise WriteFailure(type(error).__name__, sent=False) from error
    try:
        reply = send()
    except TransportFailure as error:
        raise WriteFailure(f"transport: {error}", sent=error.sent) from None
    except ChamberlainError as error:
        # The HTTP adapters acquire credentials before passing the request to the transport.
        raise WriteFailure(str(error), sent=False) from None
    if reply.status not in accepted:
        raise WriteFailure(f"{provider} http {reply.status}", sent=True, status=reply.status)
    return WriteReply()


class CalendarWritePort(ABC):
    """Common preparation, one-dispatch verification and read-only reconciliation."""

    @abstractmethod
    def resolve(self, source: str, locator: str) -> Current: ...

    @abstractmethod
    def encode(self, intent: CalendarIntent, current: Current | None) -> dict[str, JsonValue]:
        """Check provider permissions and prepare the payload from the resolved event."""
        ...

    def preflight(self, prepared: Prepared) -> None:
        """Optional provider-side check before dispatch, using reads only."""
        return None

    @abstractmethod
    def write(self, prepared: Prepared) -> WriteReply: ...

    @abstractmethod
    def observe(self, prepared: Prepared, reply: WriteReply | None) -> Observation: ...

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
            current = self.resolve(intent.source, target)
            if current.etag != intent.expected_etag:
                raise ChamberlainError(
                    ErrorCode.REMOTE_CHANGED, "The event changed since it was read."
                )
            if isinstance(intent, CalendarUpdate | CalendarDelete) and intent.scope != current.part:
                raise ChamberlainError(
                    ErrorCode.CAPABILITY_UNSUPPORTED,
                    "A repeating event is written only with the scope that names it.",
                )
        encoded = self.encode(intent, current)
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
            self.preflight(prepared)
        except Exception as error:
            return DispatchResult(outcome=DispatchOutcome.NOT_APPLIED, detail=str(error)[:300])
        try:
            reply = self.write(prepared)
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
            observation = self.observe(prepared, reply)
        except Exception as error:
            return DispatchResult(outcome=DispatchOutcome.UNKNOWN, detail=type(error).__name__)
        if (receipt := observation.verified(prepared.intent_kind)) is not None:
            return DispatchResult(outcome=DispatchOutcome.APPLIED, receipt=receipt)
        return DispatchResult(
            outcome=DispatchOutcome.UNKNOWN, detail="read-back differs or is incomplete"
        )

    def reconcile(self, prepared: Prepared, key: str) -> ReconcileResult:
        try:
            observed = self.observe(prepared, None)
        except Exception as error:
            return ReconcileResult(status=ReconcileStatus.UNKNOWN, detail=type(error).__name__)
        if not observed.complete:
            return ReconcileResult(status=ReconcileStatus.UNKNOWN, detail="read-back incomplete")
        kind = prepared.intent_kind
        create = kind is IntentKind.CALENDAR_CREATE
        # A cancelled entry or excluded occurrence can predate this write.
        if (create and not observed.present) or (not create and observed.unchanged):
            return ReconcileResult(status=ReconcileStatus.ABSENT, proof=observed.absence_provable)
        if (receipt := observed.verified(kind, recovery=True)) is not None:
            return ReconcileResult(status=ReconcileStatus.PRESENT, receipt=receipt)
        return ReconcileResult(status=ReconcileStatus.UNKNOWN, detail="changed otherwise")
