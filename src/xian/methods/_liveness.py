"""Validator liveness for ``finalize_block``.

The decided last commit (part of the agreed block, so every node sees the
same votes) says which validators signed the previous block. When one of
the active validators is absent, or a validator's consecutive-missed count
is open, an unmetered system transaction ``validators.record_liveness``
counts the misses and jails a validator absent for the policy's downtime
window (``downtime_jail_blocks``); CometBFT then drops its power at the next
block. A signature (or a nil vote) resets the count.
"""

from loguru import logger

from cometbft.types.v1beta1.validator_pb2 import BlockIDFlag
from xian.app_logging import build_log_fields
from xian.methods._evidence import active_validator_keys_by_address

SYSTEM_LIVENESS_SENDER = "__liveness_driver__"


def absent_validator_keys(self, req) -> list[str]:
    """The known validators whose signature is absent from the decided last
    commit, in vote order."""
    votes = list(getattr(req.decided_last_commit, "votes", []) or [])
    absent = [
        bytes(vote.validator.address)
        for vote in votes
        if vote.block_id_flag == BlockIDFlag.BLOCK_ID_FLAG_ABSENT
    ]
    if not absent:
        return []
    keys = active_validator_keys_by_address(self)
    return [keys[address] for address in absent if address in keys]


def _has_open_count(self) -> bool:
    driver = self.client.raw_driver
    active = driver.get("validators.active_validators") or []
    return any(driver.get(f"validators.missed_blocks:{key}") for key in active)


def maybe_record_liveness(self, req, *, height: int) -> bool:
    """Run ``validators.record_liveness`` when someone is absent or a count
    is open; True if it wrote state."""
    absent = absent_validator_keys(self, req)
    if not absent and not _has_open_count(self):
        return False
    tx = {
        "payload": {
            "sender": SYSTEM_LIVENESS_SENDER,
            "contract": "validators",
            "function": "record_liveness",
            "kwargs": {"absent": absent},
            "chi_supplied": 0,
        },
        "metadata": {"signature": f"validator-liveness:{height}"},
        "b_meta": self.current_block_meta,
    }
    tx_result = self.tx_processor.process_system_tx(tx).get("tx_result") or {}
    applied = tx_result.get("status") == 0
    logger.bind(
        **build_log_fields(
            stage="finalize_liveness",
            block_height=height,
            block_hash=self.current_block_meta["hash"],
            status=tx_result.get("status"),
            extra={"absent": absent, "result": str(tx_result.get("result"))[:200]},
        )
    ).log("INFO" if applied else "WARNING", "Recorded validator liveness")
    return applied and len(tx_result.get("state", [])) > 0
