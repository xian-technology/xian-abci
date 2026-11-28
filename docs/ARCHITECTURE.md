# Architecture

`xian-abci` is the deterministic node runtime that sits between CometBFT and
the contract engine.

Main areas:

- `src/xian/xian_abci.py`: runtime assembly, startup wiring, and ABCI dispatch
- `src/xian/methods/`: ABCI request handlers and query surfaces
- `src/xian/processor.py`, `execution_engine.py`, and `execution_policy.py`:
  transaction execution flow; every transaction runs in Rust. Blocks run
  serially in the `BlockExecutor` (`processor.process_block`, which turns each
  outcome into its receipt; metering, fees and fee rewards follow the chain's
  fee policy), and so do the unmetered system transactions (epoch rebalance,
  evidence penalties: `processor.process_system_tx`); readonly simulations,
  contract-view queries and genesis contract deploys run one call with the
  same Rust code (`xian_vm_core.execute_tx` over a fork of the pending state,
  or the genesis driver's own). There is no Python per-transaction path.
  `execution_engine.py` keeps the startup checks that the installed vm-core
  and compiler implement the node's execution and admission revisions
  (`build_vm_runtime`) and the VM context of an external call
  (`vm_call_context`). The chain chi rate rule (`validated_chi_rate`) and the
  stored bytes a state root hashes (`stored_value_bytes`) come from vm-core.
- `src/xian/services/`: BDS and offline application snapshot services
- `src/xian/dashboard/`, `metrics.py` and `simulator.py`: optional
  node-adjacent surfaces
- `src/xian/utils/state_patches.py` and `src/xian/tools/state_patches/`:
  governed state-patch loading and execution
- `src/xian/node_setup.py` and `src/xian/node_admin.py`: reusable home/config
  bootstrap helpers and archive restore flows
- `src/xian/cli/`: backend-oriented command entrypoints
- `tests/`: unit, integration, governance, and system coverage

Dependency direction:

- consumes `xian-contracting` for execution semantics
- consumes `xian-runtime-types` for shared deterministic values
- consumes `xian-configs` for canonical contract bundles and network assets
- is consumed by `xian-cli` and `xian-stack`

## Account authorization

Xian keeps account IDs at 64 lowercase hexadecimal characters. Balances,
contract ownership, allowances, fees, signer context and nonces continue to use
that ID. Each registered account has two signing authorities, active and
recovery. Each authority needs both its Ed25519 and ML-DSA-65 private keys.
The recovery authority can replace lost active keys. A transition sets a new
epoch and replaces both authorities. The old keys cannot spend or recover.

The committed key `_accounts.policy` selects the account rules.

| State | Admission rules |
| --- | --- |
| Absent | Ordinary legacy transactions from unregistered senders. Any existing registry entry blocks legacy authorization. |
| `{"version":1,"enrollment_open":true,"transition_chi":1000}` | A legacy owner can enroll independently generated active and recovery keys. Closing enrollment prevents new Ed25519-authorized migrations. Registered users keep hybrid spending and recovery. |
| `{"version":2,"transition_chi":1000}` | Every sender requires hybrid authorization. Anyone can create an account by proving possession of both initial key pairs. ML-DSA-65 validators and disabled BN254 proofs are required for hardened genesis. |

The transition costs shown are examples in chi. A paid transition reserves its
transaction budget, charges the rooted transition cost, settles the fee under
the chain fee policy and produces an ordinary receipt. Each sender may send
ordinary transactions before a transition in a block. Successful admission of
that transition excludes the sender's remaining transactions until the next
block, including when execution later fails for lack of funds. Other senders
continue normally. CheckTx and recheck read the last committed record. Commit
stores record, balance, nonce and root together.

A fresh account's ID is the lowercase SHA3-256 digest of
`XIAN_ACCOUNT_ADDRESS_V1` followed by one zero byte and the four raw public
keys in active Ed25519, active ML-DSA-65, recovery Ed25519, recovery ML-DSA-65
order. A migration keeps the existing ID. In the strict profile an ordinary
spend consists of the five bytes `58 49 41 4e 01`, the 3,401-byte hybrid
signature frame and the canonical V2 payload in UTF-8. The node computes the
CometBFT transaction ID over those original bytes. Version 1 spends and all
registry transitions retain the signed JSON envelope in lowercase ASCII hex.
Each policy accepts exactly one wire representation for an ordinary spend.

New deployments provide public records to the genesis builder. Existing-chain
operators must perform any migration while Ed25519 still authenticates their
legacy owners. The native runtime and compiler revisions are pinned by genesis.
A transition from a previous runtime or from migration policy to strict policy
requires a coordinated network upgrade with an independently verified state
checkpoint, custody records and an audit of deployed contract authorizers.
A governance patch can close version 1 enrollment. It cannot install version 2,
reopen classical authorization on version 2, enable BN254 there, or rewrite
registry records. An upgrader must account for unregistered balances, account
and reward authorities, and contracts whose own business logic accepts external
signatures before choosing the new state.
