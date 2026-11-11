"""Independent state machine for the processor fuzz probe's business rules.

This model uses Python integers and dictionaries. It does not import compiler,
VM, storage, processor, serialization, or metering implementations.
"""

CONTRACT = "con_vm_processor_fuzz"


def typed_value(value):
    """Keep bool/int and other scalar shapes distinct during model comparison."""
    if isinstance(value, dict):
        return type(value), {key: typed_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value), tuple(typed_value(item) for item in value)
    return type(value), value


class ProcessorProbeModel:
    def __init__(self):
        self.state = {}

    @property
    def counter(self):
        return self.state.get(f"{CONTRACT}.counter", 0)

    def step(self, function, kwargs):
        try:
            result, writes, event = getattr(self, f"_{function}")(**kwargs)
        except AssertionError as error:
            return {
                "status": 1,
                "result": f"AssertionError({str(error)!r})",
                "state": [],
                "events": [],
            }
        self.state.update(writes)
        return {
            "status": 0,
            "result": result,
            "state": [{"key": key, "value": writes[key]} for key in sorted(writes)],
            "events": [event],
        }

    @staticmethod
    def _event(kind, key, value, counter):
        return {
            "contract": CONTRACT,
            "event": "Probe",
            "signer": "alice",
            "caller": "alice",
            "data_indexed": {"kind": kind, "key": key},
            "data": {"value": value, "counter": counter},
        }

    def _put(self, key, value):
        assert isinstance(key, str) and key != "", "key must be non-empty."
        assert -50 <= value <= 50, "value out of range."
        counter = self.counter + 1
        writes = {f"{CONTRACT}.counter": counter, f"{CONTRACT}.items:{key}": value}
        return {"counter": counter, "value": value}, writes, self._event("put", key, value, counter)

    def _add(self, key, delta):
        value = self.state.get(f"{CONTRACT}.items:{key}", 0) + delta
        assert -100 <= value <= 100, "updated value out of range."
        counter = self.counter + 1
        writes = {f"{CONTRACT}.counter": counter, f"{CONTRACT}.items:{key}": value}
        return value, writes, self._event("add", key, value, counter)

    def _claim(self, slot, amount):
        assert isinstance(slot, str) and slot != "", "slot must be non-empty."
        assert amount >= 0, "amount must be non-negative."
        key = f"{CONTRACT}.claims:{slot}"
        assert not self.state.get(key, False), "slot already claimed."
        counter = self.counter + amount
        writes = {key: True, f"{CONTRACT}.counter": counter}
        return counter, writes, self._event("claim", slot, amount, counter)

    @staticmethod
    def _fail_after_write(key, value):
        # Tentative effects from a rejected operation are absent from committed state.
        raise AssertionError("forced failure")
