from __future__ import annotations

from dataclasses import replace

from xian.constants import Constants
from xian.services.bds.config import BdsConfig
from xian.utils.cometbft import load_xian_config


def resolve_bds_config(constants: Constants) -> BdsConfig:
    xian_config = load_xian_config(constants)
    bds_config = BdsConfig.from_runtime_settings(xian_config)
    if bds_config.spool_dir is None:
        bds_config = replace(
            bds_config,
            spool_dir=str(constants.STORAGE_HOME / "bds-spool"),
        )
    return bds_config
