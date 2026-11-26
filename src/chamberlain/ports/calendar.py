"""CalendarPort: what the core may ask of a calendar connector.

Contract: docs/contracts/08-ports.md. The executable form is
`tests/support/calendar_contract.py`.
"""

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from chamberlain.domain.calendar import CalendarEvent, CalendarQuery
from chamberlain.domain.capabilities import CapabilitySet, SourceHealth
from chamberlain.domain.intents import CalendarIntent
from chamberlain.domain.operations import DispatchResult, Prepared, ReconcileResult
from chamberlain.ports.page import Page


@runtime_checkable
class CalendarPort(Protocol):
    def capabilities(self, source: str) -> CapabilitySet: ...

    def health(self, source: str) -> SourceHealth: ...

    # --- reading (P-3, P-4) ---------------------------------------------------

    def events(
        self, source: str, query: CalendarQuery, cursor: str | None = None
    ) -> Page[CalendarEvent]:
        """Events overlapping the window. Cancelled ones only when asked (P-16)."""
        ...

    def read(self, source: str, locator: str) -> CalendarEvent:
        """One event. A cancelled event is returned as cancelled, not as missing."""
        ...

    # --- writing: called only by the write engine ----------------------------

    def prepare(self, intent: CalendarIntent, refs: Mapping[str, str]) -> Prepared:
        """Read-only. Checks `expected_etag` (P-9, P-16)."""
        ...

    def dispatch(self, prepared: Prepared, key: str) -> DispatchResult:
        """The one mutation (P-10)."""
        ...

    def reconcile(self, prepared: Prepared, key: str) -> ReconcileResult:
        """Read-only (P-11, P-12)."""
        ...
