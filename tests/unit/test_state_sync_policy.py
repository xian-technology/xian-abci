"""Replay remains the default even when the optional Comet section is absent."""

import tomllib

import pytest

from xian.state_sync_policy import validate_comet_state_sync_disabled


@pytest.mark.parametrize(
    "config",
    [{}, {"statesync": {}}, {"statesync": {"enable": False}}, {"StateSync": {"EnAbLe": False}}],
)
def test_default_replay_configuration_is_supported(config):
    validate_comet_state_sync_disabled(config)


@pytest.mark.parametrize(
    "section", [None, [], "false", {"enable": "false"}, {"enable": 0}, {"enable": True}]
)
def test_invalid_or_enabled_p2p_configuration_is_rejected(section):
    with pytest.raises(ValueError):
        validate_comet_state_sync_disabled({"statesync": section})


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ("[StateSync]\nEnable = true", "verified full-node-store"),
        ("[statesync]\nENABLE = true", "verified full-node-store"),
        ("[STATESYNC]\nenable = true", "verified full-node-store"),
        ("StateSync = []", "statesync configuration must be an object"),
        ('[StateSync]\nEnable = "false"', "statesync.enable must be a boolean"),
        ("[statesync]\n[STATESYNC]\n", "duplicate statesync"),
        ("[statesync]\nenable = false\nEnable = false", "duplicate statesync.enable"),
        ('"StateSync.Enable" = true', "quoted dotted key"),
        ('[statesync]\n"Enable.extra" = true', "quoted dotted key"),
    ],
)
def test_comet_configuration_key_aliases_are_checked(config, message):
    with pytest.raises(ValueError, match=message):
        validate_comet_state_sync_disabled(tomllib.loads(config))
