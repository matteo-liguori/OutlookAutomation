"""Minting and inspecting handles.

Contract: docs/contracts/01-domain.md (D-3 to D-6).
"""

import hashlib
import secrets
from enum import StrEnum

from chamberlain.schema.errors import ChamberlainError, ErrorCode


class HandleKind(StrEnum):
    CARD = "card"
    ACTION = "act"
    NOTE = "note"
    EVIDENCE = "evd"
    ARTIFACT = "art"
    GRANT = "grant"
    OPERATION = "op"
    EVENT = "evt"
    SESSION = "ses"
    RUN = "run"
    MESSAGE = "msg"
    CALENDAR_EVENT = "cev"
    CONTACT = "con"


REF_KINDS = frozenset({HandleKind.MESSAGE, HandleKind.CALENDAR_EVENT, HandleKind.CONTACT})
_TOKEN_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")


def new_handle(kind: HandleKind) -> str:
    """A fresh random handle. Not for refs: use `ref_handle` (D-6)."""
    if kind in REF_KINDS:
        raise ValueError("ref handles are deterministic; use ref_handle")
    return f"{kind.value}_{secrets.token_hex(8)}"


def ref_handle(kind: HandleKind, source: str, locator: str) -> str:
    """The deterministic handle for a remote item (D-5, D-6)."""
    if kind not in REF_KINDS:
        raise ValueError("only msg, cev and con handles are refs")
    digest = hashlib.sha256(f"{source}\n{locator}".encode()).hexdigest()
    return f"{kind.value}_{digest[:20]}"


def handle_kind(handle: str) -> HandleKind:
    """The kind of a handle, or `request.unknown_handle` if it is not one."""
    prefix, _, token = handle.partition("_")
    if prefix in HandleKind and 8 <= len(token) <= 32 and set(token) <= _TOKEN_CHARS:
        return HandleKind(prefix)
    raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "That is not a handle.")
