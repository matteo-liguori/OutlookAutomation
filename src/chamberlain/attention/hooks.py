"""The tick and the reply decorator.

Contract: docs/contracts/07-attention.md (A-6, A-7, A-19). `decorate` sets only
`next` (when empty), `guidance` and `notices`, never fails, and keeps the reply
within `MAX_REPLY_CHARS` by dropping guidance to pointers before anything else.
"""

import logging
from dataclasses import dataclass
from typing import cast

from chamberlain.app.context import Context
from chamberlain.attention import inbox, repo, tick
from chamberlain.attention.models import EventRow
from chamberlain.attention.overrides import apply_overrides
from chamberlain.attention.rules import load_rules
from chamberlain.attention.selection import Chosen, Situation, select
from chamberlain.attention.work import next_step, notice
from chamberlain.limits import MAX_REPLY_CHARS, NEXT_STEPS_MAX, NOTICE_PRIORITY, NOTICES_MAX
from chamberlain.schema.base import Model, ToolName
from chamberlain.schema.envelope import GuidanceItem, NextStep, Notice, Reply
from chamberlain.schema.registry import ToolSpec
from chamberlain.schema.tools import ToolRequest
from chamberlain.store.db import transaction
from chamberlain.store.migrate import stamp

log = logging.getLogger("chamberlain.attention")

PULL_TOOLS = frozenset({ToolName.START, ToolName.NEXT})
"""Replies that already carry work items never piggyback notices (the pull route)."""

_HANDLE_PREFIXES = ("card_", "act_", "op_", "grant_", "msg_", "cev_", "con_", "art_", "evt_")


@dataclass
class _Draft:
    full: list[Chosen]
    pointers: list[str]
    pointer_versions: dict[str, int]
    notices: list[tuple[EventRow, Notice]]
    next: tuple[NextStep, ...]


def _size(reply: Reply[Model]) -> int:
    return len(reply.model_dump_json(exclude_none=True))


def _named_subjects(request: dict[str, object]) -> list[str]:
    found = [v for v in request.values() if isinstance(v, str) and v.startswith(_HANDLE_PREFIXES)]
    return sorted(set(found))


class AttentionHooks:
    """Implements `app.context.Hooks`."""

    def tick(self, ctx: Context) -> None:
        tick.tick(ctx)

    def decorate(
        self, ctx: Context, spec: ToolSpec, request: ToolRequest, reply: Reply[Model]
    ) -> Reply[Model]:
        try:
            return self._decorate(ctx, spec, request, reply)
        except Exception:
            log.exception("decorating the reply failed")
            return reply

    def _decorate(
        self, ctx: Context, spec: ToolSpec, request: ToolRequest, reply: Reply[Model]
    ) -> Reply[Model]:
        db = ctx.db
        session = ctx.session
        if session is None and reply.data is not None:
            named = getattr(reply.data, "session", None)
            session = named if isinstance(named, str) else None
        row = None if session is None else repo.get_session(db, session)
        active = row is not None and row.ended_at is None
        work_events = self._work_events(ctx, reply)
        request_dict = request.model_dump(mode="json", exclude_none=True)
        concerned = repo.events_by_subject(db, _named_subjects(request_dict))
        notice_events: list[EventRow] = []
        if active and session is not None and spec.name not in PULL_TOOLS:
            shown = {e.handle for e in work_events}
            notice_events = [
                e
                for e in repo.candidates(
                    db, session, NOTICES_MAX + len(shown), min_priority=NOTICE_PRIORITY
                )
                if e.handle not in shown
            ][:NOTICES_MAX]
        events = {e.handle: e for e in [*work_events, *notice_events, *concerned]}
        situation = Situation(
            tool=spec.name.value,
            request=request_dict,
            reply=reply.model_dump(mode="json", exclude_none=True),
            error_code=None if reply.error is None else reply.error.code.value,
            error_category=None if reply.error is None else reply.error.category.value,
            events=tuple(events.values()),
        )
        rules = apply_overrides(load_rules(), ctx.data_root).rules
        ledger = repo.delivered_versions(db, session) if active and session is not None else {}
        picked = select(rules, situation, ledger)
        draft = _Draft(
            full=list(picked.full),
            pointers=[r.id for r in picked.pointers],
            pointer_versions={r.id: r.version for r in picked.pointers},
            notices=[(e, notice(e)) for e in notice_events],
            next=reply.next,
        )
        if not reply.next and draft.notices:
            draft.next = tuple(next_step(e) for e, _ in draft.notices)[:NEXT_STEPS_MAX]
        result = self._fit(reply, draft)
        if active and session is not None:
            self._record(ctx, session, result, draft)
        return result

    @staticmethod
    def _work_events(ctx: Context, reply: Reply[Model]) -> list[EventRow]:
        work: object = getattr(reply.data, "work", None)
        if not isinstance(work, tuple):
            return []
        items = cast(tuple[object, ...], work)
        handles = [h for h in (getattr(w, "event", None) for w in items) if isinstance(h, str)]
        return repo.events_by_handles(ctx.db, handles)

    @staticmethod
    def _build(reply: Reply[Model], draft: _Draft, *, notices: bool) -> Reply[Model]:
        items = [
            GuidanceItem(id=c.rule.id, version=c.rule.version, text=c.rule.text) for c in draft.full
        ] + [
            GuidanceItem(id=rid, version=draft.pointer_versions[rid], more=f"guide {rid}")
            for rid in draft.pointers
        ]
        shown = tuple(n for _, n in draft.notices) if notices else ()
        steps = draft.next if (notices or reply.next) else ()
        return reply.model_copy(update={"guidance": tuple(items), "notices": shown, "next": steps})

    def _fit(self, reply: Reply[Model], draft: _Draft) -> Reply[Model]:
        """Degrade guidance to pointers, then drop pointers, then notices (A-19)."""
        result = self._build(reply, draft, notices=True)
        while _size(result) > MAX_REPLY_CHARS and draft.full:
            lowest = min(draft.full, key=lambda c: (c.rule.priority, c.rule.id))
            draft.full.remove(lowest)
            if len(draft.pointers) < 3:
                draft.pointers.append(lowest.rule.id)
                draft.pointer_versions[lowest.rule.id] = lowest.rule.version
            result = self._build(reply, draft, notices=True)
        if _size(result) > MAX_REPLY_CHARS:
            draft.pointers.clear()
            result = self._build(reply, draft, notices=True)
        if _size(result) > MAX_REPLY_CHARS:
            draft.notices.clear()
            draft.next = reply.next
            result = self._build(reply, draft, notices=False)
        return result

    @staticmethod
    def _record(ctx: Context, session: str, result: Reply[Model], draft: _Draft) -> None:
        """Step 6, after the reply is final: full texts only, and notices actually shown."""
        shown_full = {g.id for g in result.guidance if g.text is not None}
        try:
            with transaction(ctx.db):
                for chosen in draft.full:
                    if chosen.rule.id in shown_full and chosen.rule.once_per != "always":
                        for scope in chosen.scopes:
                            repo.record_delivery(
                                ctx.db,
                                session,
                                chosen.rule.id,
                                chosen.rule.version,
                                scope,
                                stamp(ctx.clock),
                            )
                shown = {n.event for n in result.notices}
                events = [e for e, _ in draft.notices if e.handle in shown]
                inbox.deliver(ctx.db, ctx.clock, session, events)
        except Exception:
            log.exception("recording deliveries failed")


HOOKS = AttentionHooks()
