"""Context for health checks during side-effect-free planning."""

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar

_ACTIVE_ALLOWED: ContextVar[bool] = ContextVar("active_health_checks_allowed", default=True)


def active_health_checks_allowed() -> bool:
    return _ACTIVE_ALLOWED.get()


@contextmanager
def passive_health_checks() -> Generator[None]:
    token = _ACTIVE_ALLOWED.set(False)
    try:
        yield
    finally:
        _ACTIVE_ALLOWED.reset(token)
