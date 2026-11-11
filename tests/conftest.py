"""Give collected modules and every test separate filesystem ownership."""

import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest


def pytest_configure(config):
    # Imports may compute default paths during collection, before fixtures run.
    home = TemporaryDirectory(prefix="xian-abci-collection-")
    environment = pytest.MonkeyPatch()
    environment.setenv("HOME", home.name)
    config.add_cleanup(home.cleanup)
    config.add_cleanup(environment.undo)


@pytest.fixture
def test_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def node_home(test_home):
    home = test_home / ".cometbft"
    home.mkdir()
    return home


@pytest.fixture(autouse=True)
def isolate_default_paths(node_constants, monkeypatch):
    from contracting import constants as contracting_constants

    from xian.constants import Constants

    for name, value in vars(node_constants).items():
        if name.isupper():
            monkeypatch.setattr(Constants, name, value)
    monkeypatch.setattr(contracting_constants, "STORAGE_HOME", node_constants.STORAGE_HOME)
    # Restore the starting directory even when a test changes it directly.
    monkeypatch.chdir(Path.cwd())


@pytest.fixture
def node_constants(node_home):
    from xian.constants import Constants

    class NodeConstants(Constants):
        COMETBFT_HOME = node_home
        COMETBFT_CONFIG = node_home / "config/config.toml"
        XIAN_CONFIG = node_home / "config/xian.toml"
        COMETBFT_GENESIS = node_home / "config/genesis.json"
        STORAGE_HOME = node_home / "xian"

    return NodeConstants


@pytest.fixture
def node_fixture(node_home, node_constants, request):
    source = request.path.parent / "fixtures/.cometbft-fixture"
    shutil.copytree(source, node_home, dirs_exist_ok=True)
    if request.instance is not None:
        request.instance.constants = node_constants
    return node_home
