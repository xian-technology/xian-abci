"""Unsupported CometBFT mempools must fail before writing application state."""

from unittest.mock import patch

import pytest
from test_evidence_retention import app as app

from xian.mempool_policy import validate_comet_mempool_mode
from xian.xian_abci import Xian


@pytest.mark.parametrize("mode", ["app", "nop", "unknown", "", None])
def test_node_refuses_unsupported_mempool_modes_before_opening_state(app, mode):
    config = dict(app.cometbft_config)
    config["mempool"] = {"type": mode}
    with (
        patch("xian.xian_abci.load_tendermint_config", return_value=config),
        patch(
            "xian.xian_abci.initialize_execution_policy",
            side_effect=AssertionError("wrote before mempool validation"),
        ),
    ):
        with pytest.raises(ValueError, match="CometBFT.*flood.*mempool"):
            Xian(constants=app.constants)


@pytest.mark.parametrize("config", [{}, {"mempool": {}}, {"mempool": {"type": "flood"}}])
def test_default_and_explicit_flood_modes_are_accepted(config):
    validate_comet_mempool_mode(config)


@pytest.mark.parametrize("value", [None, [], "app", 7])
def test_malformed_mempool_sections_are_rejected(value):
    with pytest.raises(ValueError, match="CometBFT.*flood.*mempool"):
        validate_comet_mempool_mode({"mempool": value})
