from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from time import time
from typing import Any

from loguru import logger
from prometheus_client import CollectorRegistry, start_http_server
from prometheus_client.core import GaugeMetricFamily, InfoMetricFamily

from xian.utils.block import get_latest_block_height


@dataclass(frozen=True)
class MetricsConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 9108
    bds_refresh_seconds: float = 5.0

    @classmethod
    def from_runtime_settings(
        cls, xian_config: dict[str, Any] | None
    ) -> MetricsConfig:
        settings = xian_config or {}
        return cls(
            enabled=bool(settings.get("metrics_enabled", True)),
            host=str(settings.get("metrics_host", "127.0.0.1")),
            port=int(settings.get("metrics_port", 9108)),
            bds_refresh_seconds=float(
                settings.get("metrics_bds_refresh_seconds", 5.0)
            ),
        )


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _add_metric_if_present(
    family: GaugeMetricFamily, labels: list[str], value: Any
) -> None:
    coerced = _coerce_float(value)
    if coerced is not None:
        family.add_metric(labels, coerced)


class XianMetricsCollector:
    def __init__(self, service: MetricsService):
        self.service = service

    def collect(self):
        app = self.service.app
        perf_snapshot = app.profiler.snapshot()
        recent_blocks = perf_snapshot.get("recent_blocks", [])
        latest_block = recent_blocks[-1] if recent_blocks else None
        bds_status = self.service.last_bds_status

        node_info = InfoMetricFamily(
            "xian_node",
            "Static Xian node runtime information.",
        )
        node_info.add_metric(
            [],
            {
                "chain_id": str(app.chain_id),
                "tracer_mode": str(app.tracer_mode),
                "execution_mode": str(getattr(app, "execution_mode", "")),
                "execution_authority": str(
                    getattr(app.execution_runtime, "authority", "python")
                ),
                "execution_shadow": str(
                    getattr(app.execution_runtime, "shadow_execution", False)
                ).lower(),
                "execution_bytecode_version": str(
                    getattr(app.execution_runtime, "bytecode_version", "")
                ),
                "execution_gas_schedule": str(
                    getattr(app.execution_runtime, "gas_schedule", "")
                ),
                "block_service_mode": str(app.block_service_mode).lower(),
                "parallel_execution_enabled": str(
                    app.parallel_block_executor.enabled
                ).lower(),
                "tx_fees_enabled": str(app.enable_tx_fee).lower(),
            },
        )
        yield node_info

        height_family = GaugeMetricFamily(
            "xian_current_block_height",
            "Current finalized block height seen by the Xian app.",
        )
        current_height = None
        if isinstance(app.current_block_meta, dict):
            current_height = app.current_block_meta.get("height")
        _add_metric_if_present(height_family, [], current_height)
        yield height_family

        perf_family = GaugeMetricFamily(
            "xian_perf_global_metric",
            "Global Xian performance metrics.",
            labels=["metric", "stat"],
        )
        for metric_name, values in perf_snapshot.get(
            "global_metrics", {}
        ).items():
            for stat_name in (
                "count",
                "total_ms",
                "avg_ms",
                "min_ms",
                "max_ms",
                "p95_ms",
                "recent_sample_count",
            ):
                _add_metric_if_present(
                    perf_family,
                    [metric_name, stat_name],
                    values.get(stat_name),
                )
        yield perf_family

        latest_block_family = GaugeMetricFamily(
            "xian_perf_latest_block",
            "Most recent completed block performance snapshot.",
            labels=["field"],
        )
        if latest_block is not None:
            _add_metric_if_present(
                latest_block_family,
                ["height"],
                latest_block.get("height"),
            )
            _add_metric_if_present(
                latest_block_family,
                ["tx_count"],
                latest_block.get("tx_count"),
            )
            _add_metric_if_present(
                latest_block_family,
                ["duration_ms"],
                latest_block.get("duration_ms"),
            )
        yield latest_block_family

        latest_block_metrics_family = GaugeMetricFamily(
            "xian_perf_latest_block_metric",
            "Named performance metrics for the most recent completed block.",
            labels=["metric", "stat"],
        )
        if latest_block is not None:
            for metric_name, values in latest_block.get("metrics", {}).items():
                for stat_name in (
                    "count",
                    "total_ms",
                    "avg_ms",
                    "min_ms",
                    "max_ms",
                    "p95_ms",
                    "recent_sample_count",
                ):
                    _add_metric_if_present(
                        latest_block_metrics_family,
                        [metric_name, stat_name],
                        values.get(stat_name),
                    )
        yield latest_block_metrics_family

        latest_block_metadata_family = GaugeMetricFamily(
            "xian_perf_latest_block_metadata",
            "Recent block execution metadata exported as gauges.",
            labels=["field"],
        )
        if latest_block is not None:
            metadata = latest_block.get("metadata", {})
            for field in (
                "parallel_enabled",
                "parallel_worker_count",
                "parallel_planned_stage_count",
                "parallel_planned_parallelizable_transactions",
                "parallel_speculative_wave_count",
                "parallel_speculative_accepted",
                "parallel_serial_prefiltered",
                "parallel_serial_fallbacks",
                "state_patch_applied",
            ):
                _add_metric_if_present(
                    latest_block_metadata_family,
                    [field],
                    metadata.get(field),
                )
        yield latest_block_metadata_family

        exporter_health = GaugeMetricFamily(
            "xian_metrics_exporter",
            "Health and freshness of the Xian Prometheus exporter.",
            labels=["field"],
        )
        _add_metric_if_present(
            exporter_health, ["enabled"], self.service.config.enabled
        )
        _add_metric_if_present(
            exporter_health,
            ["bds_refresh_success"],
            self.service.last_bds_refresh_success,
        )
        _add_metric_if_present(
            exporter_health,
            ["bds_refresh_age_seconds"],
            self.service.last_bds_refresh_age_seconds,
        )
        _add_metric_if_present(
            exporter_health,
            ["perf_snapshot_age_seconds"],
            (
                time() - perf_snapshot["updated_at_unix_ns"] / 1_000_000_000
                if perf_snapshot.get("updated_at_unix_ns")
                else None
            ),
        )
        yield exporter_health

        vm_shadow = getattr(app, "vm_shadow_observer", None)
        vm_shadow_snapshot = (
            vm_shadow.snapshot() if vm_shadow is not None else None
        )

        vm_shadow_family = GaugeMetricFamily(
            "xian_vm_shadow_metric",
            "Xian VM shadow/native comparison metrics.",
            labels=["field"],
        )
        if vm_shadow_snapshot is not None:
            _add_metric_if_present(
                vm_shadow_family,
                ["enabled"],
                vm_shadow_snapshot.get("enabled"),
            )
            _add_metric_if_present(
                vm_shadow_family,
                ["shadow_execution"],
                vm_shadow_snapshot.get("shadow_execution"),
            )
            _add_metric_if_present(
                vm_shadow_family,
                ["comparisons_total"],
                vm_shadow_snapshot.get("comparisons_total"),
            )
            _add_metric_if_present(
                vm_shadow_family,
                ["mismatches_total"],
                vm_shadow_snapshot.get("mismatches_total"),
            )
            _add_metric_if_present(
                vm_shadow_family,
                ["recent_mismatch_count"],
                len(vm_shadow_snapshot.get("recent_mismatches", [])),
            )
            last_comparison_at = vm_shadow_snapshot.get(
                "last_comparison_at_unix"
            )
            last_mismatch_at = vm_shadow_snapshot.get("last_mismatch_at_unix")
            _add_metric_if_present(
                vm_shadow_family,
                ["last_comparison_age_seconds"],
                time() - last_comparison_at if last_comparison_at else None,
            )
            _add_metric_if_present(
                vm_shadow_family,
                ["last_mismatch_age_seconds"],
                time() - last_mismatch_at if last_mismatch_at else None,
            )
        yield vm_shadow_family

        vm_shadow_stage_family = GaugeMetricFamily(
            "xian_vm_shadow_stage_metric",
            "Per-stage Xian VM shadow/native comparison counters.",
            labels=["stage", "field"],
        )
        if vm_shadow_snapshot is not None:
            for stage_name, values in vm_shadow_snapshot.get(
                "stages", {}
            ).items():
                _add_metric_if_present(
                    vm_shadow_stage_family,
                    [stage_name, "comparisons_total"],
                    values.get("comparisons_total"),
                )
                _add_metric_if_present(
                    vm_shadow_stage_family,
                    [stage_name, "mismatches_total"],
                    values.get("mismatches_total"),
                )
        yield vm_shadow_stage_family

        vm_shadow_last_mismatch = InfoMetricFamily(
            "xian_vm_shadow_last_mismatch",
            "Most recent Xian VM shadow/native mismatch context.",
        )
        latest_mismatch = (
            vm_shadow_snapshot.get("latest_mismatch")
            if vm_shadow_snapshot is not None
            else None
        )
        if latest_mismatch:
            vm_shadow_last_mismatch.add_metric(
                [],
                {
                    "stage": str(latest_mismatch.get("stage", "")),
                    "contract": str(latest_mismatch.get("contract", "")),
                    "function": str(latest_mismatch.get("function", "")),
                    "sender": str(latest_mismatch.get("sender", "")),
                    "nonce": str(latest_mismatch.get("nonce", "")),
                    "tx_hash": str(latest_mismatch.get("tx_hash", "")),
                    "block_height": str(
                        latest_mismatch.get("block_height", "")
                    ),
                    "mismatch_fields": ",".join(
                        latest_mismatch.get("mismatch_fields", [])
                    ),
                },
            )
        else:
            vm_shadow_last_mismatch.add_metric(
                [],
                {
                    "stage": "",
                    "contract": "",
                    "function": "",
                    "sender": "",
                    "nonce": "",
                    "tx_hash": "",
                    "block_height": "",
                    "mismatch_fields": "",
                },
            )
        yield vm_shadow_last_mismatch

        bds_info = InfoMetricFamily(
            "xian_bds",
            "Optional BDS runtime information.",
        )
        if app.block_service_mode:
            bds_info.add_metric(
                [],
                {
                    "enabled": "true",
                    "db_status": str(
                        (bds_status or {}).get("db_status", "unknown")
                    ),
                },
            )
        else:
            bds_info.add_metric(
                [], {"enabled": "false", "db_status": "disabled"}
            )
        yield bds_info

        bds_family = GaugeMetricFamily(
            "xian_bds_metric",
            "Optional BDS worker and storage health metrics.",
            labels=["field"],
        )
        _add_metric_if_present(
            bds_family, ["enabled"], 1 if app.block_service_mode else 0
        )
        if app.block_service_mode:
            _add_metric_if_present(
                bds_family,
                ["refresh_success"],
                1 if self.service.last_bds_refresh_success else 0,
            )
            _add_metric_if_present(
                bds_family,
                ["refresh_age_seconds"],
                self.service.last_bds_refresh_age_seconds,
            )
        if bds_status:
            indexed = bds_status.get("indexed", {})
            for field, value in (
                ("catchup_running", bds_status.get("catchup_running")),
                ("worker_running", bds_status.get("worker_running")),
                ("queue_depth", bds_status.get("queue_depth")),
                ("queue_capacity", bds_status.get("queue_capacity")),
                ("queue_utilization", bds_status.get("queue_utilization")),
                ("spool_pending_count", bds_status.get("spool_pending_count")),
                ("spool_total_bytes", bds_status.get("spool_total_bytes")),
                ("storage_total_bytes", bds_status.get("storage_total_bytes")),
                ("storage_used_bytes", bds_status.get("storage_used_bytes")),
                ("storage_free_bytes", bds_status.get("storage_free_bytes")),
                (
                    "current_block_height",
                    bds_status.get("current_block_height"),
                ),
                ("height_lag", bds_status.get("height_lag")),
                ("catching_up", bds_status.get("catching_up")),
                ("db_ok", bds_status.get("db_status") == "ok"),
                ("alert_count", len(bds_status.get("alerts", []))),
                (
                    "last_enqueue_error_present",
                    bds_status.get("last_enqueue_error") is not None,
                ),
                ("indexed_block_count", indexed.get("indexed_block_count")),
                ("indexed_height", indexed.get("indexed_height")),
                ("indexed_tx_count", indexed.get("indexed_tx_count")),
            ):
                _add_metric_if_present(bds_family, [field], value)
        yield bds_family

        bds_alerts = GaugeMetricFamily(
            "xian_bds_alert",
            "BDS alerts reported by the node.",
            labels=["severity", "kind"],
        )
        if bds_status:
            for alert in bds_status.get("alerts", []):
                severity = str(
                    alert.get("severity") or alert.get("level") or "unknown"
                )
                kind = str(alert.get("kind") or alert.get("code") or "unknown")
                bds_alerts.add_metric([severity, kind], 1.0)
        yield bds_alerts


