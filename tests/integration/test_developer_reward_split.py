"""Developer rewards follow the chi each called contract actually used.

A transaction that calls into another contract pays both developers, in
proportion to the per-contract costs the VM reports. Without those costs
the whole developer share would go to the entry contract's developer.
"""

from fee_probe_support import make_processor, process, tx

LEAF = """@export
def work(n: int):
    total = 0
    for i in range(n):
        total += i
    return total
"""

ROOT_CONTRACT = """import con_leaf
@export
def run(n: int):
    return con_leaf.work(n=n)
"""


def _developer_rewards(tmp_path, n):
    processor = make_processor(tmp_path)
    driver = processor.client.raw_driver
    try:
        driver.set_contract_from_source("con_leaf", LEAF, developer="leaf_dev")
        driver.set_contract_from_source("con_root", ROOT_CONTRACT, developer="root_dev")
        driver.set("rewards.S:value", [0, 0, 0, 1])
        driver.commit()

        response = process(
            processor,
            tx(contract="con_root", function="run", kwargs={"n": n}, budget=5000),
        )
        result = response["tx_result"]
        assert result["status"] == 0, result
        return result["rewards"]["developer_reward"]
    finally:
        driver.close()


def test_called_contract_developer_shares_the_developer_reward(tmp_path):
    paid = _developer_rewards(tmp_path, 200)

    assert set(paid) == {"leaf_dev", "root_dev"}
    # The loop runs in con_leaf, so its developer earns the larger share.
    assert paid["leaf_dev"] > paid["root_dev"] > 0
