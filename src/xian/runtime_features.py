from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from contracting.runtime_features import (
    DEFAULT_CHAIN_RUNTIME_FEATURES,
    RUNTIME_FEATURE_STATE_PREFIX,
    RUNTIME_FEATURE_ZK,
    normalize_runtime_features,
    runtime_feature_state_key,
    set_driver_runtime_features,
)
from contracting.stdlib.bridge import zk as zk_bridge

ZK_VERIFIER_MISSING = (
    "this node has no native zk verifier but the chain's zk runtime feature is on. "
    "Shielded-contract proof verification is consensus critical; a node missing the "
    "backend would fork the network. Install xian-tech-abci[zk] "
    "(or xian-tech-contracting[zk])."
)


class RuntimeFeatureUnsupportedError(RuntimeError):
    """This node cannot execute the chain under its runtime features."""


def require_runtime_feature_support(features: Mapping[str, bool]) -> None:
    """Refuse runtime features this node cannot execute.

    Shielded contracts call ``zk.verify_groth16(...)`` on the consensus path;
    without the native backend that call raises instead of returning a
    verdict, so the node would compute different transaction outcomes.
    """
    if features.get(RUNTIME_FEATURE_ZK, False) and not zk_bridge.is_available():
        raise RuntimeFeatureUnsupportedError(ZK_VERIFIER_MISSING)


def runtime_feature_change(key: str, value: Any) -> tuple[str, bool]:
    """The feature and value a write of ``value`` to state ``key`` sets.

    Only a supported feature's own key and a JSON boolean, which is what
    genesis writes, are accepted.
    """
    feature = key.removeprefix(RUNTIME_FEATURE_STATE_PREFIX)
    runtime_feature_state_key(feature)
    if not isinstance(value, bool):
        raise ValueError(f"runtime feature {feature!r} must be true or false")
    return feature, value


@dataclass(frozen=True, slots=True)
class RuntimeFeatureResolution:
    features: dict[str, bool]
    source: str


def install_driver_runtime_features(
    driver,
    resolution: RuntimeFeatureResolution,
) -> dict[str, bool]:
    return set_driver_runtime_features(driver, resolution.features)


def resolve_runtime_features(
    *,
    driver=None,
    genesis: Mapping[str, Any] | None = None,
) -> RuntimeFeatureResolution:
    explicit = _explicit_features_from_driver(driver)
    source = "state"
    if explicit is None:
        explicit = _explicit_features_from_genesis(genesis)
        source = "genesis"

    if explicit is None:
        return RuntimeFeatureResolution(
            features=normalize_runtime_features(DEFAULT_CHAIN_RUNTIME_FEATURES),
            source="default",
        )

    return RuntimeFeatureResolution(
        features=normalize_runtime_features(explicit),
        source=source,
    )


def genesis_runtime_feature_entries(
    features: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    resolved = normalize_runtime_features(features)
    return [
        {
            "key": runtime_feature_state_key(feature),
            "value": enabled,
        }
        for feature, enabled in sorted(resolved.items())
    ]


def _explicit_features_from_driver(driver) -> dict[str, bool] | None:
    if driver is None:
        return None
    driver_get = getattr(driver, "get", None)
    if driver_get is None:
        return None
    features = {}
    for feature in DEFAULT_CHAIN_RUNTIME_FEATURES:
        value = driver_get(runtime_feature_state_key(feature))
        if value is not None:
            features[feature] = value
    if not features:
        return None
    return normalize_runtime_features(features)


def _explicit_features_from_genesis(genesis: Mapping[str, Any] | None) -> dict[str, bool] | None:
    features = {}
    state = _abci_genesis_state(genesis)
    for entry in state:
        if not isinstance(entry, Mapping):
            continue
        key = entry.get("key")
        if not isinstance(key, str):
            continue
        for feature in DEFAULT_CHAIN_RUNTIME_FEATURES:
            if key == runtime_feature_state_key(feature):
                features[feature] = entry.get("value")
    if not features:
        return None
    return normalize_runtime_features(features)


def _abci_genesis_state(genesis: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    if not isinstance(genesis, Mapping):
        return []
    abci_genesis = genesis.get("abci_genesis", genesis)
    if not isinstance(abci_genesis, Mapping):
        return []
    state = abci_genesis.get("genesis")
    if not isinstance(state, list):
        return []
    return state