class MetricsService:
    def __init__(self, app, config: MetricsConfig):
        self.app = app
        self.config = config
        self.registry = CollectorRegistry(auto_describe=False)
        self.registry.register(XianMetricsCollector(self))
        self._http_server = None
        self._http_thread: threading.Thread | None = None
        self._bds_task: asyncio.Task | None = None
        self.last_bds_status: dict[str, Any] | None = None
        self.last_bds_refresh_success: bool = False
        self.last_bds_refresh_at_unix: float | None = None

    @property
    def last_bds_refresh_age_seconds(self) -> float | None:
        if self.last_bds_refresh_at_unix is None:
            return None
        return max(0.0, time() - self.last_bds_refresh_at_unix)

    @classmethod
    def from_runtime_settings(
        cls, app, xian_config: dict[str, Any] | None
    ) -> MetricsService:
        return cls(
            app=app, config=MetricsConfig.from_runtime_settings(xian_config)
        )

    async def start(self) -> None:
        if not self.config.enabled or self._http_server is not None:
            return

        logger.info(
            "Starting Xian metrics exporter on http://{}:{}",
            self.config.host,
            self.config.port,
        )
        started = start_http_server(
            port=self.config.port,
            addr=self.config.host,
            registry=self.registry,
        )
        if isinstance(started, tuple):
            self._http_server, self._http_thread = started
        else:
            self._http_server = started

        if self.app.block_service_mode and hasattr(self.app, "bds"):
            self._bds_task = asyncio.create_task(
                self._refresh_bds_status_loop(),
                name="xian-metrics-bds-refresh",
            )

    async def close(self) -> None:
        if self._bds_task is not None:
            self._bds_task.cancel()
            await asyncio.gather(self._bds_task, return_exceptions=True)
        self._bds_task = None

        if self._http_server is not None:
            shutdown = getattr(self._http_server, "shutdown", None)
            server_close = getattr(self._http_server, "server_close", None)
            if callable(shutdown):
                shutdown()
            if callable(server_close):
                server_close()
        self._http_server = None
        self._http_thread = None

    async def _refresh_bds_status_loop(self) -> None:
        await self._refresh_bds_status_once()
        while True:
            await asyncio.sleep(max(self.config.bds_refresh_seconds, 1.0))
            await self._refresh_bds_status_once()

    async def _refresh_bds_status_once(self) -> None:
        try:
            current_height = None
            if isinstance(self.app.current_block_meta, dict):
                current_height = self.app.current_block_meta.get("height")
            if current_height is None:
                current_height = get_latest_block_height()
            self.last_bds_status = await self.app.bds.get_status(
                current_block_height=current_height
            )
            self.last_bds_refresh_success = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Failed to refresh BDS metrics snapshot: {}", exc)
            self.last_bds_refresh_success = False
        finally:
            self.last_bds_refresh_at_unix = time()
