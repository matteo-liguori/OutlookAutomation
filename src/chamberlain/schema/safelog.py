"""Failure logging that never carries message content.

`log.exception` prints the exception text, and a pydantic `ValidationError` quotes the
offending input (a mail body, a subject, a recipient). Logs may be shared, so a failure is
logged as its type, error code and the handles the caller names: never field values.
"""

import logging
import sys

from pydantic import ValidationError

from chamberlain.schema.errors import ChamberlainError


def describe_failure(error: BaseException | None) -> str:
    """The error's type, code and (for validation) the field paths, never values."""
    if error is None:
        return "unknown error"
    name = type(error).__name__
    if isinstance(error, ChamberlainError):
        return f"{name} code={error.code.value}"
    if isinstance(error, ValidationError):
        fields = sorted({".".join(str(part) for part in item["loc"]) for item in error.errors()})
        return f"{name} fields={','.join(fields)}"
    return name


def log_failure(logger: logging.Logger, message: str, *handles: object) -> None:
    """Log inside an `except` block: `message` with `%s` handles, then the safe description."""
    error = sys.exc_info()[1]
    logger.error(message + " (%s)", *handles, describe_failure(error))
