"""A page of connector results with honest coverage.

Contract: docs/contracts/08-ports.md (P-4).
"""

from collections.abc import Callable, Iterator
from typing import Self

from pydantic import BaseModel, model_validator

from chamberlain.schema.base import Model, SourceId


class Gap(Model):
    """Something the connector could not look at."""

    source: SourceId
    reason: str
    narrow: bool = False
    """True when asking for a smaller time window would let the connector finish."""


class Page[ItemT: BaseModel](Model):
    items: tuple[ItemT, ...] = ()
    cursor: str | None = None
    complete: bool = True
    """True only when nothing was left unseen and there is no further page."""
    gaps: tuple[Gap, ...] = ()

    @model_validator(mode="after")
    def _honest(self) -> Self:
        if self.complete and (self.cursor is not None or self.gaps):
            raise ValueError("a complete page has no cursor and no gaps")
        if not self.complete and self.cursor is None and not self.gaps:
            raise ValueError("an incomplete page names a cursor or a gap")
        return self


def pages[T: BaseModel](
    read: Callable[[str | None], Page[T]], *, limit: int | None = None
) -> Iterator[Page[T]]:
    """Yield each response with its gaps and terminal status intact."""
    cursor = None
    count = 0
    while limit is None or count < limit:
        page = read(cursor)
        yield page
        count += 1
        cursor = page.cursor
        if cursor is None:
            return
