"""Initialize and advance the canonical immutable successor-time registry."""

from xian.methods._time_batch import process_time_batch

PREFIX = "time_checkpoints."


def _checkpoint_service_state(driver):
    version = driver.get(PREFIX + "protocol_version")
    ready = driver.get(PREFIX + "keeper_ready")
    head = driver.get(PREFIX + "queue_head")
    if all(value is None for value in (version, ready, head)):
        # Universal runtimes may run a genesis without this optional service.
        # Consumers themselves refuse custody until the service is ready.
        return None
    if (type(version), version, type(ready)) != (int, 1, bool):
        raise RuntimeError("invalid time-checkpoint protocol/readiness marker")
    if type(head) is not int or head < 0:
        raise RuntimeError("invalid time-checkpoint queue head")
    return ready, head


def _needs_checkpoint_batch(driver, height):
    state = _checkpoint_service_state(driver)
    if state is None:
        return False
    ready, head = state
    if head == 0:
        return not ready
    requested_height = driver.get(PREFIX + "job_heights:" + str(head))
    if type(requested_height) is not int or requested_height < 1:
        raise RuntimeError("invalid time-checkpoint requested height")
    return not ready or requested_height < height


def maybe_process_time_checkpoints(self, *, height):
    """One bounded trusted call before paid custody; no callbacks or scans."""
    if _needs_checkpoint_batch(self.client.raw_driver, height):
        process_time_batch(
            self,
            height=height,
            contract="time_checkpoints",
            sender="__time_checkpoints__",
            signature="time-checkpoint-batch",
            stage="finalize_time_checkpoints",
            message="Processed immutable time-checkpoint batch",
        )
