"""Configuration of the Google calendar connector.

Contract: docs/contracts/09-storage.md (S-16). A source is bound to one Google
account (the sign-in) and one calendar id (`primary` for the main one).
"""

from typing import Literal

from pydantic import Field

from chamberlain.schema.base import Model, SourceId


class GoogleBinding(Model):
    kind: Literal["google"] = "google"
    account: str = Field(min_length=3)
    calendar_id: str = Field(min_length=1)
    """`primary` for the account's main calendar."""


class GoogleCalendarConfig(Model):
    bindings: dict[SourceId, GoogleBinding]
    deadline_seconds: float = Field(default=30.0, gt=0)
    """Every call gives up after this long (P-17)."""
