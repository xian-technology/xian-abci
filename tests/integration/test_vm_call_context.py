"""What a contract sees in ctx for an ordinary transaction."""

from fee_probe_support import make_processor, process, tx

CONTEXT = """@export
def seen(name: str):
    return [ctx.submission_name, ctx.caller, ctx.signer, ctx.this]
"""


def test_a_name_argument_does_not_become_the_submission_name(tmp_path):
    processor = make_processor(tmp_path)
    driver = processor.client.raw_driver
    try:
        driver.set_contract_from_source("con_context", CONTEXT)
        driver.commit()

        response = process(
            processor,
            tx(contract="con_context", function="seen", kwargs={"name": "con_spoof"}),
        )

        result = response["tx_result"]
        assert result["status"] == 0, result
        assert result["result"] == [None, "alice", "alice", "con_context"]
    finally:
        driver.close()


def test_an_empty_stored_ir_is_treated_as_missing(tmp_path):
    processor = make_processor(tmp_path)
    driver = processor.client.raw_driver
    try:
        driver.set_contract_from_source("con_context", CONTEXT)
        driver.set("con_context.__xian_ir_v1__", "")
        driver.commit()

        response = process(
            processor,
            tx(contract="con_context", function="seen", kwargs={"name": "x"}),
        )

        result = response["tx_result"]
        assert result["status"] == 1
        assert "requires persisted __xian_ir_v1__" in result["result"]
    finally:
        driver.close()


SUBMISSION_CONTEXT = """@export
def seen(name: str):
    return ctx.submission_name
"""


def test_a_transaction_to_submission_carries_its_name_argument(tmp_path):
    # A transaction to submission gets its name argument as the submission
    # name.
    processor = make_processor(tmp_path)
    driver = processor.client.raw_driver
    try:
        driver.set_contract_from_source("submission", SUBMISSION_CONTEXT)
        driver.commit()
        transaction = tx(contract="submission", function="seen", kwargs={"name": "con_x"})

        response = process(processor, transaction)

        result = response["tx_result"]
        assert result["status"] == 0, result
        assert result["result"] == "con_x"
    finally:
        driver.close()
