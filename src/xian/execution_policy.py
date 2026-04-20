from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from contracting.execution.tracer import SUPPORTED_TRACER_MODES

DEFAULT_EXECUTION_MODE = "python_line_v1"
FUTURE_EXECUTION_ENGINE_MODES = frozenset({"xian_vm_v1"})
SUPPORTED_EXECUTION_ENGINE_MODES = frozenset(SUPPORTED_TRACER_MODES) | (
    FUTURE_EXECUTION_ENGINE_MODES
)


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    mode: str
    bytecode_version: str = ""
    gas_schedule: str = ""
    authority: str = ""

    @property
    def is_current_tracer_mode(self) -> bool:
        return self.mode in SUPPORTED_TRACER_MODES

    def to_config_dict(self) -> dict[str, Any]:
        return {
            "engine": {
                "mode": self.mode,
                "bytecode_version": self.bytecode_version,
                "gas_schedule": self.gas_schedule,
                "authority": self.authority,
            }
        }


def resolve_execution_policy(
    *,
    mode: str | None = None,
    tracer_mode: str | None = None,
    bytecode_version: str = "",
    gas_schedule: str = "",
    authority: str = "",
    allow_future: bool = False,
) -> ExecutionPolicy:
    selected = mode or tracer_mode or DEFAULT_EXECUTION_MODE
    if selected in SUPPORTED_TRACER_MODES:
        if bytecode_version or gas_schedule or authority:
            raise ValueError(
                "bytecode_version, gas_schedule, and authority are only "
                "valid for future execution engines"
            )
        return ExecutionPolicy(mode=selected)

    if selected == "xian_vm_v1":
        if not allow_future:
            raise ValueError(
                "xian_vm_v1 is not implemented in the current runtime"
            )
        if not bytecode_version:
            raise ValueError(
                "xian_vm_v1 requires a bytecode_version in execution policy"
            )
        if not gas_schedule:
            raise ValueError(
                "xian_vm_v1 requires a gas_schedule in execution policy"
            )
        normalized_authority = (authority or "native").strip()
        if normalized_authority != "native":
            raise ValueError("xian_vm_v1 authority must be 'native'")
        return ExecutionPolicy(
            mode=selected,
            bytecode_version=bytecode_version,
            gas_schedule=gas_schedule,
            authority=normalized_authority,
        )

    raise ValueError(
        "execution mode must be one of "
        f"{sorted(SUPPORTED_EXECUTION_ENGINE_MODES)}"
    )


def load_execution_policy(
    xian_config: dict[str, Any] | None,
    *,
    allow_future: bool = False,
) -> ExecutionPolicy:
    payload = xian_config or {}
    configured_tracer_mode = (
        str(payload.get("tracer_mode", DEFAULT_EXECUTION_MODE)).strip()
        or DEFAULT_EXECUTION_MODE
    )
    engine_payload = payload.get("execution", {}).get("engine", {})
    configured_mode = str(engine_payload.get("mode", "")).strip()
    removed_shadow_mode = str(
        engine_payload.get("shadow_tracer_mode", "")
    ).strip()
    if removed_shadow_mode:
        raise ValueError(
            "xian.execution.engine.shadow_tracer_mode is no longer supported"
        )
    if (
        configured_mode
        and configured_tracer_mode
        and configured_mode in SUPPORTED_TRACER_MODES
        and configured_mode != configured_tracer_mode
    ):
        raise ValueError(
            "xian.execution.engine.mode must match xian.tracer_mode when "
            "both are present"
        )
    return resolve_execution_policy(
        mode=configured_mode or None,
        tracer_mode=configured_tracer_mode,
        bytecode_version=str(
            engine_payload.get("bytecode_version", "")
        ).strip(),
        gas_schedule=str(engine_payload.get("gas_schedule", "")).strip(),
        authority=str(engine_payload.get("authority", "")).strip(),
        allow_future=allow_future,
    )
