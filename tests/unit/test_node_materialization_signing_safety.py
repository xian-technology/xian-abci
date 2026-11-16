import json
from pathlib import Path
from unittest.mock import patch

import pytest

from xian.node_setup import (
    NodeConfigOptions,
    build_priv_validator_key,
    materialize_cometbft_home,
    render_cometbft_config,
    render_xian_config,
    write_json,
)
from xian.toml_utils import load as load_toml


def materialize(home: Path, *, config=None):
    return materialize_cometbft_home(
        home=home,
        config=config or render_cometbft_config(options=NodeConfigOptions(moniker="safe-node")),
        xian_config=render_xian_config(options=NodeConfigOptions(moniker="safe-node")),
        genesis={"chain_id": "new-chain", "validators": [], "abci_genesis": {}},
        priv_validator_key=build_priv_validator_key("01" * 32),
        overwrite=True,
    )


def test_private_writer_creates_a_new_nested_parent_without_prior_setup(tmp_path):
    path = tmp_path / "new" / "nested" / "secret.json"

    write_json(path, {"key": "private"}, private=True)

    assert json.loads(path.read_text()) == {"key": "private"}
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_overwrite_cannot_recreate_missing_signing_state_for_an_existing_identity(tmp_path):
    home = tmp_path / "node"
    materialize(home)
    state = home / "data/priv_validator_state.json"
    state.unlink()
    config = home / "config/config.toml"
    before = config.read_bytes()

    with pytest.raises(ValueError, match="signing state"):
        materialize(home)

    assert not state.exists()
    assert config.read_bytes() == before


def test_configured_custom_identity_alone_prevents_missing_signing_state_recreation(tmp_path):
    home = tmp_path / "node"
    materialize(home)
    default_key = home / "config/priv_validator_key.json"
    default_key.rename(home / "config/custom-key.json")
    (home / "data/priv_validator_state.json").unlink()
    path = home / "config/config.toml"
    path.write_text(path.read_text().replace("config/priv_validator_key.json", "config/custom-key.json"))
    config = load_toml(path)

    with pytest.raises(ValueError, match="signing state"):
        materialize(home, config=config)

    assert not default_key.exists()
    assert not (home / "data/priv_validator_state.json").exists()


def test_overwrite_cannot_switch_a_used_custom_signer_back_to_zero_default_state(tmp_path):
    home = tmp_path / "node"
    materialize(home)
    custom = home / "data/custom-signing.json"
    signed = {"height": "100", "round": 0, "step": 3, "signature": "signed"}
    custom.write_text(json.dumps(signed))
    config_path = home / "config/config.toml"
    text = config_path.read_text().replace(
        'priv_validator_state_file = "data/priv_validator_state.json"',
        'priv_validator_state_file = "data/custom-signing.json"',
    )
    config_path.write_text(text)
    before = config_path.read_bytes()

    with pytest.raises(ValueError, match="signing state"):
        materialize(home)

    assert config_path.read_bytes() == before
    assert json.loads(custom.read_text()) == signed


def test_overwrite_preserves_existing_signing_high_water_mark(tmp_path):
    home = tmp_path / "node"
    materialize(home)
    state = home / "data/priv_validator_state.json"
    signed = {"height": "100", "round": 2, "step": 3}
    state.write_text(json.dumps(signed))
    materialize(home, config=load_toml(home / "config/config.toml"))
    assert json.loads(state.read_text()) == signed


def test_runtime_data_without_identity_or_signing_state_is_not_reinitialized(tmp_path):
    home = tmp_path / "node"
    data = home / "data"
    data.mkdir(parents=True)
    (data / "blockstore.db").mkdir()

    with pytest.raises(ValueError, match="signing state"):
        materialize(home)

    assert not (home / "config").exists()
    assert not (data / "priv_validator_state.json").exists()


def test_overwrite_honors_existing_custom_signing_state_location(tmp_path):
    home = tmp_path / "node"
    materialize(home)
    default = home / "data/priv_validator_state.json"
    custom = home / "data/custom-signing.json"
    default.rename(custom)
    custom.write_text(json.dumps({"height": "100", "round": 2, "step": 3}))
    config_path = home / "config/config.toml"
    config_path.write_text(config_path.read_text().replace(
        'priv_validator_state_file = "data/priv_validator_state.json"',
        'priv_validator_state_file = "data/custom-signing.json"',
    ))

    result = materialize(home, config=load_toml(config_path))

    assert result["priv_validator_state_path"] == str(custom)
    assert json.loads(custom.read_text())["height"] == "100"
    assert not default.exists()


def test_private_file_write_failure_preserves_the_previous_identity_bytes(tmp_path):
    key = tmp_path / "priv_validator_key.json"
    write_json(key, {"priv_key": "original-key"}, private=True)
    original = key.read_bytes()

    with patch("xian.node_setup.os.fchmod", side_effect=OSError("injected filesystem failure")):
        with pytest.raises(OSError, match="injected filesystem failure"):
            write_json(key, {"priv_key": "replacement-key"}, overwrite=True, private=True)

    assert key.read_bytes() == original


def test_private_creation_does_not_replace_an_identity_created_while_staging(tmp_path):
    key = tmp_path / "priv_validator_key.json"
    concurrent = b'{"priv_key":"concurrent-identity"}\n'

    with patch("xian.node_setup.os.fsync", side_effect=lambda _fd: key.write_bytes(concurrent)):
        with pytest.raises(FileExistsError):
            write_json(key, {"priv_key": "new-identity"}, private=True)

    assert key.read_bytes() == concurrent
