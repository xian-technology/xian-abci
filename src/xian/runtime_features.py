from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from contracting.runtime_features import (
    DEFAULT_CHAIN_RUNTIME_FEATURES,
    normalize_runtime_features,
    runtime_feature_state_key,
    set_driver_runtime_features,
)


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
