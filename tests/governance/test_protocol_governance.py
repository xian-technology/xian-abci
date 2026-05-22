import unittest
from pathlib import Path

from contracting.local import ContractingClient
from xian_runtime_types.time import Datetime

MEMBERSHIP_CONTRACT = """
members = Variable()

@construct
def seed(initial_members: list):
    members.set(initial_members)

@export
def get_members():
    return members.get()

@export
def is_member(account: str):
    return account in members.get()

@export
def add_member(account: str):
    current = members.get()
    current.append(account)
    members.set(current)
"""


WEIGHTED_MEMBERSHIP_CONTRACT = """
members = Variable()
weights = Hash(default_value=0)

@construct
def seed(initial_members: list, initial_weights: dict):
    members.set(initial_members)
    for account in initial_members:
        configured_weight = initial_weights.get(account)
        if configured_weight is None:
            configured_weight = 1
        weights[account] = configured_weight

@export
def get_members():
    return members.get()

@export
def is_member(account: str):
    return account in members.get()

@export
def member_weight(account: str):
    if account not in members.get():
        return 0
    return weights[account]

@export
def total_member_weight():
    total = 0
    for account in members.get():
        total += weights[account]
    return total

@export
def add_member(account: str, weight: int = 1):
    current = members.get()
    current.append(account)
    members.set(current)
    weights[account] = weight
"""


TARGET_CONTRACT = """
value = Variable()

@export
def set_value(next_value: str):
    value.set(next_value)

@export
def get_value():
    return value.get()
"""


def governance_contract_source() -> str:
    return (
        Path(__file__).resolve().parents[3]
        / "xian-configs"
        / "contracts"
        / "governance.s.py"
    ).read_text(encoding="utf-8")


