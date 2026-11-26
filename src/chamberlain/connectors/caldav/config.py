"""Configuration of the CalDAV connector (iCloud and any standard server).

Contract: docs/contracts/09-storage.md (S-16, S-17). A source is bound to one calendar
collection. The password is never here: `credential` names it in the credential store.
"""

from typing import Literal

from pydantic import Field

from chamberlain.schema.base import Model, SourceId


class CalDavBinding(Model):
    kind: Literal["caldav"] = "caldav"
    url: str = Field(min_length=8)
    """The calendar collection, for example `https://host/user/calendar/`."""
    username: str = Field(min_length=1)
    credential: str = Field(min_length=1)
    """The name of the password in the credential store."""
    address: str | None = Field(default=None, pattern=r"^[^@\s]+@[^@\s]+$")
    """The calendar user address invitations are sent from and answered as (iCloud: the
    Apple ID address). Without it, a username that is an address is used; with neither, the
    calendar declines invitations rather than guess."""


class CalDavConfig(Model):
    bindings: dict[SourceId, CalDavBinding]
    deadline_seconds: float = Field(default=30.0, gt=0)
    """Every call gives up after this long (P-17)."""
