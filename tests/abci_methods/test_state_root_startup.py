"""Node startup builds the state root from committed storage without decoding it in Python."""

import asyncio
import random
import sys
from pathlib import Path

import pytest
from contracting.storage.driver import Driver

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
from state_root_reference import driver_root  # noqa: E402
from test_state_root_from_storage import (  # noqa: E402
    EVERY_VALUE_TYPE,
    random_key,
    random_value,
)

from xian.xian_abci import Xian  # noqa: E402


def full_scans(monkeypatch):
    """Record the prefix of every merged state scan made through the driver."""
    scans = []
    iter_items = Driver.iter_items

    def recording(self, prefix=""):
        scans.append(prefix)
        return iter_items(self, prefix)

    monkeypatch.setattr(Driver, "iter_items", recording)
    return scans


def restarted(constants, write_state):
    """Start a node, let ``write_state`` commit state through it, and stop it."""
    app = asyncio.run(Xian.create(constants=constants))
    try:
        write_state(app.client.raw_driver)
        app.client.raw_driver.commit()
    finally:
        asyncio.run(app.close())
        app.client.close()


@pytest.mark.usefixtures("node_fixture")
def test_restart_builds_the_reference_root_without_decoding_state_in_python(
    node_constants, monkeypatch
):
    def write_state(driver):
        rng = random.Random(11)
        for key, value in EVERY_VALUE_TYPE.items():
            driver.set(key, value)
        for index in range(2000):
            driver.set(random_key(rng, index), random_value(rng))

    restarted(node_constants, write_state)
    scans = full_scans(monkeypatch)
    app = asyncio.run(Xian.create(constants=node_constants))
    try:
        assert "" not in scans
        expected_root, expected_leaves = driver_root(app.client.raw_driver)
        assert app.state_root_cache.root_hash.hex() == expected_root.hex()
        assert app.state_root_cache.leaf_count == expected_leaves
    finally:
        asyncio.run(app.close())
        app.client.close()
