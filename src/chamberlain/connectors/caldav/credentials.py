"""Where the CalDAV password lives: the credential store, under a name the binding holds.

Contract: docs/contracts/09-storage.md (S-17). The password is never in the binding,
the connection profile, the database or a log.
"""

from typing import Final

from chamberlain.connectors.caldav.calendar import PasswordSource
from chamberlain.connectors.caldav.config import CalDavBinding
from chamberlain.connectors.calendar_support.secrets import SecretStore
from chamberlain.schema.errors import ChamberlainError, ErrorCode

SERVICE: Final = "chamberlain.caldav"


def credential_name(username: str) -> str:
    return f"password:{username.strip().lower()}"


def password_source(store: SecretStore) -> PasswordSource:
    """The function the connector calls to get a password at the moment it needs it."""

    def lookup(binding: CalDavBinding) -> str:
        password = store.get(SERVICE, binding.credential)
        if password is None:
            raise ChamberlainError(
                ErrorCode.SOURCE_SIGN_IN_NEEDED,
                "No password is saved for this calendar.",
                fix="Save the calendar password in the CalDAV setup.",
            )
        return password

    return lookup
