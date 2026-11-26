"""Fixtures for the writes track: a store, the fake provider and the service doubles."""

from collections.abc import Iterator

import pytest

from support.write_rig import WriteRig, make_rig


@pytest.fixture
def rig() -> Iterator[WriteRig]:
    made = make_rig()
    yield made
    made.close()
