from pathlib import Path

import pytest
from contracting.storage.driver import Driver

from xian.constants import Constants


@pytest.mark.parametrize("value", [11, 22])
def test_default_storage_is_private_to_each_test(tmp_path, value):
    assert Path.home().is_relative_to(tmp_path)
    assert Constants.COMETBFT_HOME.is_relative_to(Path.home())
    assert Constants.STORAGE_HOME.is_relative_to(Constants.COMETBFT_HOME)
    for path in (Constants.COMETBFT_CONFIG, Constants.XIAN_CONFIG, Constants.COMETBFT_GENESIS):
        assert path.is_relative_to(Constants.COMETBFT_HOME)

    with Driver(vm_profile="xian_vm_v2") as driver:
        assert driver.storage_home == Constants.STORAGE_HOME
        assert driver.get("con_isolation.value") is None
        driver.set("con_isolation.value", value)
        driver.commit()
        assert driver.get("con_isolation.value") == value

    with Driver(vm_profile="xian_vm_v2") as reopened:
        assert reopened.get("con_isolation.value") == value
