"""Public submission names must satisfy the contract runtime's name rules."""

import json
from pathlib import Path

import pytest
from contracting.names import assert_safe_contract_name

from xian.exceptions import TransactionException
from xian.utils.tx import check_contract_name

CASES = json.loads((Path(__file__).parents[1] / "fixtures/submission_names.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda case: str(case["name"]))
def test_submission_name_matches_runtime(case):
    name = case["name"]
    if case["accepted"]:
        check_contract_name("submission", "submit_contract", name)
        assert assert_safe_contract_name(name) == name
    else:
        with pytest.raises(TransactionException, match="contract name is invalid"):
            check_contract_name("submission", "submit_contract", name)
        # Built-in names are valid in the runtime but reserved from public submission.
        if name != "currency":
            with pytest.raises(AssertionError):
                assert_safe_contract_name(name)
