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
    shadow_tracer_mode: str = ""

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
                "shadow_tracer_mode": self.shadow_tracer_mode,
            }
        }


def resolve_execution_policy(
    *,
    mode: str | None = None,
    tracer_mode: str | None = None,
    bytecode_version: str = "",
    gas_schedule: str = "",
    authority: str = "",
    shadow_tracer_mode: str = "",
    allow_future: bool = False,
) -> ExecutionPolicy:
    selected = mode or tracer_mode or DEFAULT_EXECUTION_MODE
    if selected in SUPPORTED_TRACER_MODES:
        if bytecode_version or gas_schedule or authority or shadow_tracer_mode:
            raise ValueError(
                "bytecode_version, gas_schedule, authority, and "
                "shadow_tracer_mode are only valid for "
                "future execution engines"
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
        normalized_authority = (authority or "python").strip()
        if normalized_authority not in {"python", "native"}:
            raise ValueError(
                "xian_vm_v1 authority must be one of ['native', 'python']"
            )
        normalized_shadow_tracer_mode = shadow_tracer_mode.strip()
        if (
            normalized_shadow_tracer_mode
            and normalized_shadow_tracer_mode not in SUPPORTED_TRACER_MODES
        ):
            raise ValueError(
                "xian_vm_v1 shadow_tracer_mode must be one of "
                f"{sorted(SUPPORTED_TRACER_MODES)}"
            )
        if normalized_authority != "native" and not normalized_shadow_tracer_mode:
            raise ValueError(
                "xian_vm_v1 requires shadow_tracer_mode when authority is "
                "'python' so native execution has an explicit comparison mode"
            )
        return ExecutionPolicy(
            mode=selected,
            bytecode_version=bytecode_version,
            gas_schedule=gas_schedule,
            authority=normalized_authority,
            shadow_tracer_mode=normalized_shadow_tracer_mode,
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
    legacy_mode = str(
        payload.get("tracer_mode", DEFAULT_EXECUTION_MODE)
    ).strip() or DEFAULT_EXECUTION_MODE
    engine_payload = payload.get("execution", {}).get("engine", {})
    configured_mode = str(engine_payload.get("mode", "")).strip()
    if (
        configured_mode
        and legacy_mode
        and configured_mode in SUPPORTED_TRACER_MODES
        and configured_mode != legacy_mode
    ):
        raise ValueError(
            "xian.execution.engine.mode must match xian.tracer_mode when "
            "both are present"
        )
    return resolve_execution_policy(
        mode=configured_mode or None,
        tracer_mode=legacy_mode,
        bytecode_version=str(
            engine_payload.get("bytecode_version", "")
        ).strip(),
        gas_schedule=str(engine_payload.get("gas_schedule", "")).strip(),
        authority=str(engine_payload.get("authority", "")).strip(),
        shadow_tracer_mode=str(
            engine_payload.get("shadow_tracer_mode", "")
        ).strip(),
        allow_future=allow_future,
    )
