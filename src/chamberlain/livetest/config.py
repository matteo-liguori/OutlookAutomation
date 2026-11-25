"""The `live_test` section of `config/connections.yaml`.

Contract: docs/live-test.md and docs/configuration.md. Default: disabled, nothing listed.
Every model forbids unknown keys, so a typo or a pasted secret fails validation. This
module imports nothing from the rest of the harness except base types, so
`connections/model.py` can use it.
"""

from typing import Self

from pydantic import Field, model_validator

from chamberlain.schema.base import EmailAddress, Model, SourceId

ARRIVAL_WAIT_DEFAULT = 120


class LiveMailbox(Model):
    """One mailbox a live test may use."""

    address: EmailAddress
    """The address of this mailbox, owned by the person. Tests send only to owned addresses."""
    scratch_folder: str | None = Field(default=None, min_length=1)
    """Folder id or name for the file-and-back scenario. Default: the archive folder."""


class LiveCalendar(Model):
    """One calendar a live test may write to."""

    invitee_mail: SourceId | None = None
    """A `live_test` mailbox whose address is invited by the invitation scenarios."""
    invitee_calendar: SourceId | None = None
    """Optional: the invitee's own calendar, where the invitation should also appear and where
    the response is given. Without it only the mailbox is checked for the invitation."""


class LivePair(Model):
    """A cross-account send: `source` sends to the address of `to`."""

    source: SourceId
    to: SourceId


class LiveTestConfig(Model):
    enabled: bool = False
    mail: dict[SourceId, LiveMailbox] = Field(default_factory=dict)
    calendars: dict[SourceId, LiveCalendar] = Field(default_factory=dict)
    pairs: tuple[LivePair, ...] = ()
    approve_calendar_edits: bool = False
    """The owner's standing word that a run may change and delete the calendar events it made.

    Calendar update and delete are never covered by a standing grant, so each needs
    per-operation `agent_evidence`. When this is true the runner records evidence that cites
    this setting and the run id. `--approve-calendar-edits` does the same for one run."""
    arrival_wait_seconds: int = Field(default=ARRIVAL_WAIT_DEFAULT, ge=5, le=900)
    """How long a scenario waits for a sent message to arrive in a mailbox."""

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        for source in self.mail:
            if not source.startswith("mail:"):
                raise ValueError(f"{source}: live_test.mail names mail sources")
        for source in self.calendars:
            if not source.startswith("calendar:"):
                raise ValueError(f"{source}: live_test.calendars names calendar sources")
        addresses = [box.address for box in self.mail.values()]
        if len(set(addresses)) != len(addresses):
            raise ValueError("live_test.mail: each mailbox needs its own address")
        for pair in self.pairs:
            if pair.source == pair.to:
                raise ValueError(f"{pair.source}: a pair names two different mailboxes")
            for source in (pair.source, pair.to):
                if source not in self.mail:
                    raise ValueError(f"{source}: a pair names a mailbox not in live_test.mail")
        for source, calendar in self.calendars.items():
            if calendar.invitee_mail is not None and calendar.invitee_mail not in self.mail:
                raise ValueError(f"{source}: invitee_mail is not in live_test.mail")
            if calendar.invitee_calendar is not None and (
                calendar.invitee_calendar not in self.calendars
                or calendar.invitee_calendar == source
            ):
                raise ValueError(
                    f"{source}: invitee_calendar must be another calendar listed in "
                    "live_test.calendars"
                )
            if calendar.invitee_calendar is not None and calendar.invitee_mail is None:
                raise ValueError(f"{source}: invitee_calendar needs invitee_mail")
        if self.enabled and not self.mail and not self.calendars:
            raise ValueError("live_test is enabled but lists no mailbox or calendar")
        return self

    def owned_addresses(self) -> frozenset[str]:
        """The only addresses a live test may send to."""
        return frozenset(box.address for box in self.mail.values())

    def address_of(self, source: str) -> str:
        return self.mail[source].address
