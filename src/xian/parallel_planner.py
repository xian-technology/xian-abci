from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TransactionAccess:
    index: int
    sender: str
    nonce: int
    reads: frozenset[str]
    writes: frozenset[str]
    additive_writes: frozenset[str]
    status: int

    @classmethod
    def from_output(cls, index: int, tx: dict, output: dict):
        return cls(
            index=index,
            sender=tx["payload"]["sender"],
            nonce=tx["payload"]["nonce"],
            reads=frozenset(output["reads"].keys()),
            writes=frozenset(output["writes"].keys()),
            additive_writes=frozenset(),
            status=output["status_code"],
        )


@dataclass(frozen=True)
class ParallelStage:
    tx_indexes: tuple[int, ...]
    senders: frozenset[str]
    reads: frozenset[str]
    writes: frozenset[str]
    additive_writes: frozenset[str]

    @property
    def size(self) -> int:
        return len(self.tx_indexes)


@dataclass(frozen=True)
class ParallelPlan:
    stages: tuple[ParallelStage, ...]

    @property
    def stage_count(self) -> int:
        return len(self.stages)

    @property
    def max_stage_size(self) -> int:
        if not self.stages:
            return 0
        return max(stage.size for stage in self.stages)

    @property
    def parallelizable_transactions(self) -> int:
        return sum(max(stage.size - 1, 0) for stage in self.stages)


class ParallelExecutionPlanner:
    """Build contiguous, deterministic parallel stages.

    The planner is intentionally conservative:
    - canonical transaction order is preserved
    - stages are contiguous windows
    - the same sender never appears twice in one stage
    - read/write and write/write overlaps force a stage boundary
    """

    def build(self, accesses: list[TransactionAccess]) -> ParallelPlan:
        stages: list[ParallelStage] = []
        current_stage: list[TransactionAccess] = []

        for access in accesses:
            if current_stage and self._conflicts_with_stage(
                access, current_stage
            ):
                stages.append(self._make_stage(current_stage))
                current_stage = [access]
            else:
                current_stage.append(access)

        if current_stage:
            stages.append(self._make_stage(current_stage))

        return ParallelPlan(stages=tuple(stages))

    def _conflicts_with_stage(
        self, access: TransactionAccess, stage: list[TransactionAccess]
    ) -> bool:
        stage_senders = {item.sender for item in stage}
        if access.sender in stage_senders:
            return True

        stage_reads = set().union(*(item.reads for item in stage))
        stage_writes = set().union(*(item.writes for item in stage))
        stage_additive_writes = set().union(
            *(item.additive_writes for item in stage)
        )

        if access.writes & stage_writes:
            return True

        if access.writes & stage_reads:
            return True

        if access.writes & stage_additive_writes:
            return True

        if access.reads & stage_writes:
            return True

        if access.reads & stage_additive_writes:
            return True

        if access.additive_writes & stage_reads:
            return True

        if access.additive_writes & stage_writes:
            return True

        return False

    def _make_stage(self, stage: list[TransactionAccess]) -> ParallelStage:
        return ParallelStage(
            tx_indexes=tuple(item.index for item in stage),
            senders=frozenset(item.sender for item in stage),
            reads=frozenset().union(*(item.reads for item in stage)),
            writes=frozenset().union(*(item.writes for item in stage)),
            additive_writes=frozenset().union(
                *(item.additive_writes for item in stage)
            ),
        )
