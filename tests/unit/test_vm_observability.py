from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from xian.metrics import XianMetricsCollector
from xian.vm_observability import VmShadowObserver


class VmShadowObserverTests(unittest.TestCase):
    def test_records_counts_recent_mismatches_and_jsonl(self) -> None:
        with tempfile.TemporaryDirectory(prefix="xian-vm-observer-") as tmp:
            observer = VmShadowObserver.for_runtime(
                storage_home=Path(tmp),
                mode="xian_vm_v1",
                authority="python",
                shadow_execution=True,
            )

            observer.record_comparison(
                stage="execute_tx_native_shadow",
                contract="currency",
                function="transfer",
                sender="alice",
                nonce=7,
                tx_hash="abc123",
                block_height=42,
                mismatches={},
            )
            observer.record_comparison(
                stage="execute_tx_native_shadow",
                contract="currency",
                function="transfer",
                sender="alice",
                nonce=8,
                tx_hash="def456",
                block_height=43,
                mismatches={"writes": ({"a": 1}, {"a": 2})},
            )

            snapshot = observer.snapshot()
            self.assertTrue(snapshot["enabled"])
            self.assertEqual(snapshot["comparisons_total"], 2)
            self.assertEqual(snapshot["mismatches_total"], 1)
            self.assertEqual(
                snapshot["stages"]["execute_tx_native_shadow"][
                    "comparisons_total"
                ],
                2,
            )
            self.assertEqual(
                snapshot["stages"]["execute_tx_native_shadow"][
                    "mismatches_total"
                ],
                1,
            )
            self.assertEqual(
                snapshot["latest_mismatch"]["mismatch_fields"], ["writes"]
            )

            log_path = Path(tmp) / "logs" / "xian-vm-shadow-mismatches.jsonl"
            self.assertTrue(log_path.exists())
            rows = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["contract"], "currency")
            self.assertEqual(rows[0]["mismatch_fields"], ["writes"])

    def test_non_vm_runtime_observer_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory(prefix="xian-vm-observer-") as tmp:
            observer = VmShadowObserver.for_runtime(
                storage_home=Path(tmp),
                mode="python_line_v1",
                authority="python",
                shadow_execution=False,
            )
            observer.record_comparison(
                stage="simulate_tx_native_shadow",
                contract="currency",
                function="balance_of",
                mismatches={"result": (1, 2)},
            )
            snapshot = observer.snapshot()
            self.assertFalse(snapshot["enabled"])
            self.assertEqual(snapshot["comparisons_total"], 0)
            self.assertIsNone(snapshot["latest_mismatch"])


class VmShadowMetricsTests(unittest.TestCase):
    def test_metrics_collector_exports_vm_shadow_snapshot(self) -> None:
        with tempfile.TemporaryDirectory(prefix="xian-vm-metrics-") as tmp:
            observer = VmShadowObserver.for_runtime(
                storage_home=Path(tmp),
                mode="xian_vm_v1",
                authority="native",
                shadow_execution=True,
            )
            observer.record_comparison(
                stage="execute_tx_native_authoritative",
                contract="currency",
                function="transfer",
                sender="alice",
                tx_hash="abc123",
                block_height=9,
                mismatches={"writes": ({"a": 1}, {"a": 2})},
            )

            app = SimpleNamespace(
                chain_id="xian-local",
                tracer_mode="native_instruction_v1",
                execution_mode="xian_vm_v1",
                execution_runtime=SimpleNamespace(
                    authority="native",
                    shadow_execution=True,
                    bytecode_version="xvm-1",
                    gas_schedule="xvm-gas-1",
                ),
                block_service_mode=False,
                parallel_block_executor=SimpleNamespace(enabled=False),
                enable_tx_fee=True,
                current_block_meta={"height": 9},
                profiler=SimpleNamespace(
                    snapshot=lambda: {
                        "recent_blocks": [],
                        "global_metrics": {},
                        "updated_at_unix_ns": 0,
                    }
                ),
                vm_shadow_observer=observer,
            )
            service = SimpleNamespace(
                app=app,
                config=SimpleNamespace(enabled=True),
                last_bds_status=None,
                last_bds_refresh_success=False,
                last_bds_refresh_age_seconds=None,
            )

            families = list(XianMetricsCollector(service).collect())
            samples = [
                sample
                for family in families
                for sample in getattr(family, "samples", [])
            ]

            sample_by_name = {}
            for sample in samples:
                sample_by_name.setdefault(sample.name, []).append(sample)

            node_info = sample_by_name["xian_node_info"][0]
            self.assertEqual(node_info.labels["execution_mode"], "xian_vm_v1")
            self.assertEqual(node_info.labels["execution_authority"], "native")
            self.assertEqual(node_info.labels["execution_shadow"], "true")

            vm_shadow_metrics = {
                sample.labels["field"]: sample.value
                for sample in sample_by_name["xian_vm_shadow_metric"]
            }
            self.assertEqual(vm_shadow_metrics["comparisons_total"], 1.0)
            self.assertEqual(vm_shadow_metrics["mismatches_total"], 1.0)

            vm_stage_metrics = {
                (sample.labels["stage"], sample.labels["field"]): sample.value
                for sample in sample_by_name["xian_vm_shadow_stage_metric"]
            }
            self.assertEqual(
                vm_stage_metrics[
                    ("execute_tx_native_authoritative", "mismatches_total")
                ],
                1.0,
            )

            last_mismatch = sample_by_name["xian_vm_shadow_last_mismatch_info"][0]
            self.assertEqual(last_mismatch.labels["contract"], "currency")
            self.assertEqual(last_mismatch.labels["function"], "transfer")
            self.assertEqual(last_mismatch.labels["mismatch_fields"], "writes")


if __name__ == "__main__":
    unittest.main()
