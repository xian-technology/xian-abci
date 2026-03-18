import unittest

from xian.parallel_planner import (
    ParallelExecutionPlanner,
    TransactionAccess,
)


def access(
    index,
    sender,
    reads=(),
    writes=(),
    additive_writes=(),
    nonce=0,
    status=0,
):
    return TransactionAccess(
        index=index,
        sender=sender,
        nonce=nonce,
        reads=frozenset(reads),
        writes=frozenset(writes),
        additive_writes=frozenset(additive_writes),
        status=status,
    )


class TestParallelExecutionPlanner(unittest.TestCase):
    def setUp(self):
        self.planner = ParallelExecutionPlanner()

    def test_groups_independent_transactions_into_one_stage(self):
        plan = self.planner.build(
            [
                access(0, "alice", reads={"currency.balances:alice"}),
                access(1, "bob", reads={"currency.balances:bob"}),
                access(2, "carol", writes={"dex.orders:1"}),
            ]
        )

        self.assertEqual(plan.stage_count, 1)
        self.assertEqual(plan.max_stage_size, 3)
        self.assertEqual(plan.parallelizable_transactions, 2)
        self.assertEqual(plan.stages[0].tx_indexes, (0, 1, 2))

    def test_splits_stage_on_write_read_conflict(self):
        plan = self.planner.build(
            [
                access(0, "alice", writes={"currency.balances:alice"}),
                access(1, "bob", reads={"currency.balances:alice"}),
                access(2, "carol", reads={"currency.balances:carol"}),
            ]
        )

        self.assertEqual(plan.stage_count, 2)
        self.assertEqual(plan.stages[0].tx_indexes, (0,))
        self.assertEqual(plan.stages[1].tx_indexes, (1, 2))

    def test_splits_stage_on_same_sender(self):
        plan = self.planner.build(
            [
                access(0, "alice", writes={"currency.balances:alice"}, nonce=1),
                access(1, "alice", writes={"dex.orders:1"}, nonce=2),
            ]
        )

        self.assertEqual(plan.stage_count, 2)
        self.assertEqual(plan.stages[0].tx_indexes, (0,))
        self.assertEqual(plan.stages[1].tx_indexes, (1,))

    def test_preserves_contiguous_stage_boundaries(self):
        plan = self.planner.build(
            [
                access(0, "alice", writes={"currency.balances:alice"}),
                access(1, "bob", reads={"currency.balances:alice"}),
                access(2, "carol", reads={"dex.pairs:1"}),
            ]
        )

        self.assertEqual(plan.stage_count, 2)
        self.assertEqual(plan.stages[0].tx_indexes, (0,))
        self.assertEqual(plan.stages[1].tx_indexes, (1, 2))

    def test_allows_parallel_additive_reward_writes(self):
        plan = self.planner.build(
            [
                access(
                    0, "alice", additive_writes={"currency.balances:foundation"}
                ),
                access(
                    1, "bob", additive_writes={"currency.balances:foundation"}
                ),
                access(2, "carol", writes={"dex.orders:1"}),
            ]
        )

        self.assertEqual(plan.stage_count, 1)
        self.assertEqual(plan.stages[0].tx_indexes, (0, 1, 2))

    def test_splits_stage_when_read_depends_on_additive_reward_write(self):
        plan = self.planner.build(
            [
                access(
                    0, "alice", additive_writes={"currency.balances:foundation"}
                ),
                access(1, "bob", reads={"currency.balances:foundation"}),
            ]
        )

        self.assertEqual(plan.stage_count, 2)
        self.assertEqual(plan.stages[0].tx_indexes, (0,))
        self.assertEqual(plan.stages[1].tx_indexes, (1,))


if __name__ == "__main__":
    unittest.main()
