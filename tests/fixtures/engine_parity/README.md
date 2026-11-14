# Engine parity golden fixtures

These files freeze what the Python per-transaction path produces for the
engine parity corpora (`tests/integration/engine_parity_corpus.py`, contract
sources in `tests/integration/engine_parity_contracts.py`). The single-engine
plan (`xian-meta/docs/XIAN_SINGLE_ENGINE_PLAN.md`) moves these rules into the
Rust executor and then deletes the Python path; the fixtures are what proves
that nothing changed on the way.

`tests/integration/test_engine_parity_corpus.py` checks that the Python path
and the production path both reproduce them byte for byte.

## Files

| File | Content |
|------|---------|
| `blocks_<corpus>.json` | Per block: each transaction's receipt as `ExecTxResult.data` carries it (writes, chi, rewards, reward records, events, result) and its code; the number and SHA-256 of the block's stored writes; the app hash. A block whose processing or receipt encoding raises is recorded as `halt` with the exception type: the node stops there and the block never commits. |
| `simulations.json` | The `simulate_tx` query response value of each simulation. |
| `contract_views.json` | The query response value of each contract view (`_call_contract_view`); `error` when the query raises. |
| `system_blocks.json` | `FinalizeBlock` + `Commit` of blocks with evidence penalties and epoch rebalances: the system transactions' receipts, the serialized `ResponseFinalizeBlock` and the app hash. |
| `handback_inventory.json` | Not golden: the cases the production path still runs on the Python path, with the reason (`process_block` and system transaction hand-backs measured; simulation and contract views by entry point). |
| `inputs/groth16_bn254_demo.json` | Input: the demo Groth16 key, proof and public input (from `xian-zk`). |

The zk corpus needs the native verifier (`--extra zk`); without it the test
skips that corpus. All other corpora run with the default dependencies.

## When the golden fixtures may be regenerated

Only for an explicit, approved consensus change (a new or folded execution
revision, such as increment 7 of the plan defining rules for the edge cases
recorded here), together with the change that causes it, and with the
diff reviewed case by case. Never to make a failing parity test pass during
a refactor or a port: a difference there is a bug in the port.

Regenerate on the Python path (needs the zk extra):

    python -m tests.integration.engine_parity_corpus --write

Changing the corpus itself (new cases) also regenerates them; add cases
without changing the outputs of existing ones, and check that the diff only
adds.

## Approved changes since the corpora were frozen

Execution revision 14 (unreleased) folds in these consensus changes; the
fixtures were regenerated with them and the diff reviewed case by case:

- Random work bounds (single-engine plan, finding 8): `random.seed` accepts
  only a str, int (bool included) or `None` salt (decision 4); any other salt
  fails with `random.seed() salt must be a str, int or None`. Changed:
  `random/salt_decimal`, `salt_list`, `salt_dict` and `random/internal_salt_`
  `datetime`, `decimal`, `tuple`, `list`, `dict`, `timedelta` (now failures,
  with their failure chi). Every later transaction of that block differs only
  in the cascaded `currency.balances` values, and both block app hashes
  change. `getrandbits` above 512 bits, `choices` above the sequence limit
  and `choice`/`choices` of a population other than a list, tuple, str,
  bytes or bytearray now fail before any host work; no frozen case reached
  them (the differential tests and xian-contracting's
  `test_native_vm_work_bounds.py` cover them). `importlib.code_hash` now
  charges 4 raw chi per hashed byte; no frozen case calls it.

- Transaction admission revision 3 (`xian-fastpath-admission-3`, unreleased):
  admission refuses documents with keys beside `metadata` and `payload`. No
  corpus transaction had such keys; the genesis fixtures' `__execution_policy`
  entry names the new revision, so only `system_blocks.json` changes: every
  block's app hash (and the `ResponseFinalizeBlock` bytes that carry it). The
  system receipts are identical.

## The hand-back inventory

`handback_inventory.json` shrinks as rules move into Rust. An increment that
moves a rule removes its cases (rewrite it with `--write-inventory`, then
check that the diff only removes entries); the parity test fails while the
file and the production path disagree. After the Python path is deleted, the
inventory is empty and only the golden comparison of the production path
remains.
