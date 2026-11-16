"""The upstream runtime supports replay and complete native-store restore."""

from collections.abc import Mapping

P2P_STATE_SYNC_UNSUPPORTED = (
    "P2P application state sync is unsupported with upstream CometBFT; "
    "use ordinary replay or a verified full-node-store snapshot"
)


def require_p2p_state_sync_disabled(enable: object) -> None:
    if type(enable) is not bool:
        raise ValueError("Comet statesync.enable must be a boolean")
    if enable:
        raise ValueError(P2P_STATE_SYNC_UNSUPPORTED)


def validate_comet_state_sync_disabled(config: Mapping) -> None:
    state_sync = config.get("statesync", {})
    if not isinstance(state_sync, Mapping):
        raise ValueError("Comet statesync configuration must be an object")
    require_p2p_state_sync_disabled(state_sync.get("enable", False))
