import datetime
import os
import unittest

from contracting.client import ContractingClient
from contracting.stdlib.bridge.hashing import sha3
from xian_accounts import Ed25519Account
from xian.config_paths import resolve_contracts_dir
from xian_runtime_types.time import Datetime


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

        
        currency_path = os.path.join(self.contracts_dir, "currency.s.py")

        with open(currency_path) as f:
            code = f.read()
            self.client.submit(code, name="currency", constructor_args={"vk": self.deployer_vk})

        self.currency = self.client.get_contract("currency")


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

    def test_approve_and_allowance(self):
        # GIVEN an approval setup
        self.currency.approve(amount=500, to="eve", signer="sys")
        # WHEN checking the allowance
        allowance = self.currency.balances["sys", "eve"]
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
        remaining_allowance = self.currency.balances[self.deployer_vk, "bob"]
        # THEN the balances and allowance should reflect the transfer
        self.assertEqual(bob_balance, 100)
        self.assertEqual(sys_balance, 5555555.55 + 5555555.55 - 100)
        self.assertEqual(remaining_allowance, 100)

    # XSC002 / Permit Tests

    # Helper Functions

    def fund_wallet(self, funder, spender, amount):
        self.currency.transfer(amount=100, to=spender, signer=funder)

    def construct_permit_msg(self, owner: str, spender: str, value: float, deadline: dict):
        return f"{owner}:{spender}:{value}:{deadline}:currency:{self.chain_id}"

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
        msg = self.construct_permit_msg(public_key, spender, value, deadline)
        msg_hash = sha3(msg)
        signature = wallet.sign_msg(msg)
        # WHEN the permit is granted
        response = self.currency.permit(owner=public_key, spender=spender, value=value, deadline=deadline, signature=signature, return_full_output=True)
        # THEN the response should indicate success
        permit = self.currency.permits[msg_hash]
        expected_event = [{'contract': 'currency', 'event': 'Approve', 'signer': 'sys', 'caller': 'sys', 'data_indexed': {'from': 'ddd326fddb5d1677595311f298b744a4e9f415b577ac179a6afbf38483dc0791', 'to': 'some_spender'}, 'data': {'amount': 100}}]
        self.assertEqual(response['events'], expected_event)
        self.assertEqual(permit, True)

    def test_permit_expired(self):
        # GIVEN a permit setup with an expired deadline
        private_key = 'ed30796abc4ab47a97bfb37359f50a9c362c7b304a4b4ad1b3f5369ecb6f7fd8'
        wallet = Ed25519Account(private_key)
        public_key = wallet.public_key
        deadline = self.create_deadline(minutes=-1)  # Past deadline
        spender = "some_spender"
        value = 100
        msg = self.construct_permit_msg(public_key, spender, value, deadline)
        signature = wallet.sign_msg(msg)
        # WHEN the permit is attempted
        # THEN it should fail due to expiration
        with self.assertRaises(Exception) as context:
            self.currency.permit(owner=public_key, spender=spender, value=value, deadline=str(deadline), signature=signature)
        self.assertIn('Permit has expired', str(context.exception))

    def test_permit_invalid_signature(self):
        # GIVEN a permit setup with an invalid signature
        private_key = 'ed30796abc4ab47a97bfb37359f50a9c362c7b304a4b4ad1b3f5369ecb6f7fd8'
        wallet = Ed25519Account(private_key)
        public_key = wallet.public_key
        deadline = self.create_deadline()
        spender = "some_spender"
        value = 100
        msg = self.construct_permit_msg(public_key, spender, value, deadline)
        signature = wallet.sign_msg(msg + "tampered")
        # WHEN the permit is attempted
        # THEN it should fail due to invalid signature
        with self.assertRaises(Exception) as context:
            self.currency.permit(owner=public_key, spender=spender, value=value, deadline=str(deadline), signature=signature)
        self.assertIn('Invalid signature', str(context.exception))

    def test_permit_double_spending(self):
        # GIVEN a permit setup with a double spending attempt
        private_key = 'ed30796abc4ab47a97bfb37359f50a9c362c7b304a4b4ad1b3f5369ecb6f7fd8'
        wallet = Ed25519Account(private_key)
        public_key = wallet.public_key
        deadline = self.create_deadline()
        spender = "some_spender"
        value = 100
        msg = self.construct_permit_msg(public_key, spender, value, deadline)
        signature = wallet.sign_msg(msg)
        self.currency.permit(owner=public_key, spender=spender, value=value, deadline=str(deadline), signature=signature)
        # WHEN the permit is used again
        # THEN it should fail due to double spending
        with self.assertRaises(Exception) as context:
            self.currency.permit(owner=public_key, spender=spender, value=value, deadline=str(deadline), signature=signature)
        self.assertIn('Permit can only be used once', str(context.exception))

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
        msg = self.construct_permit_msg(public_key, spender, initial_value, deadline)
        signature = wallet.sign_msg(msg)
        self.currency.permit(owner=public_key, spender=spender, value=initial_value, deadline=deadline, signature=signature)
        
        # Verify initial allowance
        initial_allowance = self.currency.balances[public_key, spender]
        self.assertEqual(initial_allowance, initial_value)
        
        # WHEN a new permit is granted
        msg = self.construct_permit_msg(public_key, spender, new_value, deadline)
        signature = wallet.sign_msg(msg)
        self.currency.permit(owner=public_key, spender=spender, value=new_value, deadline=deadline, signature=signature)
        
        # THEN the new allowance should overwrite the old one
        new_allowance = self.currency.balances[public_key, spender]
        self.assertEqual(new_allowance, new_value)

    def test_approve_overwrites_previous_allowance(self):
        # GIVEN an initial approval setup
        self.currency.approve(amount=500, to="eve", signer="sys")
        initial_allowance = self.currency.balances["sys", "eve"]
        self.assertEqual(initial_allowance, 500)
        
        # WHEN a new approval is made
        self.currency.approve(amount=200, to="eve", signer="sys")
        new_allowance = self.currency.balances["sys", "eve"]
        
        # THEN the new allowance should overwrite the old one
        self.assertEqual(new_allowance, 200)

if __name__ == "__main__":
    unittest.main()
