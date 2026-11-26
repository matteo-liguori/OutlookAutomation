"""Run an agent tool against the current connection profile for CLI and MCP."""

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from chamberlain.connections import open_app
from chamberlain.schema.base import Model
from chamberlain.schema.envelope import Reply, error_reply
from chamberlain.schema.errors import ChamberlainError, ErrorCode

DATA_ENV = "CHAMBERLAIN_DATA"
ToolCaller = Callable[[str, Mapping[str, Any]], Reply[Model]]


def data_root(arg: str | None) -> Path:
    """The `--data` argument or `CHAMBERLAIN_DATA`; no root is a `config.invalid` error."""
    data = arg or os.environ.get(DATA_ENV)
    if not data:
        raise ChamberlainError(
            ErrorCode.CONFIG_INVALID,
            "No data root is set.",
            fix=f"Set {DATA_ENV} or pass --data before the tool name.",
        )
    return Path(data)


def configured_caller(data_root: Path) -> ToolCaller:
    """Call tools on the app that `config/connections.yaml` describes, one app per call.

    A broken profile is a `config.invalid` reply to the call, never a crashed server.
    """

    def call_tool(name: str, arguments: Mapping[str, Any]) -> Reply[Model]:
        try:
            app = open_app(data_root)
        except ChamberlainError as error:
            return error_reply(error)
        try:
            return app.call(name, arguments)
        finally:
            app.close()

    return call_tool
