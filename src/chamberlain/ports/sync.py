"""FolderSync: ask a mail client to bring folders up to date, and wait until it says it has.

Contract: docs/contracts/08-ports.md (P-20 to P-22). A connector that reads from a client's own
local index (Thunderbird) implements this next to `MailPort`; a connector that reads the server
directly does not need it. `run_sync` is the one loop both the Thunderbird connector and the fake
provider use, so batching, the time limit and the answer for a folder that would not finish are
the same everywhere.
"""

from collections.abc import Callable, Collection, Sequence
from datetime import datetime, timedelta
from typing import Final, Protocol, runtime_checkable

from chamberlain.ports.clock import Clock
from chamberlain.schema.base import Model
from chamberlain.schema.errors import ChamberlainError

NOT_SELECTABLE: Final = "not-selectable"
"""The folder holds no mail (an account root, a container, a saved search). Nothing to sync."""
NOT_REACHED: Final = "time-limit"
"""The time limit ended before this folder was asked."""
NO_ANSWER: Final = "no-answer"
"""The client did not say what became of this folder."""
UNAVAILABLE: Final = "unavailable"
"""The client could not be reached, so no further folder was asked."""

FOLDER_SECONDS: Final = 8
"""How long one folder is given in one attempt."""
BATCH_FOLDERS: Final = 3
"""Folders asked in one client call. The client answers one call within its read deadline
(30 seconds at the add-on), so a batch times out only when every folder in it is stuck."""
MAX_ROUNDS: Final = 12
"""Attempts per folder in one run, a guard for a clock that does not move."""


class FolderOutcome(Model):
    """What became of one folder."""

    folder_id: str
    synced: bool
    code: str = ""
    """Empty when synced; otherwise why not (the client's code, or one of the names above)."""
    at: datetime
    """When the answer arrived."""


class SyncReport(Model):
    """One outcome per folder asked. A folder with no outcome was never asked."""

    folders: tuple[FolderOutcome, ...]


@runtime_checkable
class FolderSync(Protocol):
    def sync_folders(
        self,
        source: str,
        folder_ids: Sequence[str],
        time_limit: timedelta,
        retry: Collection[str] = frozenset(),
    ) -> SyncReport:
        """Ask the client to sync every folder in `folder_ids`, in any number of calls, and wait.

        Stops asking when `time_limit` has passed; folders not yet asked are reported as failed.
        A folder that does not finish is asked once more, until the time limit, only when it is
        in `retry`. Never raises for a folder that would not sync: it is reported (P-21).
        """
        ...


Attempt = Callable[[Sequence[str]], Sequence[FolderOutcome]]
"""Ask for one batch; return the outcomes the client gave. May raise `ChamberlainError`."""


def run_sync(
    attempt: Attempt,
    folder_ids: Sequence[str],
    clock: Clock,
    time_limit: timedelta,
    retry: Collection[str],
    *,
    batch: int = BATCH_FOLDERS,
) -> SyncReport:
    """Ask in batches, wait for each, stop at the time limit, and answer for every folder."""
    deadline = clock.now() + time_limit
    wanted = list(dict.fromkeys(folder_ids))
    last: dict[str, FolderOutcome] = {}
    pending = wanted
    for _ in range(MAX_ROUNDS):
        failed: list[str] = []
        for start in range(0, len(pending), batch):
            group = pending[start : start + batch]
            if clock.now() >= deadline:
                for folder in pending[start:]:
                    last[folder] = _failure(folder, NOT_REACHED, clock)
                    failed.append(folder)
                break
            try:
                given = {o.folder_id: o for o in attempt(group)}
            except ChamberlainError:
                for folder in pending[start:]:
                    last[folder] = _failure(folder, UNAVAILABLE, clock)
                return SyncReport(folders=tuple(last[f] for f in wanted))
            for folder in group:
                outcome = given.get(folder) or _failure(folder, NO_ANSWER, clock)
                last[folder] = outcome
                if not outcome.synced and outcome.code != NOT_SELECTABLE:
                    failed.append(folder)
        pending = [f for f in failed if f in retry and last[f].code != NOT_REACHED]
        if not pending or clock.now() >= deadline:
            break
    return SyncReport(folders=tuple(last[f] for f in wanted))


def _failure(folder: str, code: str, clock: Clock) -> FolderOutcome:
    return FolderOutcome(folder_id=folder, synced=False, code=code, at=clock.now())
