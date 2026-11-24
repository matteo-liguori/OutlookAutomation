"""Fixtures for the memory tests. Fake provider and temporary roots only."""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from memory_helpers import SESSION, add_session

from chamberlain.app.context import Context
from chamberlain.app.core import App
from chamberlain.memory.artifacts import ArtifactService
from chamberlain.memory.cards import CardService
from chamberlain.memory.handlers import HANDLERS
from chamberlain.ports.clock import ManualClock


@pytest.fixture
def session(db: sqlite3.Connection, clock: ManualClock) -> str:
    return add_session(db, clock, SESSION)


@pytest.fixture
def cards(ctx: Context) -> CardService:
    return CardService(ctx.db, ctx.clock)


@pytest.fixture
def store(ctx: Context) -> ArtifactService:
    return ArtifactService(ctx.db, ctx.clock, ctx.data_root)


@pytest.fixture
def action(cards: CardService) -> str:
    """An open action on a fresh card."""
    card = cards.create(title="Reply to the supplier")
    view = cards.add_action(
        card.handle, None, title="Send the quote", due_at=None, snooze_until=None
    )
    return view.actions[0].handle


@pytest.fixture
def app(ctx: Context) -> App:
    return App(ctx, HANDLERS)
