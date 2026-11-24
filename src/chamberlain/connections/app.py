"""The one function every surface calls to get a working app.

The CLI, the tick, the MCP server and the viewer all call `open_app`, so a configured
installation behaves the same from each of them.
"""

from pathlib import Path

from chamberlain.app.core import App
from chamberlain.app.wiring import build_app
from chamberlain.connections.build import build_runtime
from chamberlain.connections.load import load_connections
from chamberlain.connections.repo import sync_sources
from chamberlain.connectors.calendar_support.secrets import SecretStore
from chamberlain.ports.clock import Clock, SystemClock
from chamberlain.store.db import transaction
from chamberlain.store.migrate import stamp


def open_app(
    data_root: Path, *, clock: Clock | None = None, secrets: SecretStore | None = None
) -> App:
    """Load `config/connections.yaml`, build the connectors and assemble the app.

    Raises `ChamberlainError(config.invalid)` before the store is touched when the
    profile is malformed.
    """
    the_clock = clock or SystemClock()
    connections = load_connections(data_root)
    runtime = build_runtime(connections, data_root, the_clock, secrets=secrets)
    app = build_app(
        data_root,
        clock=the_clock,
        sources=runtime.sources,
        policy=runtime.policy,
        budgets=runtime.budgets,
    )
    try:
        with transaction(app.context.db) as db:
            sync_sources(db, connections, stamp(the_clock))
    except BaseException:
        app.close()
        raise
    return app
