from __future__ import annotations

import unittest
from types import SimpleNamespace

from xian.metrics import XianMetricsCollector


class BdsMetricsCollectorTests(unittest.TestCase):
    def test_metrics_collector_exports_bds_recovery_fields(self) -> None:
        app = SimpleNamespace(
            chain_id="xian-local",
            tracer_mode="python_line_v1",
            execution_mode="python_line_v1",
            execution_runtime=SimpleNamespace(
                authority="python",
                shadow_execution=False,
                bytecode_version="",
                gas_schedule="",
            ),
            block_service_mode=True,
            parallel_block_executor=SimpleNamespace(enabled=False),
            enable_tx_fee=True,
            current_block_meta={"height": 128},
            profiler=SimpleNamespace(
                snapshot=lambda: {
                    "recent_blocks": [],
                    "global_metrics": {},
                    "updated_at_unix_ns": 0,
                }
            ),
            vm_shadow_observer=None,
        )
        service = SimpleNamespace(
            app=app,
            config=SimpleNamespace(enabled=True),
            last_bds_status={
                "worker_running": True,
                "catchup_running": True,
                "queue_depth": 4,
                "queue_capacity": 16,
                "queue_utilization": 0.25,
                "spool_pending_count": 9,
                "spool_total_bytes": 4096,
                "storage": {
                    "filesystem_total_bytes": 1024,
                    "filesystem_used_bytes": 512,
                    "filesystem_free_bytes": 512,
                },
                "current_block_height": 128,
                "height_lag": 3,
                "catching_up": True,
                "db_status": "ok",
                "last_enqueue_error": {
                    "code": "pending_buffer_full",
                    "message": "queue full",
                },
                "alerts": [
                    {
                        "level": "warning",
                        "code": "spool_entries_high",
                        "message": "spool high",
                    }
                ],
                "indexed": {
                    "indexed_block_count": 127,
                    "indexed_height": 125,
                    "indexed_tx_count": 512,
                },
                "pool": {
                    "size": 8,
                    "idle": 3,
                    "in_use": 5,
                    "max_size": 10,
                    "min_size": 2,
                    "utilization": 0.5,
                },
            },
            last_bds_refresh_success=True,
            last_bds_refresh_age_seconds=4.5,
        )

        families = list(XianMetricsCollector(service).collect())
        samples = [
            sample
            for family in families
            for sample in getattr(family, "samples", [])
        ]
        sample_by_name: dict[str, list] = {}
        for sample in samples:
            sample_by_name.setdefault(sample.name, []).append(sample)

        bds_info = sample_by_name["xian_bds_info"][0]
        self.assertEqual(bds_info.labels["enabled"], "true")
        self.assertEqual(bds_info.labels["db_status"], "ok")

        bds_metrics = {
            sample.labels["field"]: sample.value
            for sample in sample_by_name["xian_bds_metric"]
        }
        self.assertEqual(bds_metrics["enabled"], 1.0)
        self.assertEqual(bds_metrics["refresh_success"], 1.0)
        self.assertEqual(bds_metrics["refresh_age_seconds"], 4.5)
        self.assertEqual(bds_metrics["catchup_running"], 1.0)
        self.assertEqual(bds_metrics["catching_up"], 1.0)
        self.assertEqual(bds_metrics["db_ok"], 1.0)
        self.assertEqual(bds_metrics["alert_count"], 1.0)
        self.assertEqual(bds_metrics["last_enqueue_error_present"], 1.0)
        self.assertEqual(bds_metrics["pool_size"], 8.0)
        self.assertEqual(bds_metrics["pool_idle"], 3.0)
        self.assertEqual(bds_metrics["pool_in_use"], 5.0)
        self.assertEqual(bds_metrics["pool_max_size"], 10.0)
        self.assertEqual(bds_metrics["pool_min_size"], 2.0)
        self.assertEqual(bds_metrics["pool_utilization"], 0.5)

        bds_alert = sample_by_name["xian_bds_alert"][0]
        self.assertEqual(bds_alert.labels["severity"], "warning")
        self.assertEqual(bds_alert.labels["kind"], "spool_entries_high")


if __name__ == "__main__":
    unittest.main()
