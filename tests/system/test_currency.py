import datetime
import hashlib
import os
import unittest
from decimal import Decimal

from contracting.local import ContractingClient
from xian_accounts import Ed25519Account
from xian_runtime_types.time import Datetime

from xian.config_paths import resolve_contracts_dir


class TestCurrencyContract(unittest.TestCase):
    def setUp(self):

        # Called before every test, bootstraps the environment.
        self.chain_id = "test-chain"
        self.environment = {
            "chain_id": self.chain_id
        }
        self.deployer_vk = "xian-deployer"

        self.client = ContractingClient(environment=self.environment)
        self.client.flush()
        
        self.contracts_dir = str(resolve_contracts_dir())

        permit_authorizer_path = os.path.join(
            self.contracts_dir, "permit_authorizer.s.py"
        )
        currency_path = os.path.join(self.contracts_dir, "currency.s.py")

        with open(permit_authorizer_path) as f:
            code = f.read()
            self.client.submit(code, name="permit_authorizer")

        with open(currency_path) as f:
            code = f.read()
            self.client.submit(code, name="currency", constructor_args={"vk": self.deployer_vk})

        self.permit_authorizer = self.client.get_contract_proxy("permit_authorizer")
        self.currency = self.client.get_contract_proxy("currency")


    def tearDown(self):
        # Called after every test, ensures each test starts with a clean slate and is isolated from others
        self.client.flush()

    def test_balance_of(self):
        # GIVEN
        receiver = 'receiver_account'
        self.currency.balances[receiver] = 100000000000000

        # WHEN
        balance = self.currency.balance_of(address=receiver, signer="sys")

        # THEN
        self.assertEqual(balance, 100000000000000)

    def test_initial_balance(self):
        # GIVEN the initial setup
        # WHEN checking the initial balance
        sys_balance = self.currency.balances[self.deployer_vk]
        # THEN the balance should be as expected
        self.assertEqual(sys_balance, 5555555.55 + 5555555.55)

    def test_initial_team_lock_and_dao_balances(self):
        self.assertEqual(self.currency.balances["team_lock"], 16666666.65 + 49999999.95)
        self.assertEqual(self.currency.balances["dao"], 33333333.3)

    def test_intermediate_stream_accounts_are_empty(self):
        self.assertEqual(self.currency.balance_of(address="team_vesting", signer="sys"), 0)
        self.assertEqual(self.currency.balance_of(address="dao_funding_stream", signer="sys"), 0)

    def test_transfer(self):
        # GIVEN a transfer setup
        self.currency.transfer(amount=100, to="bob", signer=self.deployer_vk)
        # WHEN checking balances after transfer
        bob_balance = self.currency.balances["bob"]
        sys_balance = self.currency.balances[self.deployer_vk]
        # THEN the balances should reflect the transfer correctly
        self.assertEqual(bob_balance, 100)
        self.assertEqual(sys_balance, 5555555.55 + 5555555.55 - 100)


    def test_change_metadata(self):
        # GIVEN a non-operator trying to change metadata
        with self.assertRaises(Exception):
            self.currency.change_metadata(
                key="token_name", value="NEW TOKEN", signer="bob"
            )
        # WHEN the operator changes metadata
        self.currency.change_metadata(key="token_name", value="NEW TOKEN", signer="team_lock")
        new_name = self.currency.metadata["token_name"]
        # THEN the metadata should be updated correctly
        self.assertEqual(new_name, "NEW TOKEN")

    def test_change_metadata_cannot_replace_sensitive_authority_keys(self):
        for key in ("operator", "permit_authorizer", "total_supply"):
            with self.subTest(key=key):
                original = self.currency.metadata[key]
                with self.assertRaises(Exception):
                    self.currency.change_metadata(
                        key=key,
                        value="attacker",
                        signer="team_lock",
                    )
                self.assertEqual(self.currency.metadata[key], original)

    def test_governance_controls_sensitive_currency_settings(self):
        self.client.submit(
            """
@export
def approve_from_authorizer(owner: str, spender: str, amount: float):
    return True
""",
            name="new_permit_authorizer",
        )

        with self.assertRaises(Exception):
            self.currency.set_operator(
                new_operator="ops", signer="team_lock"
            )
        with self.assertRaises(Exception):
            self.currency.set_permit_authorizer(
                new_authorizer="new_permit_authorizer",
                signer="team_lock",
            )

        self.currency.set_operator(new_operator="ops", signer="governance")
        self.currency.set_permit_authorizer(
            new_authorizer="new_permit_authorizer",
            signer="governance",
        )

        self.assertEqual(self.currency.metadata["operator"], "ops")
        self.assertEqual(
            self.currency.metadata["permit_authorizer"],
            "new_permit_authorizer",
        )

    def test_approve_and_allowance(self):
        # GIVEN an approval setup
        self.currency.approve(amount=500, to="eve", signer="sys")
        # WHEN checking the allowance
        allowance = self.currency.approvals["sys", "eve"]
        # THEN the allowance should be set correctly
        self.assertEqual(allowance, 500)

    def test_transfer_from_without_approval(self):
        # GIVEN an attempt to transfer without approval
        # WHEN the transfer is attempted
        # THEN it should fail
        with self.assertRaises(Exception):
            self.currency.transfer_from(
                amount=100, to="bob", main_account="sys", signer="bob"
            )

    def test_transfer_from_with_approval(self):
        # GIVEN a setup with approval
        self.currency.approve(amount=200, to="bob", signer=self.deployer_vk)
        # WHEN transferring with approval
        self.currency.transfer_from(
            amount=100, to="bob", main_account=self.deployer_vk, signer="bob"
        )
        bob_balance = self.currency.balances["bob"]
        sys_balance = self.currency.balances[self.deployer_vk]
        remaining_allowance = self.currency.approvals[self.deployer_vk, "bob"]
        # THEN the balances and allowance should reflect the transfer
        self.assertEqual(bob_balance, 100)
        self.assertEqual(sys_balance, 5555555.55 + 5555555.55 - 100)
        self.assertEqual(remaining_allowance, 100)

    # XSC002 / Permit Tests

    # Helper Functions

    def fund_wallet(self, funder, spender, amount):
        self.currency.transfer(amount=100, to=spender, signer=funder)

    def construct_permit_msg(
        self,
        token_contract: str,
        owner: str,
        spender: str,
        value,
        deadline,
        nonce: int,
    ):
        amount = Decimal(str(value))
        amount_text = format(amount.normalize(), "f")
        if "." in amount_text:
            amount_text = amount_text.rstrip("0").rstrip(".")
        return "\n".join(
            [
                "xian-permit-v2",
                f"chain_id:{self.chain_id}",
                "authorizer:permit_authorizer",
                f"token_contract:{token_contract}",
                f"owner:{owner}",
                f"spender:{spender}",
                f"amount:{amount_text}",
                f"deadline:{deadline}",
                f"nonce:{nonce}",
            ]
        )

    def permit_hash(self, msg: str):
        return hashlib.sha3_256(msg.encode("utf-8")).hexdigest()

    def create_deadline(self, minutes=1):
        d = datetime.datetime.now() + datetime.timedelta(minutes=minutes)
        return Datetime(d.year, d.month, d.day, hour=d.hour, minute=d.minute)

    # Permit Tests

    def test_permit_valid(self):
        # GIVEN a valid permit setup
        private_key = 'ed30796abc4ab47a97bfb37359f50a9c362c7b304a4b4ad1b3f5369ecb6f7fd8'
        wallet = Ed25519Account(private_key)
        public_key = wallet.public_key
        deadline = str(self.create_deadline())
        spender = "some_spender"
        value = 100
        msg = self.construct_permit_msg(
            "currency", public_key, spender, value, deadline, nonce=0
        )
        msg_hash = self.permit_hash(msg)
        signature = wallet.sign_msg(msg)
        # WHEN the permit is granted
        response = self.permit_authorizer.permit(
            token_contract="currency",
            owner=public_key,
            spender=spender,
            value=value,
            deadline=deadline,
            nonce=0,
            signature=signature,
            return_full_output=True,
        )
        # THEN the response should indicate success
        permit = self.permit_authorizer.permits[msg_hash]
        expected_event = [{'contract': 'currency', 'event': 'Approve', 'signer': 'sys', 'caller': 'permit_authorizer', 'data_indexed': {'from': 'ddd326fddb5d1677595311f298b744a4e9f415b577ac179a6afbf38483dc0791', 'to': 'some_spender'}, 'data': {'amount': 100}}]
        self.assertEqual(response['events'], expected_event)
        self.assertEqual(permit, True)
        self.assertEqual(self.permit_authorizer.nonces[public_key], 1)

    def test_permit_canonicalizes_equivalent_amounts(self):
        private_key = 'ed30796abc4ab47a97bfb37359f50a9c362c7b304a4b4ad1b3f5369ecb6f7fd8'
        wallet = Ed25519Account(private_key)
        public_key = wallet.public_key
        deadline = str(self.create_deadline())
        spender = "some_spender"
        msg = self.construct_permit_msg(
            "currency", public_key, spender, 100, deadline, nonce=0
        )
        signature = wallet.sign_msg(msg)

        self.permit_authorizer.permit(
            token_contract="currency",
            owner=public_key,
            spender=spender,
            value=100.0,
            deadline=deadline,
            nonce=0,
            signature=signature,
        )

        self.assertEqual(self.currency.approvals[public_key, spender], 100)
        self.assertEqual(self.permit_authorizer.nonces[public_key], 1)

    def test_permit_expired(self):
        # GIVEN a permit setup with an expired deadline
        private_key = 'ed30796abc4ab47a97bfb37359f50a9c362c7b304a4b4ad1b3f5369ecb6f7fd8'
        wallet = Ed25519Account(private_key)
        public_key = wallet.public_key
        deadline = self.create_deadline(minutes=-1)  # Past deadline
        spender = "some_spender"
        value = 100
        msg = self.construct_permit_msg(
            "currency", public_key, spender, value, deadline, nonce=0
        )
        signature = wallet.sign_msg(msg)
        # WHEN the permit is attempted
        # THEN it should fail due to expiration
        with self.assertRaises(Exception) as context:
            self.permit_authorizer.permit(
                token_contract="currency",
                owner=public_key,
                spender=spender,
                value=value,
                deadline=str(deadline),
                nonce=0,
                signature=signature,
            )
        self.assertIn('Permit has expired', str(context.exception))

    def test_permit_invalid_signature(self):
        # GIVEN a permit setup with an invalid signature
        private_key = 'ed30796abc4ab47a97bfb37359f50a9c362c7b304a4b4ad1b3f5369ecb6f7fd8'
        wallet = Ed25519Account(private_key)
        public_key = wallet.public_key
        deadline = self.create_deadline()
        spender = "some_spender"
        value = 100
        msg = self.construct_permit_msg(
            "currency", public_key, spender, value, deadline, nonce=0
        )
        signature = wallet.sign_msg(msg + "tampered")
        # WHEN the permit is attempted
        # THEN it should fail due to invalid signature
        with self.assertRaises(Exception) as context:
            self.permit_authorizer.permit(
                token_contract="currency",
                owner=public_key,
                spender=spender,
                value=value,
                deadline=str(deadline),
                nonce=0,
                signature=signature,
            )
        self.assertIn('Invalid signature', str(context.exception))

    def test_permit_double_spending(self):
        # GIVEN a permit setup with a double spending attempt
        private_key = 'ed30796abc4ab47a97bfb37359f50a9c362c7b304a4b4ad1b3f5369ecb6f7fd8'
        wallet = Ed25519Account(private_key)
        public_key = wallet.public_key
        deadline = self.create_deadline()
        spender = "some_spender"
        value = 100
        msg = self.construct_permit_msg(
            "currency", public_key, spender, value, deadline, nonce=0
        )
        signature = wallet.sign_msg(msg)
        self.permit_authorizer.permit(
            token_contract="currency",
            owner=public_key,
            spender=spender,
            value=value,
            deadline=str(deadline),
            nonce=0,
            signature=signature,
        )
        # WHEN the permit is used again
        # THEN it should fail due to double spending
        with self.assertRaises(Exception) as context:
            self.permit_authorizer.permit(
                token_contract="currency",
                owner=public_key,
                spender=spender,
                value=value,
                deadline=str(deadline),
                nonce=0,
                signature=signature,
            )
        self.assertIn('Invalid permit nonce', str(context.exception))

    def test_permit_overwrites_previous_allowance(self):
        # GIVEN an initial allowance setup
        private_key = 'ed30796abc4ab47a97bfb37359f50a9c362c7b304a4b4ad1b3f5369ecb6f7fd8'
        wallet = Ed25519Account(private_key)
        public_key = wallet.public_key
        spender = "some_spender"
        initial_value = 500
        new_value = 200
        deadline = str(self.create_deadline())
        
        # Set initial allowance via permit
        msg = self.construct_permit_msg(
            "currency", public_key, spender, initial_value, deadline, nonce=0
        )
        signature = wallet.sign_msg(msg)
        self.permit_authorizer.permit(
            token_contract="currency",
            owner=public_key,
            spender=spender,
            value=initial_value,
            deadline=deadline,
            nonce=0,
            signature=signature,
        )
        
        # Verify initial allowance
        initial_allowance = self.currency.approvals[public_key, spender]
        self.assertEqual(initial_allowance, initial_value)
        
        # WHEN a new permit is granted
        msg = self.construct_permit_msg(
            "currency", public_key, spender, new_value, deadline, nonce=1
        )
        signature = wallet.sign_msg(msg)
        self.permit_authorizer.permit(
            token_contract="currency",
            owner=public_key,
            spender=spender,
            value=new_value,
            deadline=deadline,
            nonce=1,
            signature=signature,
        )
        
        # THEN the new allowance should overwrite the old one
        new_allowance = self.currency.approvals[public_key, spender]
        self.assertEqual(new_allowance, new_value)
        self.assertEqual(self.permit_authorizer.nonces[public_key], 2)

    def test_approve_overwrites_previous_allowance(self):
        # GIVEN an initial approval setup
        self.currency.approve(amount=500, to="eve", signer="sys")
        initial_allowance = self.currency.approvals["sys", "eve"]
        self.assertEqual(initial_allowance, 500)
        
        # WHEN a new approval is made
        self.currency.approve(amount=200, to="eve", signer="sys")
        new_allowance = self.currency.approvals["sys", "eve"]
        
        # THEN the new allowance should overwrite the old one
        self.assertEqual(new_allowance, 200)

    def test_approve_from_authorizer_rejects_direct_callers(self):
        with self.assertRaises(Exception) as context:
            self.currency.approve_from_authorizer(
                owner="alice",
                spender="bob",
                amount=100,
                signer="mallory",
            )
        self.assertIn(
            "Only permit authorizer can approve on behalf of others.",
            str(context.exception),
        )

if __name__ == "__main__":
    unittest.main()
