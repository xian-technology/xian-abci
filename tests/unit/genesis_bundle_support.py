"""Test contract bundles for genesis builds."""

import json
from pathlib import Path

CONFIG_CONTRACTS = Path(__file__).resolve().parents[3] / "xian-configs" / "contracts"


def seed_chi_rate(contracts_dir: Path, rate: int = 20) -> None:
    """Add the canonical chi_cost contract to every bundle in a test
    contracts directory: genesis must seed a chi rate the node can price
    fees with."""
    (contracts_dir / "chi_cost.s.py").write_text(
        (CONFIG_CONTRACTS / "chi_cost.s.py").read_text(), encoding="utf-8"
    )
    for bundle in contracts_dir.glob("contracts_*.json"):
        payload = json.loads(bundle.read_text(encoding="utf-8"))
        payload["contracts"].append(
            {"name": "chi_cost", "owner": None, "constructor_args": {"initial_rate": rate}}
        )
        bundle.write_text(json.dumps(payload), encoding="utf-8")
