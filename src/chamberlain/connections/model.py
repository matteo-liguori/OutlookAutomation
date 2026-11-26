"""The private connection profile: which sources exist and how each is bound.

Contract: docs/contracts/09-storage.md (S-16, S-17). `<data root>/config/connections.yaml`
is a versioned Pydantic model. It names bindings (account, folder and calendar ids) and
credential names, never a secret: every model forbids unknown keys, so a pasted password
fails validation instead of being stored.
"""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from chamberlain.cleanup.rules import CleanupConfig
from chamberlain.domain.grants import Requirement
from chamberlain.domain.read_state import ReadStateConfig
from chamberlain.domain.senders import AuthorisedSenders
from chamberlain.limits import CARD_ARTIFACT_BYTES, SENDS_PER_HOUR, TOTAL_ARTIFACT_BYTES
from chamberlain.livetest.config import LiveTestConfig
from chamberlain.schema.base import Model, SourceId

PROFILE_VERSION = 1


class _Source(Model):
    label: str = Field(default="", max_length=80)
    enabled: bool = True
    """A disabled source stays in the file (and the `sources` table) but gets no connector."""
    commitments: bool = True
    """Calendars only: the owner's own commitments. A calendar kept for information (a family or
    shared calendar) sets this false: it is read and reported, but its events never count as a
    clash (A-26)."""


class ThunderbirdMailSource(_Source):
    """A mailbox bound to its Thunderbird account and exact folders."""

    kind: Literal["thunderbird"] = "thunderbird"
    account_id: str = Field(min_length=1)
    mailbox: str = Field(min_length=3, pattern=r"^[^@\s]+@[^@\s]+$")
    inbox_folder_id: str = Field(min_length=1)
    sent_folder_id: str = Field(min_length=1)
    draft_folder_id: str = Field(min_length=1)
    trash_folder_id: str = Field(min_length=1)


class ThunderbirdContactsSource(_Source):
    kind: Literal["thunderbird_contacts"] = "thunderbird_contacts"
    address_book_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        if len(set(self.address_book_ids)) != len(self.address_book_ids):
            raise ValueError("address_book_ids must be unique")
        return self


class OwlCalendarSource(_Source):
    """The Microsoft 365 calendar reached through Thunderbird with Owl."""

    kind: Literal["owl"] = "owl"
    calendar_id: str = Field(min_length=1)


class GoogleCalendarSource(_Source):
    kind: Literal["google"] = "google"
    account: str = Field(min_length=3)
    calendar_id: str = Field(min_length=1)
    """`primary` for the account's main calendar."""


class CalDavSource(_Source):
    kind: Literal["caldav"] = "caldav"
    url: str = Field(min_length=8)
    username: str = Field(min_length=1)
    credential: str = Field(min_length=1)
    """The name of the password in the credential store, never the password."""
    address: str | None = Field(default=None, pattern=r"^[^@\s]+@[^@\s]+$")
    """The address invitations are sent from. Optional when the username is an address."""


SourceConfig = Annotated[
    ThunderbirdMailSource
    | ThunderbirdContactsSource
    | OwlCalendarSource
    | GoogleCalendarSource
    | CalDavSource,
    Field(discriminator="kind"),
]

ROLE_OF_KIND: dict[str, str] = {
    "thunderbird": "mail",
    "thunderbird_contacts": "contacts",
    "owl": "calendar",
    "google": "calendar",
    "caldav": "calendar",
}


class WritePolicyConfig(Model):
    """The configurable part of authority (G-12, G-16). It may only tighten."""

    send_known: Requirement = Requirement.AGENT_EVIDENCE
    other: Requirement = Requirement.AGENT_EVIDENCE
    sends_per_hour: int = Field(default=SENDS_PER_HOUR, ge=1, le=SENDS_PER_HOUR)


class BudgetsConfig(Model):
    """Artifact byte limits (M-27)."""

    per_card_bytes: int = Field(default=CARD_ARTIFACT_BYTES, ge=1)
    total_bytes: int = Field(default=TOTAL_ARTIFACT_BYTES, ge=1)


