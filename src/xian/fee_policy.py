from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache

from xian_vm_core import runtime_info

from xian.exceptions import TransactionException
from xian.execution_policy import VM_EXECUTION_MODE

PAID_METERED = "paid_metered"
FREE_METERED = "free_metered"
SYSTEM_UNMETERED = "system_unmetered"

DEFAULT_TX_FEE_MODE = PAID_METERED
# Hard chi caps that hold in every chain fee mode. Chi is the only
# deterministic bound on execution time, so these caps bound how long one
# transaction, and one block, can keep every later transaction waiting.
DEFAULT_MAX_TX_CHI = 200_000
DEFAULT_MAX_BLOCK_CHI = 500_000
# Upper bound for a genesis-chosen block cap: 20x the default, which is tens of
# seconds of execution per block even at fair prices. Larger values are a
# misconfiguration, not a policy.
MAX_BLOCK_CHI_LIMIT = 10_000_000

SUPPORTED_TX_FEE_MODES = frozenset({PAID_METERED, FREE_METERED})

# Consensus state key holding the chain's fee policy. Genesis writes it; every
# validator reads it from state, so nodes cannot disagree through local config.
FEE_POLICY_KEY = "__fee_policy"
FEE_POLICY_FIELDS = ("tx_fee_mode", "max_tx_chi", "max_block_chi")


def normalize_tx_fee_mode(mode: object | None) -> str:
    normalized = str(mode or DEFAULT_TX_FEE_MODE).strip().lower()
    if normalized not in SUPPORTED_TX_FEE_MODES:
        raise ValueError(f"tx_fee_mode must be one of {sorted(SUPPORTED_TX_FEE_MODES)}")
    return normalized


def _positive_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be an integer greater than zero")
    return value


@cache
def vm_max_tx_chi() -> int:
    """Per-transaction chi ceiling the installed VM enforces for the node's profile."""
    ceiling = runtime_info(VM_EXECUTION_MODE).get("max_tx_chi")
    if type(ceiling) is not int or ceiling <= 0:
        raise ValueError("the installed VM does not report its per-transaction chi ceiling")
    return ceiling


def _validate_chi_caps(max_tx_chi: object, max_block_chi: object) -> None:
    _positive_int(max_tx_chi, name="max_tx_chi")
    _positive_int(max_block_chi, name="max_block_chi")
    if max_block_chi < max_tx_chi:
        raise ValueError("max_block_chi must be greater than or equal to max_tx_chi")
    if max_block_chi > MAX_BLOCK_CHI_LIMIT:
        raise ValueError(f"max_block_chi must not exceed {MAX_BLOCK_CHI_LIMIT} chi")
    ceiling = vm_max_tx_chi()
    if max_tx_chi > ceiling:
        raise ValueError(f"max_tx_chi {max_tx_chi} exceeds the VM ceiling of {ceiling} chi")


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
    # Set exactly for metered (chain) policies; system work is unmetered.
    max_tx_chi: int | None = None
    max_block_chi: int | None = None

    def __post_init__(self) -> None:
        if self.meter_execution:
            _validate_chi_caps(self.max_tx_chi, self.max_block_chi)

    @classmethod
    def from_settings(cls, settings: Mapping[str, object] | None) -> "TxFeePolicy":
        """Build a chain policy from genesis settings, filling default caps."""
        payload = dict(settings or {})
        unknown = sorted(set(payload) - set(FEE_POLICY_FIELDS))
        if unknown:
            raise ValueError(f"unknown fee policy settings: {', '.join(unknown)}")
        return cls.metered(
            normalize_tx_fee_mode(payload.get("tx_fee_mode")),
            max_tx_chi=payload.get("max_tx_chi", DEFAULT_MAX_TX_CHI),
            max_block_chi=payload.get("max_block_chi", DEFAULT_MAX_BLOCK_CHI),
        )

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
        if self.mode not in SUPPORTED_TX_FEE_MODES:
            raise ValueError(f"{self.mode} is not a chain fee policy")
        return {
            "tx_fee_mode": self.mode,
            "max_tx_chi": self.max_tx_chi,
            "max_block_chi": self.max_block_chi,
        }

    @classmethod
    def metered(
        cls,
        mode: str,
        *,
        max_tx_chi: int = DEFAULT_MAX_TX_CHI,
        max_block_chi: int = DEFAULT_MAX_BLOCK_CHI,
    ) -> "TxFeePolicy":
        """A chain policy; paid mode charges and rewards fees, free mode does not."""
        mode = normalize_tx_fee_mode(mode)
        paid = mode == PAID_METERED
        return cls(
            mode=mode,
            meter_execution=True,
            charge_fees=paid,
            distribute_fee_rewards=paid,
            require_chi_balance=paid,
            max_tx_chi=max_tx_chi,
            max_block_chi=max_block_chi,
        )

    @classmethod
    def paid_metered(cls, **caps: int) -> "TxFeePolicy":
        return cls.metered(PAID_METERED, **caps)

    @classmethod
    def free_metered(cls, **caps: int) -> "TxFeePolicy":
        return cls.metered(FREE_METERED, **caps)

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
        if not self.meter_execution:
            return
        if chi_supplied <= 0:
            raise TransactionException("Transaction chi_supplied must be greater than zero")
        if chi_supplied > self.max_tx_chi:
            raise TransactionException(
                f"Transaction chi_supplied {chi_supplied} exceeds the chain's "
                f"per-transaction limit of {self.max_tx_chi} chi"
            )

    def validate_block_total(self, total_chi_supplied: int) -> None:
        if self.meter_execution and total_chi_supplied > self.max_block_chi:
            raise TransactionException(
                f"Block chi_supplied {total_chi_supplied} would exceed the chain's "
                f"per-block limit of {self.max_block_chi} chi"
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
