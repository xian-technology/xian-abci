from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from xian.exceptions import TransactionException

PAID_METERED = "paid_metered"
FREE_METERED = "free_metered"
SYSTEM_UNMETERED = "system_unmetered"

DEFAULT_TX_FEE_MODE = PAID_METERED
DEFAULT_FREE_TX_MAX_CHI = 1_000_000
DEFAULT_FREE_BLOCK_MAX_CHI = 20_000_000

SUPPORTED_TX_FEE_MODES = frozenset({PAID_METERED, FREE_METERED})

# Consensus state key holding the chain's fee policy. Genesis writes it; every
# validator reads it from state, so nodes cannot disagree through local config.
FEE_POLICY_KEY = "__fee_policy"
FEE_POLICY_FIELDS = ("tx_fee_mode", "free_tx_max_chi", "free_block_max_chi")


def normalize_tx_fee_mode(mode: object | None) -> str:
    normalized = str(mode or DEFAULT_TX_FEE_MODE).strip().lower()
    if normalized not in SUPPORTED_TX_FEE_MODES:
        raise ValueError(f"tx_fee_mode must be one of {sorted(SUPPORTED_TX_FEE_MODES)}")
    return normalized


def _positive_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be an integer greater than zero")
    return value


def fee_policy_state(settings: Mapping[str, object] | None = None) -> dict[str, object]:
    """Canonical ``__fee_policy`` genesis value for the given settings."""
    return TxFeePolicy.from_settings(settings).to_state()


def reject_node_local_fee_settings(xian_config: Mapping[str, object] | None) -> None:
    configured = sorted(set(xian_config or {}) & set(FEE_POLICY_FIELDS))
    if configured:
        raise ValueError(
            f"xian.toml must not set {', '.join(configured)}: the fee policy is chain "
            "state chosen in genesis (__fee_policy); remove these keys"
        )


@dataclass(frozen=True, slots=True)
class TxFeePolicy:
    mode: str
    meter_execution: bool
    charge_fees: bool
    distribute_fee_rewards: bool
    require_chi_balance: bool
    max_tx_chi: int | None = None
    max_block_chi: int | None = None

    @classmethod
    def from_settings(cls, settings: Mapping[str, object] | None) -> "TxFeePolicy":
        """Build a policy from genesis settings; free limits are ignored when paid."""
        payload = dict(settings or {})
        unknown = sorted(set(payload) - set(FEE_POLICY_FIELDS))
        if unknown:
            raise ValueError(f"unknown fee policy settings: {', '.join(unknown)}")
        mode = normalize_tx_fee_mode(payload.get("tx_fee_mode"))
        max_tx_chi = _positive_int(
            payload.get("free_tx_max_chi", DEFAULT_FREE_TX_MAX_CHI),
            name="free_tx_max_chi",
        )
        max_block_chi = _positive_int(
            payload.get("free_block_max_chi", DEFAULT_FREE_BLOCK_MAX_CHI),
            name="free_block_max_chi",
        )
        if max_block_chi < max_tx_chi:
            raise ValueError("free_block_max_chi must be greater than or equal to free_tx_max_chi")
        if mode == PAID_METERED:
            return cls.paid_metered()
        return cls.free_metered(max_tx_chi=max_tx_chi, max_block_chi=max_block_chi)

    @classmethod
    def from_state(cls, value: object) -> "TxFeePolicy":
        """Read the consensus ``__fee_policy`` value; it must be canonical."""
        if not isinstance(value, Mapping):
            raise ValueError("chain state has no valid fee policy (__fee_policy)")
        policy = cls.from_settings(value)
        if policy.to_state() != dict(value):
            raise ValueError("chain fee policy (__fee_policy) is not canonical")
        return policy

    def to_state(self) -> dict[str, object]:
        if self.mode == PAID_METERED:
            return {"tx_fee_mode": PAID_METERED}
        if self.mode == FREE_METERED:
            return {
                "tx_fee_mode": FREE_METERED,
                "free_tx_max_chi": self.max_tx_chi,
                "free_block_max_chi": self.max_block_chi,
            }
        raise ValueError(f"{self.mode} is not a chain fee policy")

    @classmethod
    def paid_metered(
        cls,
        *,
        max_tx_chi: int | None = None,
        max_block_chi: int | None = None,
    ) -> "TxFeePolicy":
        return cls(
            mode=PAID_METERED,
            meter_execution=True,
            charge_fees=True,
            distribute_fee_rewards=True,
            require_chi_balance=True,
            max_tx_chi=max_tx_chi,
            max_block_chi=max_block_chi,
        )

    @classmethod
    def free_metered(
        cls,
        *,
        max_tx_chi: int = DEFAULT_FREE_TX_MAX_CHI,
        max_block_chi: int = DEFAULT_FREE_BLOCK_MAX_CHI,
    ) -> "TxFeePolicy":
        return cls(
            mode=FREE_METERED,
            meter_execution=True,
            charge_fees=False,
            distribute_fee_rewards=False,
            require_chi_balance=False,
            max_tx_chi=max_tx_chi,
            max_block_chi=max_block_chi,
        )

    @classmethod
    def system_unmetered(cls) -> "TxFeePolicy":
        return cls(
            mode=SYSTEM_UNMETERED,
            meter_execution=False,
            charge_fees=False,
            distribute_fee_rewards=False,
            require_chi_balance=False,
        )

    def tx_chi_supplied(self, tx: dict) -> int:
        payload = tx.get("payload") if isinstance(tx, dict) else None
        if not isinstance(payload, dict):
            raise TransactionException("Transaction payload is missing")
        try:
            chi_supplied = int(payload.get("chi_supplied") or 0)
        except (TypeError, ValueError) as exc:
            raise TransactionException("Transaction chi_supplied is invalid") from exc
        if chi_supplied < 0:
            raise TransactionException("Transaction chi_supplied is invalid")
        return chi_supplied

    def validate_tx(self, tx: dict) -> None:
        chi_supplied = self.tx_chi_supplied(tx)
        # Metered user transactions must supply an explicit positive budget;
        # internal system transactions use a separate unmetered policy.
        if self.meter_execution and chi_supplied <= 0:
            raise TransactionException("Transaction chi_supplied must be greater than zero")
        if self.max_tx_chi is None:
            return
        if chi_supplied > self.max_tx_chi:
            raise TransactionException(
                f"Transaction chi_supplied exceeds maximum configured limit of {self.max_tx_chi}"
            )

    def validate_block_total(self, total_chi_supplied: int) -> None:
        if self.max_block_chi is None:
            return
        if total_chi_supplied > self.max_block_chi:
            raise TransactionException(
                f"Block chi_supplied exceeds maximum configured limit of {self.max_block_chi}"
            )


def resolve_chain_fee_policy(*, committed_value: object, genesis: Mapping | None) -> TxFeePolicy:
    """Fee policy from committed state, or from genesis before InitChain ran."""
    if committed_value is not None:
        return TxFeePolicy.from_state(committed_value)
    abci_genesis = (genesis or {}).get("abci_genesis", genesis) or {}
    entries = abci_genesis.get("genesis", []) if isinstance(abci_genesis, Mapping) else []
    values = [
        entry.get("value")
        for entry in entries
        if isinstance(entry, Mapping) and entry.get("key") == FEE_POLICY_KEY
    ]
    if len(values) != 1:
        raise ValueError("genesis must contain exactly one fee policy (__fee_policy)")
    return TxFeePolicy.from_state(values[0])
