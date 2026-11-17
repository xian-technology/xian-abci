"""Send the fixed canonical clock-keeper batches with shared receipt logging."""

from loguru import logger

from xian.app_logging import build_log_fields


def process_time_batch(self, *, height, contract, sender, signature, stage, message):
    # Callers select one of two known canonical modules; no user callbacks run.
    tx = {
        "payload": {
            "sender": sender,
            "contract": contract,
            "function": "process_time_anchors",
            "kwargs": {"max_work": 32},
            "chi_supplied": 0,
        },
        "metadata": {"signature": f"{signature}:{height}"},
        "b_meta": self.current_block_meta,
    }
    result = self.tx_processor.process_system_tx(tx).get("tx_result") or {}
    logger.bind(
        **build_log_fields(
            stage=stage,
            block_height=height,
            block_hash=self.current_block_meta["hash"],
            status=result.get("status"),
        )
    ).log("INFO" if result.get("status") == 0 else "WARNING", message)
