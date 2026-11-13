"""What a contract sees in ctx for an ordinary transaction."""

from fee_probe_support import make_processor, process, tx

CONTEXT = """@export
def seen(name: str):
    return [ctx.submission_name, ctx.caller, ctx.signer, ctx.this]
"""


def test_a_name_argument_does_not_become_the_submission_name(tmp_path):
    processor, rewards = make_processor(tmp_path)
    driver = processor.client.raw_driver
    try:
        driver.set_contract_from_source("con_context", CONTEXT)
        driver.commit()

        response = process(
            processor,
            rewards,
            tx(contract="con_context", function="seen", kwargs={"name": "con_spoof"}),
        )

        result = response["tx_result"]
        assert result["status"] == 0, result
        assert result["result"] == [None, "alice", "alice", "con_context"]
    finally:
        driver.close()
