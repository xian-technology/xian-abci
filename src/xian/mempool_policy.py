"""The application admits transactions through CometBFT's flood mempool."""

from collections.abc import Mapping


def validate_comet_mempool_mode(config: Mapping) -> None:
    mempool = config.get("mempool", {})
    if not isinstance(mempool, Mapping) or mempool.get("type", "flood") != "flood":
        raise ValueError(
            "Xian's CometBFT 0.40 baseline supports only the flood mempool; "
            "app and nop mempool modes are unsupported"
        )