class ProtocolGovernanceContractTests(unittest.TestCase):
    def setUp(self):
        self.client = ContractingClient(environment={"chain_id": "test-chain"})
        self.client.flush()
        self.client.submit(
            MEMBERSHIP_CONTRACT,
            name="masternodes",
            constructor_args={"initial_members": ["node1", "node2"]},
        )
        self.client.submit(
            TARGET_CONTRACT,
            name="con_target",
        )
        self.client.submit(
            governance_contract_source(),
            name="governance",
            constructor_args={
                "membership_contract_name": "masternodes",
                "approval_threshold_numerator": 1,
                "approval_threshold_denominator": 1,
                "min_patch_delay_blocks": 2,
                "emergency_patch_delay_blocks": 1,
            },
        )
        self.governance = self.client.get_contract_proxy("governance")
        self.target = self.client.get_contract_proxy("con_target")

    def test_contract_call_proposal_executes_after_threshold(self):
        environment = {
            "now": Datetime(2026, 1, 1),
            "block_num": 10,
            "chain_id": "test-chain",
        }

        proposal = self.governance.propose_contract_call(
            target_contract="con_target",
            target_function="set_value",
            kwargs={"next_value": "hello"},
            summary="update target",
            signer="node1",
            environment=environment,
        )
        self.assertEqual(proposal["status"], "pending")
        self.assertIsNone(self.target.get_value())

        proposal = self.governance.vote(
            proposal_id=1,
            support=True,
            signer="node2",
            environment=environment,
        )
        self.assertEqual(proposal["status"], "executed")
        self.assertEqual(self.target.get_value(), "hello")

    def test_state_patch_proposal_enforces_delay_and_schedules_patch(self):
        environment = {
            "now": Datetime(2026, 1, 1),
            "block_num": 10,
            "chain_id": "test-chain",
        }

        with self.assertRaises(AssertionError):
            self.governance.propose_state_patch(
                patch_id="patch-delay",
                bundle_hash="abc123",
                activation_height=11,
                signer="node1",
                environment=environment,
            )

        proposal = self.governance.propose_state_patch(
            patch_id="patch-delay",
            bundle_hash="abc123",
            activation_height=11,
            emergency=True,
            signer="node1",
            environment=environment,
        )
        self.assertEqual(proposal["status"], "pending")

        proposal = self.governance.vote(
            proposal_id=1,
            support=True,
            signer="node2",
            environment=environment,
        )
        self.assertEqual(proposal["status"], "approved")
        patch = self.governance.get_patch(patch_id="patch-delay")
        self.assertEqual(patch["status"], "approved")
        self.assertEqual(patch["activation_height"], 11)
        self.assertTrue(
            self.governance.is_patch_approved(patch_id="patch-delay")
        )

    def test_threshold_is_snapshotted_when_membership_changes_mid_vote(self):
        self.client.flush()
        self.client.submit(
            MEMBERSHIP_CONTRACT,
            name="masternodes",
            constructor_args={"initial_members": ["node1", "node2", "node3"]},
        )
        self.client.submit(
            TARGET_CONTRACT,
            name="con_target",
        )
        self.client.submit(
            governance_contract_source(),
            name="governance",
            constructor_args={
                "membership_contract_name": "masternodes",
                "approval_threshold_numerator": 2,
                "approval_threshold_denominator": 3,
                "min_patch_delay_blocks": 2,
                "emergency_patch_delay_blocks": 1,
            },
        )
        governance = self.client.get_contract_proxy("governance")
        target = self.client.get_contract_proxy("con_target")
        membership = self.client.get_contract_proxy("masternodes")
        environment = {
            "now": Datetime(2026, 1, 1),
            "block_num": 10,
            "chain_id": "test-chain",
        }

        proposal = governance.propose_contract_call(
            target_contract="con_target",
            target_function="set_value",
            kwargs={"next_value": "snapshot"},
            summary="update target",
            signer="node1",
            environment=environment,
        )
        self.assertEqual(proposal["required_yes_votes"], 2)
        self.assertEqual(proposal["status"], "pending")

        membership.add_member(
            account="node4",
            signer="node1",
            environment=environment,
        )
        proposal = governance.vote(
            proposal_id=1,
            support=True,
            signer="node2",
            environment=environment,
        )
        self.assertEqual(proposal["required_yes_votes"], 2)
        self.assertEqual(proposal["status"], "executed")
        self.assertEqual(target.get_value(), "snapshot")

    def test_weighted_membership_uses_snapshot_weights_for_approval(self):
        self.client.flush()
        self.client.submit(
            WEIGHTED_MEMBERSHIP_CONTRACT,
            name="masternodes",
            constructor_args={
                "initial_members": ["node1", "node2", "node3"],
                "initial_weights": {
                    "node1": 40,
                    "node2": 30,
                    "node3": 30,
                },
            },
        )
        self.client.submit(
            TARGET_CONTRACT,
            name="con_target",
        )
        self.client.submit(
            governance_contract_source(),
            name="governance",
            constructor_args={
                "membership_contract_name": "masternodes",
                "approval_threshold_numerator": 2,
                "approval_threshold_denominator": 3,
                "min_patch_delay_blocks": 2,
                "emergency_patch_delay_blocks": 1,
            },
        )
        governance = self.client.get_contract_proxy("governance")
        membership = self.client.get_contract_proxy("masternodes")
        target = self.client.get_contract_proxy("con_target")
        environment = {
            "now": Datetime(2026, 1, 1),
            "block_num": 10,
            "chain_id": "test-chain",
        }

        proposal = governance.propose_contract_call(
            target_contract="con_target",
            target_function="set_value",
            kwargs={"next_value": "weighted"},
            summary="weighted governance",
            signer="node1",
            environment=environment,
        )
        self.assertEqual(proposal["yes_votes"], 1)
        self.assertEqual(proposal["yes_weight"], 40)
        self.assertEqual(proposal["required_yes_weight"], 67)
        self.assertEqual(proposal["status"], "pending")

        membership.add_member(
            account="node4",
            weight=100,
            signer="node1",
            environment=environment,
        )

        with self.assertRaises(AssertionError):
            governance.vote(
                proposal_id=1,
                support=True,
                signer="node4",
                environment=environment,
            )

        proposal = governance.vote(
            proposal_id=1,
            support=True,
            signer="node2",
            environment=environment,
        )
        self.assertEqual(proposal["status"], "executed")
        self.assertEqual(proposal["yes_weight"], 70)
        self.assertEqual(target.get_value(), "weighted")

    def test_weighted_membership_low_weight_no_votes_do_not_reject_by_count(self):
        self.client.flush()
        self.client.submit(
            WEIGHTED_MEMBERSHIP_CONTRACT,
            name="masternodes",
            constructor_args={
                "initial_members": [
                    "node1",
                    "node2",
                    "node3",
                    "node4",
                    "node5",
                ],
                "initial_weights": {
                    "node1": 100,
                    "node2": 10,
                    "node3": 10,
                    "node4": 10,
                    "node5": 10,
                },
            },
        )
        self.client.submit(TARGET_CONTRACT, name="con_target")
        self.client.submit(
            governance_contract_source(),
            name="governance",
            constructor_args={
                "membership_contract_name": "masternodes",
                "approval_threshold_numerator": 4,
                "approval_threshold_denominator": 5,
                "min_patch_delay_blocks": 2,
                "emergency_patch_delay_blocks": 1,
            },
        )
        governance = self.client.get_contract_proxy("governance")
        target = self.client.get_contract_proxy("con_target")
        environment = {
            "now": Datetime(2026, 1, 1),
            "block_num": 10,
            "chain_id": "test-chain",
        }

        proposal = governance.propose_contract_call(
            target_contract="con_target",
            target_function="set_value",
            kwargs={"next_value": "weighted-count-regression"},
            summary="weighted no count regression",
            signer="node1",
            environment=environment,
        )
        self.assertEqual(proposal["yes_weight"], 100)
        self.assertEqual(proposal["required_yes_weight"], 112)
        self.assertEqual(proposal["status"], "pending")

        proposal = governance.vote(
            proposal_id=1,
            support=False,
            signer="node2",
            environment=environment,
        )
        self.assertEqual(proposal["status"], "pending")
        proposal = governance.vote(
            proposal_id=1,
            support=False,
            signer="node3",
            environment=environment,
        )
        self.assertEqual(proposal["no_votes"], 2)
        self.assertEqual(proposal["no_weight"], 20)
        self.assertEqual(proposal["status"], "pending")

        proposal = governance.vote(
            proposal_id=1,
            support=True,
            signer="node4",
            environment=environment,
        )
        self.assertEqual(proposal["status"], "pending")
        proposal = governance.vote(
            proposal_id=1,
            support=True,
            signer="node5",
            environment=environment,
        )
        self.assertEqual(proposal["status"], "executed")
        self.assertEqual(target.get_value(), "weighted-count-regression")
