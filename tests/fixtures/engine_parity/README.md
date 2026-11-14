# Engine parity golden fixtures

These files freeze the outputs of the engine parity corpora
(`tests/integration/engine_parity_corpus.py`, contract sources in
`tests/integration/engine_parity_contracts.py`). The single-engine plan
(`xian-meta/docs/XIAN_SINGLE_ENGINE_PLAN.md`) moved these rules into the Rust
executor and then deletes the Python path; the fixtures are what proves that
nothing changed on the way except the approved changes listed below.

`tests/integration/test_engine_parity_corpus.py` checks that the production
path reproduces them byte for byte, running every block transaction in Rust,
and that the Python path reproduces them up to the first case revision 14
decides by a rule the Python path does not have (`REVISION_14_CASES`).

## Files

| File | Content |
|------|---------|
| `blocks_<corpus>.json` | On the production path, per block: each transaction's receipt as `ExecTxResult.data` carries it (writes, chi, rewards, reward records, events, result) and its code; the number and SHA-256 of the block's stored writes; the app hash. A block whose processing or receipt encoding raises is recorded as `halt` with the exception type: the node stops there and the block never commits. |
| `simulations.json` | The `simulate_tx` query response value of each simulation. |
| `contract_views.json` | The query response value of each contract view (`_call_contract_view`); `error` when the query raises. |
| `system_blocks.json` | `FinalizeBlock` + `Commit` of blocks with evidence penalties and epoch rebalances: the system transactions' receipts, the serialized `ResponseFinalizeBlock` and the app hash. |
| `handback_inventory.json` | Not golden: what the production path still runs on the Python path. Every block and system transaction runs in Rust (their entries are empty); simulations and contract views are listed by entry point until increment 8 moves them. |
| `inputs/groth16_bn254_demo.json` | Input: the demo Groth16 key, proof and public input (from `xian-zk`). |

Every corpus runs with the default dependencies: the VM (`xian-tech-vm-core`)
links the zk verifier.

## When the golden fixtures may be regenerated

Only for an explicit, approved consensus change (a new or folded execution
revision, such as increment 7 of the plan defining rules for the edge cases
recorded here), together with the change that causes it, and with the
diff reviewed case by case. Never to make a failing parity test pass during
a refactor or a port: a difference there is a bug in the port.

Regenerate (block corpora on the production path, the rest on the Python
path):

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

- Explicit rules for the Python path's halts and quirks (single-engine plan,
  increment 7; findings 1-9). Every block transaction now gets a receipt in
  Rust; only a host fault halts. Changed in the goldens (each a
  `REVISION_14_CASES` entry), and nothing else but the cascades named here:
  - A sender balance that is not a number (str, dict, list, bool): the
    transaction fails with `AssertionError('Sender balance is not a
    number.')`, chi 5, nothing charged, no balance write, no rewards (the
    Python path halted: `InvalidOperation`, `TypeError`, `ValueError`).
    `balance/{erin,fay,gus,hal}_*`, `deploy/str_balance_sender`.
  - A failure charge never raises a negative balance: the balance stays as
    it is and pays nothing (the Python path wrote 0, minting the
    difference). `balance/{ivy,jon}_*`: the balance write is gone; later
    blocks of the corpus differ only in their app hash.
  - Rewards are paid in full or not at all: an invalid reward input (a
    split that is not a list of four ints/decimals, a foundation owner,
    validator list, validator key, reward key or developer that is not a
    string, a power or other term that is not a number, a recipient whose
    balance is not a number or whose balance key LMDB cannot store, an
    amount the decimal context cannot represent) means no rewards for that
    transaction; its fee is charged and burned, as the burn share is. The
    transaction itself is unaffected, so the governance transaction that
    repairs the configuration still succeeds. `rewards/{split_text,
    split_short, split_not_list, split_dict, split_member_none,
    foundation_owner_int, power_text, active_dict, active_int_member,
    active_text_balance}_*` (the Python path paid with coerced inputs,
    failed every paid transaction in the host runtime, or halted on a
    non-string key). The later cases of the rewards corpus differ only in
    the cascaded `con_probe` counter (now incremented by the transactions
    that succeed), `currency.balances` values and app hashes; their reward
    payouts are unchanged.
  - Not in a golden corpus (covered by
    `tests/integration/test_revision_14_rules.py`, the differential tests
    and vm-core's unit tests): an owner that is not a string is no caller
    (`Exception('Caller is not the owner!')`, as an empty owner); a persisted
    IR that is not a valid xian_vm_v2 module fails as `contract '<name>' has
    invalid persisted VM IR` whatever its defect; a budget beyond the raw
    chi range fails in the host runtime; a balance that execution leaves as
    no number fails the transaction (effects dropped, fee from the balance
    before); a submission `name` that is not text is no submission name
    (the deployment refuses it); a deployment's contract name that is not a
    string is refused as an empty one, its constructor arguments reach the
    constructor as the VM values they are (a dict with string keys), its
    actors must be strings, and a constructor exception with arguments
    that are not plain values fails with the host failure alone; a
    contract metadata syscall names a contract by its name or handle;
    `code_hash` of code that is not text is `None`; random calls that do
    not bind to their parameters or take a number that is not an int fail
    with a `TypeError` of their own; zk calls of another shape or argument
    type fail with a `TypeError`; runtime-feature text with non-ASCII
    characters is refused; an unknown host syscall fails as
    `unsupported host syscall`.
  - zk off (finding 9): every zk syscall fails while the chain's zk
    runtime feature is off, however the contract was reached (a dynamic
    import too): `zk.<function> is unavailable: the chain zk runtime
    feature is disabled`. No golden case reached it.
  - The base chi counts against `chi_supplied` (finding 7): a V2
    transaction runs out of chi when its work and the 5 base chi exceed the
    supply (`raw / 1000 + 5 > chi_supplied`), so `chi_used` never exceeds it
    and a transaction can no longer supply up to 4 chi less than it uses. No
    golden case sat in that window. The simulator sizes the real envelope
    (the client's nonce, else the sender's next committed one, and its own
    estimate as `chi_supplied`, re-estimated until the digit count holds):
    supplying the estimate succeeds, one chi less runs out
    (`tests/integration/test_exact_chi_supply.py`).
  - The receipt encoding check of raw decimals, decimal tuples, floats and
    heap references is gone on both paths: V2 contracts cannot produce them
    in their effects (the VM boundary refuses them first;
    `test_values_the_receipt_encoding_refuses_never_reach_the_effects`).
  - Unchanged, but now explicit: a chain chi rate that is not a positive
    integer is a host fault (the node halts, as admission refuses), never a
    guessed price.

## The hand-back inventory

`handback_inventory.json` shrank as rules moved into Rust; since increment
7 no block or system transaction is handed back (the hand-back mechanism is
gone from the executor). The simulations and contract views leave it with
increment 8 (rewrite it with `--write-inventory`, then check that the diff
only removes entries). After the Python path is deleted, only the golden
comparison of the production path remains.
