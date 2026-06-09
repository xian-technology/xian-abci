"""Tests for the block performance tracker."""

import json

from xian.perf import NoopPerfTracker, PerfStat, PerfTracker


def _tracker(tmp_path, **overrides):
    kwargs = {
        "output_path": tmp_path / "perf.json",
        "node_name": "node0",
        "chain_id": "xian-test",
        "execution_mode": "xian_vm_v1",
    }
    kwargs.update(overrides)
    return PerfTracker(**kwargs)


def test_perf_stat_aggregates_observations():
    stat = PerfStat()
    for duration in (4_000_000, 1_000_000, 3_000_000):
        stat.observe(duration)

    summary = stat.to_dict()

    assert summary["count"] == 3
    assert summary["total_ms"] == 8.0
    assert summary["avg_ms"] == 2.667
    assert summary["min_ms"] == 1.0
    assert summary["max_ms"] == 4.0
    assert summary["p95_ms"] == 4.0
    assert summary["recent_sample_count"] == 3


def test_perf_stat_empty_reports_nones():
    summary = PerfStat().to_dict()

    assert summary["count"] == 0
    assert summary["avg_ms"] is None
    assert summary["min_ms"] is None
    assert summary["max_ms"] is None
    assert summary["p95_ms"] is None


def test_noop_tracker_is_inert():
    tracker = NoopPerfTracker()

    with tracker.scope("anything", block_scoped=True):
        pass
    tracker.start_block(1, 2)
    tracker.set_block_metadata(app_hash="ab")
    tracker.end_block()
    tracker.flush()

    snapshot = tracker.snapshot()
    assert snapshot["enabled"] is False
    assert snapshot["global_metrics"] == {}
    assert snapshot["recent_blocks"] == []


def test_from_env_returns_noop_unless_enabled(monkeypatch, tmp_path):
    monkeypatch.delenv("XIAN_PERF_ENABLED", raising=False)

    tracker = PerfTracker.from_env(
        cometbft_home=tmp_path,
        node_name="node0",
        chain_id="xian-test",
        execution_mode="xian_vm_v1",
    )

    assert isinstance(tracker, NoopPerfTracker)


def test_from_env_builds_tracker_with_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("XIAN_PERF_ENABLED", "true")
    monkeypatch.setenv("XIAN_PERF_OUTPUT_PATH", str(tmp_path / "custom.json"))
    monkeypatch.setenv("XIAN_PERF_RECENT_BLOCKS", "4")

    tracker = PerfTracker.from_env(
        cometbft_home=tmp_path,
        node_name="node0",
        chain_id="xian-test",
        execution_mode="xian_vm_v1",
    )

    assert isinstance(tracker, PerfTracker)
    assert tracker.output_path == tmp_path / "custom.json"
    assert tracker.recent_blocks == 4


def test_scope_records_block_scoped_metrics(tmp_path):
    tracker = _tracker(tmp_path)
    tracker.start_block(7, tx_count=3)

    with tracker.scope("finalize_block", block_scoped=True):
        pass
    with tracker.scope("check_tx"):
        pass

    assert tracker.global_metrics["finalize_block"].count == 1
    assert tracker.global_metrics["check_tx"].count == 1
    assert tracker.active_block.metrics["finalize_block"].count == 1
    assert "check_tx" not in tracker.active_block.metrics


def test_end_block_snapshots_and_flushes(tmp_path):
    tracker = _tracker(tmp_path)
    tracker.start_block(7, tx_count=3)
    tracker.observe("finalize_block", 2_000_000, block_scoped=True)
    tracker.set_block_metadata(proposer="node0")

    tracker.end_block(app_hash="ab" * 32)

    assert tracker.active_block is None
    snapshot = tracker.snapshot()
    assert snapshot["enabled"] is True
    (block,) = snapshot["recent_blocks"]
    assert block["height"] == 7
    assert block["tx_count"] == 3
    assert block["metadata"] == {"proposer": "node0", "app_hash": "ab" * 32}
    assert block["metrics"]["finalize_block"]["count"] == 1

    on_disk = json.loads((tmp_path / "perf.json").read_text(encoding="utf-8"))
    assert on_disk["recent_blocks"][0]["height"] == 7
    assert not (tmp_path / "perf.json.tmp").exists()


def test_block_lifecycle_ignores_calls_without_active_block(tmp_path):
    tracker = _tracker(tmp_path)

    tracker.set_block_metadata(ignored=True)
    tracker.end_block()
    tracker.observe("finalize_block", 1_000, block_scoped=True)

    assert tracker.snapshot()["recent_blocks"] == []
    assert tracker.global_metrics["finalize_block"].count == 1


def test_recent_blocks_bounded(tmp_path):
    tracker = _tracker(tmp_path, recent_blocks=1)

    for height in (1, 2):
        tracker.start_block(height, tx_count=0)
        tracker.end_block()

    (block,) = tracker.snapshot()["recent_blocks"]
    assert block["height"] == 2
