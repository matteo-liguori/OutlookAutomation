"""Tool handlers of the writes track: propose, do, reconcile.

Contract: docs/contracts/04-writes.md, 05-grants.md, 02-tools.md. `app/wiring.py`
reads the names below; this track fills them and never edits the app package.
"""

from chamberlain.app.context import DoctorCheckFn, Handler
from chamberlain.schema.base import ToolName
from chamberlain.schema.tools import DoctorCheck

HANDLERS: dict[ToolName, Handler] = {}
"""Tool name to handler, for the tools this track owns (schema/registry.py)."""

DOCTOR_CHECKS: list[tuple[DoctorCheck, DoctorCheckFn]] = []
"""Checks this track contributes to `doctor`."""
