import hashlib

from contracting.execution.executor import Executor
from contracting.stdlib.bridge.time import Datetime
from contracting.storage.encoder import convert_dict, safe_repr
from loguru import logger

from xian.parallel_planner import TransactionAccess
from xian.utils.block import is_compiled_key, nanoseconds_to_utc_datetime
from xian.utils.tx import format_dictionary, tx_hash_from_tx


class TxProcessor:
    def __init__(self, client, metering=False):
        self.client = client
        self.executor = Executor(
            driver=self.client.raw_driver, metering=metering
        )

    def process_tx(self, tx, enabled_fees=False, rewards_handler=None):
        self.client.raw_driver.clear_transaction_reads()
        environment = self.get_environment(tx=tx)

        stamp_cost = (
            self.client.get_var(
                contract="stamp_cost", variable="S", arguments=["value"]
            )
            or 1
        )

        try:
            # Execute the transaction
            output = self.execute_tx(
                transaction=tx,
                stamp_cost=stamp_cost,
                environment=environment,
                metering=enabled_fees,
            )
            if output is None:
                return {
                    "tx_result": None,
                    "stamp_rewards_amount": 0,
                    "stamp_rewards_contract": None,
                    "base_writes": {},
                    "reward_deltas": {},
                    "access": None,
                }

            # Process the result of the executor
            processed = self.process_tx_output(
                output=output,
                transaction=tx,
                stamp_cost=stamp_cost,
                rewards_handler=rewards_handler,
            )
            tx_result = processed["tx_result"]

            access = self.build_access_record(
                tx=tx,
                status_code=output["status_code"],
                reads=processed["reads"],
                base_writes=processed["base_writes"],
                reward_deltas=processed["reward_deltas"],
            )
            tx_result = self.prune_tx_result(tx_result)

            return {
                "tx_result": tx_result,
                "stamp_rewards_amount": output["stamps_used"],
                "stamp_rewards_contract": tx["payload"]["contract"],
                "base_writes": processed["base_writes"],
                "reward_deltas": processed["reward_deltas"],
                "access": access,
            }
        except Exception as e:
            logger.error(e)

            return {
                "tx_result": None,
                "stamp_rewards_amount": 0,
                "stamp_rewards_contract": None,
                "base_writes": {},
                "reward_deltas": {},
                "access": None,
            }
        finally:
            self.client.raw_driver.clear_transaction_reads()

    def execute_tx(
        self, transaction, stamp_cost, environment: dict = {}, metering=False
    ):
        # TODO better error handling of anything in here
        logger.debug("Executing transaction...")

        try:
            # Execute transaction
            return self.executor.execute(
                sender=transaction["payload"]["sender"],
                contract_name=transaction["payload"]["contract"],
                function_name=transaction["payload"]["function"],
                stamps=transaction["payload"]["stamps_supplied"],
                stamp_cost=stamp_cost,
                kwargs=convert_dict(transaction["payload"]["kwargs"]),
                environment=environment,
                auto_commit=False,
                metering=metering,
            )
        except (TypeError, ValueError) as err:
            import traceback

            traceback.print_exc()
            logger.error(err)
            logger.debug(
                {
                    "transaction": transaction,
                    "sender": transaction["payload"]["sender"],
                    "contract_name": transaction["payload"]["contract"],
                    "function_name": transaction["payload"]["function"],
                    "stamps": transaction["payload"]["stamps_supplied"],
                    "stamp_cost": stamp_cost,
                    "kwargs": convert_dict(transaction["payload"]["kwargs"]),
                    "environment": environment,
                    "auto_commit": False,
                }
            )
            return None

    def process_tx_output(
        self, output, transaction, stamp_cost, rewards_handler
    ):
        # self.executor.driver.pending_writes.clear()
        # Log out to the node logs if the tx fails
        logger.debug(f"status code = {output['status_code']}")

        if output["status_code"] > 0:
            logger.error(
                f"TX executed unsuccessfully. "
                f"{output['stamps_used']} stamps used. "
                f"{len(output['writes'])} writes. "
                f"Result = {output['result']}"
            )

        tx_hash = tx_hash_from_tx(transaction)

        rewards = None
        reward_deltas = {}
        if output["status_code"] == 0 and rewards_handler is not None:
            rewards, reward_deltas = rewards_handler.build_tx_reward_outputs(
                total_stamps_to_split=output["stamps_used"],
                contract=transaction["payload"]["contract"],
            )

        base_writes = self.determine_writes_from_output(
            status_code=output["status_code"],
            ouput_writes=output["writes"],
            stamps_used=output["stamps_used"],
            stamp_cost=stamp_cost,
            tx_sender=transaction["payload"]["sender"],
        )
        writes = self.materialize_writes(base_writes, reward_deltas)
        reads = frozenset(self.client.raw_driver.transaction_reads.keys())

        for write in writes:
            self.client.raw_driver.set(key=write["key"], value=write["value"])

        tx_output = {
            "hash": tx_hash,
            "transaction": transaction,
            "status": output["status_code"],
            "state": writes,
            "events": output["events"],
            "stamps_used": output["stamps_used"],
            "result": safe_repr(output["result"]),
            "rewards": rewards if rewards else None,
        }

        tx_output = format_dictionary(tx_output)

        return {
            "tx_result": tx_output,
            "reads": reads,
            "base_writes": base_writes,
            "reward_deltas": reward_deltas,
        }

    def apply_tx_result(self, tx_result: dict) -> None:
        for write in tx_result["state"]:
            self.client.raw_driver.set(key=write["key"], value=write["value"])

    def determine_writes_from_output(
        self,
        status_code,
        ouput_writes,
        stamps_used,
        stamp_cost,
        tx_sender,
    ):
        # Only apply the writes if the tx passes
        if status_code == 0:
            return dict(ouput_writes)
        else:
            sender_balance = self.executor.driver.get_var(
                contract="currency",
                variable="balances",
                arguments=[tx_sender],
                mark=False,
            )

            # Calculate only stamp deductions
            to_deduct = stamps_used / stamp_cost
            new_bal = 0
            try:
                new_bal = sender_balance - to_deduct
                assert new_bal > 0
            except TypeError:
                pass
            except AssertionError:
                new_bal = 0

            return {f"currency.balances:{tx_sender}": new_bal}

    def materialize_writes(self, base_writes, reward_deltas):
        writes_map = dict(base_writes)

        for key, delta in reward_deltas.items():
            if key in writes_map:
                writes_map[key] += delta
                continue

            current_value = self.client.raw_driver.get(key, save=False)
            if current_value is None:
                current_value = 0
            writes_map[key] = current_value + delta

        writes = [{"key": k, "value": v} for k, v in writes_map.items()]
        try:
            writes.sort(key=lambda x: x["key"])
        except Exception as e:
            logger.error(f"Unable to sort state writes by 'key': {e}")

        return writes

    def get_environment(self, tx):
        block_meta = tx["b_meta"]
        nanos = block_meta["nanos"]
        signature = tx["metadata"]["signature"]
        chain_id = block_meta["chain_id"]

        # Nanos is set at the time of block being processed, and is shared between all txns in a block.
        # TODO : confirm this w/ CometBFT docs.
        # it's a deterministic value which is the average of times from validators who voted for this block
        # it's set during the consensus agreement & voting for block between all validators.

        return {
            "block_hash": block_meta["hash"],  # hash nanos
            "block_num": block_meta["height"],  # block number
            "__input_hash": self.get_timestamp_hash_from_tx(nanos, signature),
            "now": self.get_now_from_nanos(nanos=nanos),
            "AUXILIARY_SALT": signature,
            "chain_id": chain_id,
        }

    def get_timestamp_hash_from_tx(self, nanos, signature):
        h = hashlib.sha3_256()
        h.update("{}".format(str(nanos) + signature).encode())
        return h.hexdigest()

    def get_now_from_nanos(self, nanos):
        block_time = nanoseconds_to_utc_datetime(nanos)
        return Datetime._from_datetime(block_time)

    def prune_tx_result(self, tx_result: dict):
        # remove compiled code in the case of a contract submission
        tx_result["state"] = [
            entry
            for entry in tx_result["state"]
            if not is_compiled_key(entry["key"])
        ]
        # remove original sent transaction
        tx_result.pop("transaction")
        return tx_result

    def build_access_record(
        self,
        tx: dict,
        status_code: int,
        reads: frozenset[str],
        base_writes: dict,
        reward_deltas: dict,
    ) -> TransactionAccess:
        return TransactionAccess(
            index=-1,
            sender=tx["payload"]["sender"],
            nonce=tx["payload"].get("nonce", 0),
            reads=reads,
            writes=frozenset(base_writes.keys()),
            additive_writes=frozenset(reward_deltas.keys()),
            status=status_code,
        )
