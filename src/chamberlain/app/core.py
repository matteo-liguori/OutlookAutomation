"""The one entry point both surfaces call.

Contract: docs/contracts/02-tools.md (T-3, T-5, T-16) and
docs/contracts/03-envelope-errors.md (E-1, E-2, E-14). Shared file.
"""

import logging
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from chamberlain.app.context import Context, Handler, Hooks
from chamberlain.domain.canonical import sha256_text
from chamberlain.limits import MAX_REPLY_CHARS, SUMMARY_MAX
from chamberlain.schema.base import Model, ToolName
from chamberlain.schema.envelope import Reply, error_reply
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.schema.registry import ToolSpec, check_next_step, get_tool, parse_request
from chamberlain.schema.tools import ToolRequest
from chamberlain.store.db import transaction

log = logging.getLogger("chamberlain.app")


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
            log.exception("unexpected failure in tool %s", tool)
            reply = error_reply(ChamberlainError(ErrorCode.INTERNAL_UNEXPECTED, "Harness fault."))
        reply = self._bounded(reply)
        self._audit(spec, raw, reply)
        return reply

    def _run(self, spec: ToolSpec, request: ToolRequest) -> Reply[Model]:
        if spec.needs_session and request.session is None:
            raise ChamberlainError(ErrorCode.SESSION_REQUIRED, "This tool needs a session.")
        handler = self._handlers.get(spec.name)
        if handler is None:
            raise ChamberlainError(
                ErrorCode.TOOL_NOT_IMPLEMENTED, f"{spec.name.value} is not built yet."
            )
        ctx = replace(self._context, session=request.session)
        self._tick(ctx)
        try:
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
            log.exception("tick failed")

    @staticmethod
    def _bounded(reply: Reply[Model]) -> Reply[Model]:
        """Enforce the reply cap last (E-2)."""
        size = len(reply.model_dump_json(exclude_none=True))
        if size <= MAX_REPLY_CHARS:
            return reply
        log.error("reply of %d characters exceeds the cap", size)
        return error_reply(
            ChamberlainError(ErrorCode.REPLY_TOO_LARGE, "The reply was too large to return.")
        )

    def _audit(self, spec: ToolSpec | None, raw: Mapping[str, Any], reply: Reply[Model]) -> None:
        """One `tool.call` row per call (G-23): hashes and codes, never content."""
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
                    summary=f"{name}: {outcome}",
                    payload_sha256=sha256_text(repr(sorted(raw.items(), key=lambda kv: kv[0]))),
                )
        except Exception:
            log.exception("could not write the audit row for %s", name)
