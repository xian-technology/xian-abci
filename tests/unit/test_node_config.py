from dataclasses import asdict

import pytest

from xian import toml_utils
from xian.node_config import validate_node_config
from xian.node_defaults import RUNTIME_DEFAULTS
from xian.node_setup import DEFAULT_XIAN_CONFIG_TOML
from xian.services.bds.config import BdsConfig
from xian.services.snapshot_limits import SnapshotLimits


def test_generated_runtime_config_is_supported():
    config = toml_utils.loads(DEFAULT_XIAN_CONFIG_TOML)
    config["bds"] = asdict(BdsConfig())
    config.update({"snapshot_" + key: value for key, value in asdict(SnapshotLimits()).items()})
    config["simulation_max_chi"] = 50000
    assert validate_node_config(config) == BdsConfig()


def test_unknown_settings_are_named_without_echoing_values():
    with pytest.raises(ValueError) as raised:
        validate_node_config({**asdict(RUNTIME_DEFAULTS), "typo": "secret", "another": {}})
    assert str(raised.value) == "unknown xian.toml key(s): another, typo"


@pytest.mark.parametrize("config", [None, {}, asdict(RUNTIME_DEFAULTS)])
def test_partial_runtime_settings_use_defaults(config):
    assert validate_node_config(config) == BdsConfig()


@pytest.mark.parametrize("key", ["tx_fee_mode", "max_tx_chi", "max_block_chi"])
def test_fee_policy_fields_belong_to_chain_state(key):
    with pytest.raises(ValueError, match=f"unknown xian.toml key\\(s\\): {key}"):
        validate_node_config({key: "value"})


def test_nested_settings_are_validated_before_startup():
    with pytest.raises(ValueError, match="unknown bds"):
        validate_node_config({"bds": {"typo": "secret"}})


def test_runtime_receives_the_parsed_bds_configuration():
    config = validate_node_config({"bds": {"host": "indexer", "port": 5544}})
    assert config == BdsConfig(host="indexer", port=5544)
