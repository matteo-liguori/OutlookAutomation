"""The one entry point both surfaces call.

Contract: docs/contracts/02-tools.md (T-3, T-5, T-16) and
docs/contracts/03-envelope-errors.md (E-1, E-2, E-14). Shared file.
"""

import logging
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from chamberlain.app.context import Context, Handler, Hooks, SyncBudget
from chamberlain.domain.canonical import sha256_text
from chamberlain.limits import MAX_REPLY_CHARS, SUMMARY_MAX
from chamberlain.schema.base import Model, ToolName
from chamberlain.schema.envelope import Reply, error_reply
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.schema.registry import ToolSpec, check_next_step, get_tool, parse_request
from chamberlain.schema.safelog import log_failure
from chamberlain.schema.tools import ToolRequest
from chamberlain.store.db import transaction

log = logging.getLogger("chamberlain.app")


def _reply_size(reply: Reply[Model]) -> int:
    return len(reply.model_dump_json(exclude_none=True))


REPLY_SIZE_PATTERN = r"^[a-z]+: \S+ \((\d+) chars\)$"
"""The audit summary of a `tool.call` row; `app/metrics.py` parses the size from it."""


class App:
    """Validates, ticks, dispatches to a handler, decorates and bounds the reply."""

    def __init__(
        self,
        context: Context,
        handlers: Mapping[ToolName, Handler],
        hooks: Hooks | None = None,
    ) -> None:
        self._context = context
        self._handlers = dict(handlers)
        self._hooks = hooks

    @property
    def context(self) -> Context:
        """The shared context (no session): what the operator commands run against."""
        return self._context

    def close(self) -> None:
        """Close the store. The app is unusable afterwards."""
        self._context.db.close()

    def call(self, tool: str, raw: Mapping[str, Any]) -> Reply[Model]:
        """Run one tool call and return the envelope. Never raises."""
        spec: ToolSpec | None = None
        try:
            spec = get_tool(tool)
            request = parse_request(spec, dict(raw))
            reply = self._run(spec, request)
        except ChamberlainError as error:
            reply = error_reply(error)
        except Exception:
            log_failure(log, "unexpected failure in tool %s", tool)
            reply = error_reply(ChamberlainError(ErrorCode.INTERNAL_UNEXPECTED, "Harness fault."))
        size = _reply_size(reply)
        reply = self._bounded(reply, size)
        self._audit(spec, raw, reply, size)
        return reply

    def _run(self, spec: ToolSpec, request: ToolRequest) -> Reply[Model]:
        if spec.needs_session and request.session is None:
            raise ChamberlainError(ErrorCode.SESSION_REQUIRED, "This tool needs a session.")
        ctx = replace(self._context, session=request.session, sync_budget=SyncBudget())
        try:
            if self._hooks is not None:
                self._hooks.guard(ctx, spec, request)
            handler = self._handlers.get(spec.name)
            if handler is None:
                raise ChamberlainError(
                    ErrorCode.TOOL_NOT_IMPLEMENTED, f"{spec.name.value} is not built yet."
                )
            self._tick(ctx)
            result = handler(ctx, request)
        except ChamberlainError as error:
            reply = error_reply(error)
        else:
            if not isinstance(result.data, spec.response):
                raise TypeError(f"{spec.name.value} handler returned {type(result.data).__name__}")
            for step in result.next:
                check_next_step(step)
            reply = Reply[Model](
                ok=True, data=result.data, coverage=result.coverage, next=result.next
            )
        if ctx.sources.demo and reply.coverage is not None:
            marked = reply.coverage.model_copy(update={"demo": True})
            reply = reply.model_copy(update={"coverage": marked})
        if self._hooks is not None:
            reply = self._hooks.decorate(ctx, spec, request, reply)
        return reply

    def _tick(self, ctx: Context) -> None:
        """A tick never fails a tool call (A-7)."""
        if self._hooks is None:
            return
        try:
            self._hooks.tick(ctx)
        except Exception:
            log_failure(log, "tick failed")

    @staticmethod
    def _bounded(reply: Reply[Model], size: int) -> Reply[Model]:
        """Enforce the reply cap last (E-2)."""
        if size <= MAX_REPLY_CHARS:
            return reply
        log.error("reply of %d characters exceeds the cap", size)
        return error_reply(
            ChamberlainError(ErrorCode.REPLY_TOO_LARGE, "The reply was too large to return.")
        )

    def _audit(
        self, spec: ToolSpec | None, raw: Mapping[str, Any], reply: Reply[Model], size: int
    ) -> None:
        """One `tool.call` row per call (G-23): hashes, codes and the reply size, never content.

        The size (before the cap) is what `doctor --metrics` reads for `largest_reply_chars_7d`.
        """
        name = spec.name.value if spec is not None else "unknown"
        outcome = "ok" if reply.error is None else reply.error.code.value
        session = raw.get("session")
        actor = f"agent:{session}" if isinstance(session, str) else "agent:none"
        try:
            with transaction(self._context.db):
                self._context.services.audit.append(
                    actor=actor[:SUMMARY_MAX],
                    kind="tool.call",
                    subject=None,
                    summary=f"{name}: {outcome} ({size} chars)",
                    payload_sha256=sha256_text(repr(sorted(raw.items(), key=lambda kv: kv[0]))),
                )
        except Exception:
            log_failure(log, "could not write the audit row for %s", name)
