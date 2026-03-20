from __future__ import annotations

import json
import os
import socket
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any, Iterator


def _percentile(sorted_values: list[int], ratio: float) -> int | None:
    if not sorted_values:
        return None
    index = int(round((len(sorted_values) - 1) * ratio))
    return sorted_values[max(0, min(index, len(sorted_values) - 1))]


def _ms(value_ns: int | None) -> float | None:
    if value_ns is None:
        return None
    return round(value_ns / 1_000_000, 3)


@dataclass
class PerfStat:
    count: int = 0
    total_ns: int = 0
    min_ns: int | None = None
    max_ns: int = 0
    samples_ns: deque[int] = field(default_factory=lambda: deque(maxlen=2048))

    def observe(self, duration_ns: int) -> None:
        self.count += 1
        self.total_ns += duration_ns
        self.max_ns = max(self.max_ns, duration_ns)
        self.min_ns = (
            duration_ns
            if self.min_ns is None
            else min(self.min_ns, duration_ns)
        )
        self.samples_ns.append(duration_ns)

    def to_dict(self) -> dict[str, Any]:
        sample_values = sorted(self.samples_ns)
        avg_ns = self.total_ns / self.count if self.count else None
        return {
            "count": self.count,
            "total_ms": _ms(self.total_ns),
            "avg_ms": _ms(int(avg_ns)) if avg_ns is not None else None,
            "min_ms": _ms(self.min_ns),
            "max_ms": _ms(self.max_ns if self.count else None),
            "p95_ms": _ms(_percentile(sample_values, 0.95)),
            "recent_sample_count": len(sample_values),
        }


@dataclass
class BlockPerfSnapshot:
    height: int
    tx_count: int
    duration_ns: int
    metrics: dict[str, PerfStat]
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "height": self.height,
            "tx_count": self.tx_count,
            "duration_ms": _ms(self.duration_ns),
            "metrics": {
                name: stat.to_dict()
                for name, stat in sorted(self.metrics.items())
            },
            "metadata": self.metadata,
        }


@dataclass
class _ActiveBlock:
    height: int
    tx_count: int
    started_ns: int
    metrics: dict[str, PerfStat] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


class NoopPerfTracker:
    enabled = False

    @contextmanager
    def scope(
        self,
        name: str,
        *,
        block_scoped: bool = False,
    ) -> Iterator[None]:
        yield

    def start_block(self, height: int, tx_count: int) -> None:
        return None

    def set_block_metadata(self, **metadata: Any) -> None:
        return None

    def end_block(self, **metadata: Any) -> None:
        return None

    def flush(self) -> None:
        return None


class PerfTracker:
    def __init__(
        self,
        *,
        output_path: Path,
        node_name: str,
        chain_id: str,
        tracer_mode: str,
        recent_blocks: int = 32,
    ) -> None:
        self.enabled = True
        self.output_path = output_path
        self.node_name = node_name
        self.chain_id = chain_id
        self.tracer_mode = tracer_mode
        self.recent_blocks = max(1, recent_blocks)
        self.global_metrics: dict[str, PerfStat] = {}
        self.blocks: deque[BlockPerfSnapshot] = deque(maxlen=self.recent_blocks)
        self.active_block: _ActiveBlock | None = None
        self.lock = Lock()

    @classmethod
    def from_env(
        cls,
        *,
        cometbft_home: Path,
        node_name: str,
        chain_id: str,
        tracer_mode: str,
    ) -> PerfTracker | NoopPerfTracker:
        if os.environ.get("XIAN_PERF_ENABLED", "").strip().lower() not in {
            "1",
            "true",
            "yes",
            "on",
        }:
            return NoopPerfTracker()

        output_path = Path(
            os.environ.get(
                "XIAN_PERF_OUTPUT_PATH",
                str(cometbft_home / "xian-perf.json"),
            )
        ).expanduser()
        recent_blocks = int(os.environ.get("XIAN_PERF_RECENT_BLOCKS", "32"))
        return cls(
            output_path=output_path,
            node_name=node_name or socket.gethostname(),
            chain_id=chain_id,
            tracer_mode=tracer_mode,
            recent_blocks=recent_blocks,
        )

    def _metric(self, name: str) -> PerfStat:
        return self.global_metrics.setdefault(name, PerfStat())

    def _block_metric(self, name: str) -> PerfStat | None:
        if self.active_block is None:
            return None
        return self.active_block.metrics.setdefault(name, PerfStat())

    def observe(
        self,
        name: str,
        duration_ns: int,
        *,
        block_scoped: bool = False,
    ) -> None:
        with self.lock:
            self._metric(name).observe(duration_ns)
            if block_scoped and self.active_block is not None:
                self._block_metric(name).observe(duration_ns)

    @contextmanager
    def scope(
        self,
        name: str,
        *,
        block_scoped: bool = False,
    ) -> Iterator[None]:
        started_ns = time.perf_counter_ns()
        try:
            yield
        finally:
            self.observe(
                name,
                time.perf_counter_ns() - started_ns,
                block_scoped=block_scoped,
            )

    def start_block(self, height: int, tx_count: int) -> None:
        with self.lock:
            self.active_block = _ActiveBlock(
                height=height,
                tx_count=tx_count,
                started_ns=time.perf_counter_ns(),
            )

    def set_block_metadata(self, **metadata: Any) -> None:
        with self.lock:
            if self.active_block is None:
                return
            self.active_block.metadata.update(metadata)

    def end_block(self, **metadata: Any) -> None:
        with self.lock:
            if self.active_block is None:
                return
            self.active_block.metadata.update(metadata)
            duration_ns = time.perf_counter_ns() - self.active_block.started_ns
            self.blocks.append(
                BlockPerfSnapshot(
                    height=self.active_block.height,
                    tx_count=self.active_block.tx_count,
                    duration_ns=duration_ns,
                    metrics=dict(self.active_block.metrics),
                    metadata=dict(self.active_block.metadata),
                )
            )
            self.active_block = None
        self.flush()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "node_name": self.node_name,
                "chain_id": self.chain_id,
                "tracer_mode": self.tracer_mode,
                "pid": os.getpid(),
                "updated_at_unix_ns": time.time_ns(),
                "global_metrics": {
                    name: stat.to_dict()
                    for name, stat in sorted(self.global_metrics.items())
                },
                "recent_blocks": [block.to_dict() for block in self.blocks],
            }

    def flush(self) -> None:
        payload = self.snapshot()
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.output_path.with_suffix(
            self.output_path.suffix + ".tmp"
        )
        temp_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temp_path.replace(self.output_path)
