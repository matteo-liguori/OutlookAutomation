"""The one function every surface calls to get a working app.

The CLI, the tick, the MCP server and the viewer all call `open_app`, so a configured
installation behaves the same from each of them.
"""

from pathlib import Path

from chamberlain.app.core import App
from chamberlain.app.wiring import build_app
from chamberlain.connections.build import build_runtime
from chamberlain.connections.load import load_connections
from chamberlain.connections.model import Connections
from chamberlain.connections.repo import sync_sources
from chamberlain.connectors.calendar_support.secrets import SecretStore
from chamberlain.ports.clock import Clock, SystemClock
from chamberlain.review.homes import agent_homes
from chamberlain.review.rounds import RoundRule
from chamberlain.review.scan import ReviewSettings, checkout_root
from chamberlain.runs.models import PassKind
from chamberlain.runs.schedule import ScheduleSpec, apply_schedules
from chamberlain.runs.updates import TagLister, git_tag_lister
from chamberlain.store.db import transaction
from chamberlain.store.migrate import stamp
from chamberlain.store.schema import AUTHORISED_SENDERS_KEY, MAIL_SYNC_KEY, set_state


def _specs(connections: Connections) -> list[ScheduleSpec]:
    return [
        ScheduleSpec(
            name=item.name,
            kind=PassKind(item.kind),
            at_local=item.at,
            zone=item.zone or connections.timezone,
            weekdays=frozenset(item.weekdays),
            enabled=item.enabled,
            presync_minutes=item.presync_minutes,
        )
        for item in connections.schedules
    ]


def _update_lister(connections: Connections) -> TagLister | None:
    return git_tag_lister(connections.updates.remote) if connections.updates.enabled else None


def _review(connections: Connections) -> ReviewSettings | None:
    if not connections.review.enabled:
        return None
    config = connections.review
    return ReviewSettings(
        homes=agent_homes(),
        checkout=checkout_root(),
        rule=RoundRule(config.new_patterns, config.min_days, config.max_days),
    )


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
        update_lister=_update_lister(connections),
        review=_review(connections),
    )
    try:
        with transaction(app.context.db) as db:
            sync_sources(db, connections, stamp(the_clock))
            set_state(
                db,
                the_clock,
                AUTHORISED_SENDERS_KEY,
                connections.authorised_senders.model_dump_json(),
            )
            set_state(db, the_clock, MAIL_SYNC_KEY, connections.mail_sync.model_dump_json())
        apply_schedules(app.context, _specs(connections))
    except BaseException:
        app.close()
        raise
    return app
