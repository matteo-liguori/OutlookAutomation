"""Contact types exchanged across ContactsPort.

Contract: docs/contracts/08-ports.md.
"""

from pydantic import Field

from chamberlain.schema.base import EmailAddress, Model, SourceId


class Contact(Model):
    source: SourceId
    locator: str
    name: str
    emails: tuple[EmailAddress, ...] = ()


class ContactQuery(Model):
    text: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=10, ge=1, le=50)