class ScheduleConfig(Model):
    """One pass the person wants, applied to the schedule record at every start."""

    name: str = Field(pattern=r"^[a-z0-9_.-]{1,40}$")
    kind: Literal["mail", "calendar"]
    at: str = Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    zone: str | None = None
    """Defaults to the profile's `timezone`."""
    weekdays: tuple[int, ...] = Field(default=(1, 2, 3, 4, 5, 6, 7), min_length=1)
    enabled: bool = True
    presync_minutes: int = Field(default=5, ge=0, le=120)
    """Minutes before the pass that the tick syncs mail folders, so the pass finds them fresh.
    0 turns it off. Keep it at least as long as the time between two ticks."""


class MailSyncConfig(Model):
    """How the harness keeps Thunderbird's mail folders fresh (07-attention.md A-24, A-25)."""

    fresh_minutes: float = Field(default=10, gt=0, le=1440)
    """A folder last synced longer ago than this is synced again when the scan or the agent
    is about to read it."""
    time_limit_seconds: float = Field(default=300, ge=10, le=3600)
    """One sync of one source gives up after this long and goes on with the folders that
    finished."""


class UpdatesConfig(Model):
    """The daily check for a newer tag. Off unless enabled; it only reads tag names."""

    enabled: bool = False
    remote: str = Field(default="origin", min_length=1)


class ReviewConfig(Model):
    """The harness watching how the agent works outside it (docs/review.md). On by default."""

    enabled: bool = True
    new_patterns: int = Field(default=5, ge=1, le=500)
    """A review is raised once this many new ways of working outside the tools were logged..."""
    min_days: int = Field(default=3, ge=0, le=365)
    """...but not sooner than this many days after the last review..."""
    max_days: int = Field(default=60, ge=1, le=365)
    """...and after this many days even for fewer, if anything new was logged at all."""


class Connections(Model):
    version: Literal[1] = PROFILE_VERSION
    timezone: str = "UTC"
    write_policy: WritePolicyConfig = WritePolicyConfig()
    budgets: BudgetsConfig = BudgetsConfig()
    bridge_dir: str | None = None
    """Where the Thunderbird bridge keeps its runtime file; default `<data root>/bridge`."""
    schedules: tuple[ScheduleConfig, ...] = ()
    mail_sync: MailSyncConfig = MailSyncConfig()
    read_state: ReadStateConfig = ReadStateConfig()
    cleanup: CleanupConfig = CleanupConfig()
    """Old mail the owner's rules move to Trash (`chamberlain cleanup`). None by default."""
    updates: UpdatesConfig = UpdatesConfig()
    review: ReviewConfig = ReviewConfig()
    authorised_senders: AuthorisedSenders = AuthorisedSenders()
    live_test: LiveTestConfig = LiveTestConfig()
    """Which sources `chamberlain live-test` may use (docs/live-test.md). Off by default."""
    sources: dict[SourceId, SourceConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _role_matches_kind(self) -> Self:
        for source_id, source in self.sources.items():
            role = source_id.partition(":")[0]
            if ROLE_OF_KIND[source.kind] != role:
                raise ValueError(f"{source_id}: a {source.kind} source cannot be a {role} source")
        return self

    @model_validator(mode="after")
    def _live_test_names_real_sources(self) -> Self:
        live = self.live_test
        for source_id, box in live.mail.items():
            source = self.sources.get(source_id)
            if source is None:
                raise ValueError(f"live_test.mail: {source_id} is not a configured source")
            if isinstance(source, ThunderbirdMailSource) and source.mailbox.lower() != box.address:
                raise ValueError(
                    f"live_test.mail: {source_id} is the mailbox {source.mailbox}, "
                    f"not {box.address}"
                )
        for source_id in live.calendars:
            if source_id not in self.sources:
                raise ValueError(f"live_test.calendars: {source_id} is not a configured source")
        return self

    def of_kind[S: _Source](self, model: type[S]) -> dict[str, S]:
        """Enabled sources of one kind, by id."""
        return {
            source_id: source
            for source_id, source in self.sources.items()
            if isinstance(source, model) and source.enabled
        }
