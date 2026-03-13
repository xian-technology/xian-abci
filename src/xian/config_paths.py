from __future__ import annotations

import os
from pathlib import Path

CONFIGS_REPO_NAME = "xian-configs"
CONTAINER_CONFIGS_DIR = Path("/usr/src/app/xian-configs")
LEGACY_GENESIS_SUBPATH = Path("legacy") / "genesis"


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


def resolve_legacy_genesis_dir(configs_dir: Path | None = None) -> Path:
    return resolve_configs_dir(configs_dir) / LEGACY_GENESIS_SUBPATH


def resolve_legacy_genesis_file(
    filename: str, configs_dir: Path | None = None
) -> Path:
    path = resolve_legacy_genesis_dir(configs_dir) / filename
    if not path.exists():
        raise FileNotFoundError(f"legacy genesis file not found: {path}")
    return path


def resolve_legacy_contracts_dir(configs_dir: Path | None = None) -> Path:
    path = resolve_legacy_genesis_dir(configs_dir) / "contracts"
    if not path.exists():
        raise FileNotFoundError(f"legacy contracts directory not found: {path}")
    return path
