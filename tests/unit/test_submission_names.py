"""Public submission names must satisfy the contract runtime's name rules."""

import json
from pathlib import Path

import pytest
from admission_corpus import base_payload, node_admission, signed_tx
from contracting.names import assert_safe_contract_name

from xian.exceptions import TransactionException

CASES = json.loads((Path(__file__).parents[1] / "fixtures/submission_names.json").read_text())


def _submission(name) -> bytes:
    return signed_tx(
        base_payload(
            contract="submission",
            function="submit_contract",
            kwargs={"name": name, "code": "@export\ndef value():\n    return 1"},
        )
    )


@pytest.mark.parametrize("case", CASES, ids=lambda case: str(case["name"]))
def test_submission_name_matches_runtime(case):
    name = case["name"]
    if case["accepted"]:
        assert node_admission(_submission(name))["payload"]["kwargs"]["name"] == name
        assert assert_safe_contract_name(name) == name
    else:
        with pytest.raises(TransactionException, match="contract name is invalid"):
            node_admission(_submission(name))
        # Built-in names are valid in the runtime but reserved from public submission.
        if name != "currency":
            with pytest.raises(AssertionError):
                assert_safe_contract_name(name)

