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


def _comet_config_value(config: Mapping, path: str, default: object) -> object:
    # Viper lowercases keys and expands quoted dotted keys during unmarshalling.
    field = path.rsplit(".", 1)[-1]
    matches = [
        (key, value)
        for key, value in config.items()
        if isinstance(key, str) and key.lower().split(".", 1)[0] == field
    ]
    if len(matches) > 1:
        raise ValueError(f"Comet configuration has duplicate {path} keys")
    if not matches:
        return default
    key, value = matches[0]
    if "." in key:
        raise ValueError(f"Comet {path} configuration uses a quoted dotted key")
    return value


def validate_comet_state_sync_disabled(config: Mapping) -> None:
    state_sync = _comet_config_value(config, "statesync", {})
    if not isinstance(state_sync, Mapping):
        raise ValueError("Comet statesync configuration must be an object")
    require_p2p_state_sync_disabled(_comet_config_value(state_sync, "statesync.enable", False))
