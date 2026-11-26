"""What the agent found in a review: `keep` kind `note`, and the owner line for a worrying one.

Contract: docs/contracts/02-tools.md (T-10c), 06-memory.md (M-40). An idea stays in the store for
the coordinator. A worrying finding is shown once, as a line in the next pass report.
"""

from chamberlain.app.context import Context, Result
from chamberlain.domain.canonical import sha256_text
from chamberlain.domain.events import EventKind
from chamberlain.domain.handles import HandleKind, handle_kind
from chamberlain.review import repo
from chamberlain.review.tuning import FINDING_MAX, FINDINGS_PER_ROUND
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.schema.tools import KeepKind, KeepRequest, KeepResponse
from chamberlain.store.db import transaction
from chamberlain.store.migrate import stamp

KINDS = ("idea", "worrying")


def _round_of(ctx: Context, handle: str) -> int:
    if handle_kind(handle) is not HandleKind.EVENT:
        raise ChamberlainError(
            ErrorCode.REQUEST_WRONG_KIND,
            "A finding belongs to the review item. Name that item's handle.",
            field="owner",
        )
    identity = repo.event_identity(ctx.db, handle)
    if identity is None or identity[0] != EventKind.HARNESS_REVIEW.value:
        raise ChamberlainError(
            ErrorCode.REQUEST_WRONG_KIND,
            "A finding belongs to a harness review item.",
            field="owner",
        )
    round_ = repo.get_round(ctx.db, int(identity[1].rsplit(":", 1)[1]))
    if round_ is None or round_.state != "open":
        raise ChamberlainError(ErrorCode.REQUEST_INVALID, "That review has ended.", field="owner")
    return round_.id


def keep_finding(ctx: Context, request: KeepRequest) -> Result:
    """Record one finding: `idea | text` or `worrying | text`."""
    assert request.text is not None
    kind, _, rest = request.text.partition("|")
    word = kind.strip().lower()
    sentence = " ".join(rest.split())
    if word not in KINDS or not sentence or len(sentence) > FINDING_MAX:
        raise ChamberlainError(
            ErrorCode.REQUEST_INVALID,
            f"A finding is idea or worrying, a bar, then one sentence of at most {FINDING_MAX} "
            "characters.",
            field="text",
        )
    round_id = _round_of(ctx, request.owner)
    with transaction(ctx.db):
        if repo.count_findings(ctx.db, round_id) >= FINDINGS_PER_ROUND:
            raise ChamberlainError(
                ErrorCode.REQUEST_INVALID,
                f"A review holds at most {FINDINGS_PER_ROUND} findings. Join the smaller ones.",
                field="text",
            )
        repo.add_finding(ctx.db, round_id, word, sentence, stamp(ctx.clock))
    stored = f"{word}|{sentence}"
    return Result(
        KeepResponse(
            handle=request.owner,
            kind=KeepKind.NOTE,
            size=len(stored.encode()),
            sha256=sha256_text(stored),
        )
    )


def _plain(text: str) -> str:
    """Made safe for a line of markdown: no bar, link, emphasis or code can come out of it."""
    cleaned = text.translate({ord(c): " " for c in "|[]()*`_<>#"})
    return " ".join(cleaned.split())


def report_lines(ctx: Context) -> tuple[str, list[int]]:
    """The owner's line for each worrying finding not yet shown, and their ids."""
    found = repo.unreported_worrying(ctx.db)
    text = "\n".join(f"- {_plain(f.text)}" for f in found)
    return text, [f.id for f in found]


def mark_reported(ctx: Context, ids: list[int]) -> None:
    if not ids:
        return
    with transaction(ctx.db):
        repo.mark_reported(ctx.db, ids, stamp(ctx.clock))
