"""Replay remains the default even when the optional Comet section is absent."""

import pytest

from xian.state_sync_policy import validate_comet_state_sync_disabled


@pytest.mark.parametrize("config", [{}, {"statesync": {}}, {"statesync": {"enable": False}}])
def test_default_replay_configuration_is_supported(config):
    validate_comet_state_sync_disabled(config)


@pytest.mark.parametrize(
    "section", [None, [], "false", {"enable": "false"}, {"enable": 0}, {"enable": True}]
)
def test_invalid_or_enabled_p2p_configuration_is_rejected(section):
    with pytest.raises(ValueError):
        validate_comet_state_sync_disabled({"statesync": section})
