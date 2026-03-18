from __future__ import annotations

import os
from pathlib import Path

CONFIGS_REPO_NAME = "xian-configs"
CONTAINER_CONFIGS_DIR = Path("/usr/src/app/xian-configs")
NETWORKS_SUBPATH = Path("networks")
CONTRACTS_SUBPATH = Path("contracts")


def resolve_configs_dir(explicit: Path | None = None) -> Path:
    candidates: list[Path] = []

    if explicit is not None:
        candidates.append(explicit)

    env_value = os.environ.get("XIAN_CONFIGS_DIR")
    if env_value:
        candidates.append(Path(env_value))

    repo_root = Path(__file__).resolve().parents[2]
    candidates.append(repo_root.parent / CONFIGS_REPO_NAME)
    candidates.append(CONTAINER_CONFIGS_DIR)

    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved.exists():
            return resolved

    raise FileNotFoundError(
        "unable to resolve xian-configs directory; "
        "set XIAN_CONFIGS_DIR or use the shared sibling workspace layout"
    )


def resolve_contracts_dir(configs_dir: Path | None = None) -> Path:
    path = resolve_configs_dir(configs_dir) / CONTRACTS_SUBPATH
    if not path.exists():
        raise FileNotFoundError(f"contracts directory not found: {path}")
    return path


def resolve_network_dir(
    network_name: str, configs_dir: Path | None = None
) -> Path:
    path = resolve_configs_dir(configs_dir) / NETWORKS_SUBPATH / network_name
    if not path.exists():
        raise FileNotFoundError(f"network directory not found: {path}")
    return path


def resolve_network_manifest_file(
    network_name: str, configs_dir: Path | None = None
) -> Path:
    path = resolve_network_dir(network_name, configs_dir) / "manifest.json"
    if not path.exists():
        raise FileNotFoundError(f"network manifest not found: {path}")
    return path


def resolve_network_genesis_file(
    network_name: str, configs_dir: Path | None = None
) -> Path:
    path = resolve_network_dir(network_name, configs_dir) / "genesis.json"
    if not path.exists():
        raise FileNotFoundError(f"network genesis not found: {path}")
    return path


def resolve_genesis_source(
    source: str, configs_dir: Path | None = None
) -> Path:
    configs_root = resolve_configs_dir(configs_dir)
    raw_path = Path(source)

    candidates: list[Path] = []
    if raw_path.is_absolute():
        candidates.append(raw_path)
    else:
        candidates.append(configs_root / raw_path)
        if raw_path.suffix == "" and raw_path.parent == Path("."):
            candidates.append(
                configs_root / NETWORKS_SUBPATH / source / "genesis.json"
            )

    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved.exists():
            return resolved

    raise FileNotFoundError(
        f"unable to resolve genesis source {source!r} in xian-configs"
    )
