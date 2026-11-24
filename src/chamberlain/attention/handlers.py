"""Tool handlers of the attention track: start, next, guide.

Contract: docs/contracts/07-attention.md and 02-tools.md. `app/wiring.py` reads the names below;
this track fills them and never edits the app package.
"""

from chamberlain.app.context import DoctorCheckFn, Handler, Hooks
from chamberlain.schema.base import ToolName
from chamberlain.schema.tools import DoctorCheck

HANDLERS: dict[ToolName, Handler] = {}
"""Tool name to handler, for the tools this track owns (schema/registry.py)."""

DOCTOR_CHECKS: list[tuple[DoctorCheck, DoctorCheckFn]] = []
"""Checks this track contributes to `doctor`."""

HOOKS: Hooks | None = None
"""The tick and reply decorator (A-6, A-19). Set once attention/hooks.py exists."""
