from __future__ import annotations

import argparse
import statistics
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from contracting.storage.driver import Driver

from xian.state_root import (
    StateRootCache,
    compute_driver_state_root,
    merkle_root_from_items,
)


def build_items(size: int) -> list[tuple[str, Any]]:
    return [
        (
            f"con_bench.balances:{index:08d}",
            {
                "amount": index,
                "owner": f"vk-{index % 997}",
                "flags": [index % 2 == 0, index % 5 == 0],
            },
        )
        for index in range(size)
    ]


def time_root(items: Iterable[tuple[str, Any]], repeats: int) -> list[float]:
    durations = []
    materialized_items = list(items)
    for _ in range(repeats):
        started = time.perf_counter()
        merkle_root_from_items(materialized_items)
        durations.append((time.perf_counter() - started) * 1000)
    return durations


def time_driver_root(items: Iterable[tuple[str, Any]], repeats: int) -> list[float]:
    materialized_items = dict(items)
    durations = []
    with tempfile.TemporaryDirectory() as tmp_dir:
        driver = Driver(storage_home=Path(tmp_dir))
        driver._store.batch_set(materialized_items)
        driver.flush_cache()
        for _ in range(repeats):
            started = time.perf_counter()
            compute_driver_state_root(driver)
            durations.append((time.perf_counter() - started) * 1000)
    return durations


def time_cached_updates(
    items: Iterable[tuple[str, Any]],
    *,
    update_count: int,
    repeats: int,
) -> list[float]:
    materialized_items = list(items)
    cache = StateRootCache(materialized_items)
    writes = {
        key: {
            "amount": index + 1_000_000,
            "owner": f"updated-vk-{index}",
        }
        for index, (key, _value) in enumerate(
            materialized_items[: max(update_count, 0)]
        )
    }
    durations = []
    for _ in range(repeats):
        started = time.perf_counter()
        cache.prepare(writes)
        durations.append((time.perf_counter() - started) * 1000)
        cache.rollback()
    return durations


def print_durations(source: str, size: int, durations: list[float]) -> None:
    print(
        "source={source} size={size} median_ms={median:.2f} "
        "min_ms={min_value:.2f} max_ms={max_value:.2f}".format(
            source=source,
            size=size,
            median=statistics.median(durations),
            min_value=min(durations),
            max_value=max(durations),
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark Xian state-root Merkle computation."
    )
    parser.add_argument(
        "--sizes",
        nargs="+",
        type=int,
        default=[1_000, 10_000, 50_000],
        help="state entry counts to benchmark",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=5,
        help="measurements per size",
    )
    parser.add_argument(
        "--driver",
        action="store_true",
        help="also benchmark root calculation through Driver.items()",
    )
    parser.add_argument(
        "--update-counts",
        nargs="+",
        type=int,
        default=[1, 10, 100],
        help="touched key counts to benchmark through the incremental cache",
    )
    args = parser.parse_args()

    for size in args.sizes:
        items = build_items(size)
        repeats = max(args.repeats, 1)
        print_durations("memory", size, time_root(items, repeats))
        if args.driver:
            print_durations("driver", size, time_driver_root(items, repeats))
        for update_count in args.update_counts:
            print_durations(
                f"cache-update-{update_count}",
                size,
                time_cached_updates(
                    items,
                    update_count=update_count,
                    repeats=repeats,
                ),
            )


if __name__ == "__main__":
    main()
