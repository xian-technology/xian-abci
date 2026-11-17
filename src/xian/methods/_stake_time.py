"""Anchor one bounded batch of staking clocks in a successor block."""

from collections.abc import Mapping

from xian.methods._time_batch import process_time_batch

TIME_ANCHOR_HEAD = "validator_penalties.time_anchor_head"
TIME_ANCHOR_JOBS = "validator_penalties.time_anchor_jobs:"
SYSTEM_STAKE_TIME_SENDER = "__stake_time_driver__"


def _has_older_time_job(driver, height: int) -> bool:
    head = driver.get(TIME_ANCHOR_HEAD)
    if head is None:
        return False
    if type(head) is not int or head < 1:
        raise RuntimeError("invalid staking time-anchor queue head")
    job = driver.get(TIME_ANCHOR_JOBS + str(head))
    if not isinstance(job, Mapping) or type(job.get("height")) is not int or job["height"] < 0:
        raise RuntimeError("invalid staking time-anchor queue job")
    return job["height"] < height


def maybe_process_time_anchors(self, *, height: int) -> None:
    """Read only the head; the contract enforces one shared 32-unit budget.

    Same-height jobs cannot start their own clock. Evidence/liveness has already
    run, but pending penalty jobs must not block timing-only anchor updates.
    """
    if not _has_older_time_job(self.client.raw_driver, height):
        return
    process_time_batch(
        self,
        height=height,
        contract="validator_penalties",
        sender=SYSTEM_STAKE_TIME_SENDER,
        signature="stake-time-anchor-batch",
        stage="finalize_stake_time_anchor",
        message="Processed staking time-anchor batch",
    )
