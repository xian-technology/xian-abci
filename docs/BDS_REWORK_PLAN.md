# BDS Rework Plan

## Status

Completed on `main` in the current rewrite:

- runtime/env-driven BDS config replaced the old checked-in `config.json`
- the old ad hoc batch flow was removed from the active BDS path
- BDS now ingests one block at a time with one explicit database transaction
- the schema now has first-class `blocks`
- `state_changes` now carries chain-order and lineage metadata
- current state is stored separately as a projection in `state`
- the old `CustomEncoder` path was replaced by canonical runtime-based
  serialization
- state patch queries now read from BDS instead of the JSON file directly
- first-class BDS-backed ABCI query endpoints now exist for blocks,
  transactions, events, contracts, and state history
- `xian-docs-web` now documents the read-surface split:
  raw ABCI for current state, BDS-backed ABCI for indexed/history reads,
  GraphQL only as an optional convenience layer
- BDS now uses a canonical `BdsBlockPayload`
- BDS writes now go through a dedicated sequential in-process worker instead
  of waiting on direct Postgres writes in `FinalizeBlock`
- graceful node shutdown now flushes the BDS queue
- BDS now buffers live finalized blocks in memory and persists them in strict
  contiguous block order
- BDS now auto-detects height gaps and catches up from local CometBFT RPC in
  the background while newer live blocks continue to arrive
- BDS can therefore receive block `N+2`, keep it pending, fetch `N+1`
  locally, then persist `N+1` followed by `N+2`
- the local spool remains available for offline maintenance, snapshot import,
  and explicit recovery flows, but it is no longer the primary hot-path
  durability mechanism
- BDS now exposes operator-facing status/spool inspection queries for queue
  depth, indexed head, lag, and pending spooled blocks
- BDS now has a local `xian-bds-reindex` path for full historical backfill
  from CometBFT RPC when the node still retains block history
- BDS status now reports spool byte totals, filesystem capacity/free space, and
  warning/error alerts for spool growth and low disk space
- BDS now supports snapshot export/import for fast bootstrap and recovery
- BDS now has explicit operator commands to compact stale spool entries and
  drain the local spool into Postgres safely when the node is offline
- BDS query paths now include:
  - `/state`
  - `/state_history`
  - `/state_for_tx`
  - `/state_for_block`
  - `/state_patches`
  - `/state_patches_for_block`
  - `/state_patch/<hash>`
  - `/state_changes_for_patch/<hash>`

Still worth doing next:

- decide later whether that worker should stay in-process or become a separate
  replayable external indexer
- benchmark and tune large-table index strategy on realistic chain data
- decide whether live buffering should gain an optional on-disk write-ahead
  mode for operators who prefer durability over the lowest possible hot-path
  latency
- decide whether startup should block on spool catch-up or continue with
  eventual consistency
- extend reindex/backfill to work against explicit archival RPC sources as a
  first-class documented operator flow
- add richer storage policy guidance for Postgres maintenance, log retention,
  and long-term archival sizing

## Goal

Rework the Blockchain Data Service (BDS) so it behaves like a real chain data
indexer instead of an opportunistic side store.

The target shape is:

- simple to operate
- explicit and configurable
- fast enough to keep up with live nodes
- easy to query for explorers, dashboards, analytics, and audits
- able to answer state-history questions precisely
- clearly non-consensus-critical

Backward compatibility is not required for this rework. The goal is a cleaner
and stronger design, not preserving the current schema or payload shape.

## What BDS Should Be

BDS should be an optional indexing service that consumes finalized chain data
and stores it in a relational/queryable form.

It should not:

- influence consensus
- block ABCI execution on avoidable work
- invent its own serialization rules that drift from chain/runtime semantics
- own secrets via checked-in repo files

It should:

- store enough metadata to reconstruct what happened in a block
- preserve an append-only history of state changes
- keep a fast current-state projection
- expose contract events, rewards, contracts, and blocks cleanly
- support common blockchain queries with the right indexes

## Confirmed Problems In The Current Implementation

### 1. Configuration Is Hardcoded In The Repo

Current behavior:

- `BDS.init()` constructs `DB(Config("config.json"))`
- `src/xian/services/bds/config.json` contains live connection details
- `docker-compose-abci-bds.yml` also hardcodes database credentials

Why this is wrong:

- secrets/config do not belong in a checked-in JSON file inside the service
- runtime configuration should come from node config and/or environment
- the current shape makes local dev easy, but it is not a professional service
  contract

### 2. The Database Layer Is Too Weak

Current behavior in `src/xian/services/bds/database.py`:

- `DB.batch` is a class attribute instead of instance state
- batch commit executes statements one-by-one on a connection without an
  explicit transaction block
- mutable default arguments are used (`params: list = []`)
- database creation uses string interpolation
- pool creation has no tuning or explicit contract

Why this is wrong:

- batch ownership is unclear
- a block-level BDS write is not modeled as one explicit unit of work
- error handling and atomicity are weaker than they should be
- the implementation does not communicate which guarantees BDS actually has

### 3. The Current Schema Is Too Thin For A Blockchain Indexer

Current tables:

- `transactions`
- `state`
- `state_changes`
- `events`
- `rewards`
- `addresses`
- `contracts`
- `state_patches`

The biggest schema issue is `state_changes`.

Current `state_changes` columns:

- `id`
- `tx_hash`
- `key`
- `value`
- `value_numeric`
- `created`

What is missing:

- `block_height`
- `block_hash`
- `block_time`
- `tx_index`
- `write_index`
- a deterministic ordering key inside the block
- previous-link metadata for the same key
- an explicit relation back to the exact block record

As a result, BDS can show that a key changed, but it cannot model state lineage
cleanly enough for:

- "show me every version of this key in chain order"
- "what was the previous value before this tx?"
- "which exact block/tx/write introduced this state version?"
- "reconstruct the state diff for this block without fuzzy joins"

### 4. The Query/Index Strategy Is Not Strong Enough

Current problems:

- `transactions` has almost no useful secondary indexes
- `events` lacks some obvious blockchain access indexes
- `state_history` currently relies on `created DESC`, which is weaker than
  chain-order metadata
- `state` reads use windowing over `state_changes` even though a dedicated
  current-state table already exists

Likely missing indexes:

- `transactions(block_height)`
- `transactions(block_hash)`
- `transactions(sender, nonce)`
- `transactions(contract, function)`
- `transactions(created)`
- `transactions(success, block_height)`
- `events(tx_hash)`
- `events(contract, event)`
- `events(created)`
- `state_changes(tx_hash)`
- `state_changes(block_height, tx_index, write_index)`
- `state_changes(key, block_height DESC, tx_index DESC, write_index DESC)`

### 5. `CustomEncoder` Is Too Ad Hoc And Too Lossy

Current behavior in `src/xian/services/bds/bds.py`:

- custom JSON encoding is separate from the canonical runtime encoding
- integers are stringified
- decimals are stringified
- time values are rewritten into ISO strings
- special cases like `{"__fixed__": ...}` and `{"__time__": ...}` are handled
  manually

Why this is wrong:

- BDS is inventing a separate storage representation instead of using a clearly
  defined canonical one
- numeric and time queryability is weakened by aggressive stringification
- the rules are difficult to reason about and easy to drift from runtime
  semantics

### 6. The BDS Integration Boundary In `FinalizeBlock` Is Weak

Current behavior:

- each tx schedules `asyncio.create_task(self.bds.add_to_batch(...))`
- block end schedules `asyncio.create_task(self.bds.commit_batch())`

Why this is not ideal:

- task creation is per tx
- batching is implicit and shared mutable state
- the ingestion unit is not modeled as "this block"
- ordering and visibility are harder to reason about
- BDS is still too entangled with the `FinalizeBlock` flow

### 7. Some BDS SQL Should Not Exist

Two concrete examples:

- `create_readonly_role()` hardcodes DB/user/password policy into app SQL
- `enforce_table_limits()` does not meaningfully enforce anything and should be
  removed

These are not the application’s job in their current form.

## Target Design

## Design Principles

- BDS remains optional.
- ABCI remains authoritative.
- BDS consumes finalized block results only.
- One block is one BDS ingestion unit.
- Current-state and historical-state storage are separate on purpose.
- The serializer used for BDS must be explicit and canonical.
- Configuration must come from node config / env, not a checked-in JSON file.

## Proposed Ingestion Model

Replace the current per-tx `create_task(...)` batch flow with a block-scoped
collector.

Target flow:

1. `FinalizeBlock` builds one in-memory `BdsBlockPayload`
2. It contains:
   - block metadata
   - ordered tx execution results
   - ordered state writes
   - ordered contract events
   - rewards
   - optional state patches
3. At block end, ABCI hands that block payload to BDS once
4. BDS writes the entire payload inside one explicit database transaction
5. If BDS fails, it logs and surfaces the failure operationally, but does not
   affect consensus

This gives us:

- explicit ordering
- simpler code
- fewer tasks
- cleaner atomicity
- a clearer place to benchmark and optimize

## Proposed Schema V2

### `blocks`

Add a real blocks table.

Suggested columns:

- `height BIGINT PRIMARY KEY`
- `block_hash TEXT NOT NULL UNIQUE`
- `block_time BIGINT NOT NULL`
- `block_time_iso TIMESTAMPTZ NOT NULL`
- `tx_count INTEGER NOT NULL`
- `app_hash TEXT NOT NULL`
- `created TIMESTAMPTZ NOT NULL`

Suggested indexes:

- unique on `block_hash`
- index on `block_time_iso`

Why:

- BDS currently has block metadata spread indirectly across tx rows and state
  patch rows
- blocks deserve first-class modeling

### `transactions`

Keep transactions, but make them more queryable and less blob-centric.

Suggested columns:

- `hash TEXT PRIMARY KEY`
- `block_height BIGINT NOT NULL REFERENCES blocks(height)`
- `tx_index INTEGER NOT NULL`
- `block_hash TEXT NOT NULL`
- `block_time BIGINT NOT NULL`
- `sender TEXT NOT NULL`
- `nonce BIGINT NOT NULL`
- `contract TEXT NOT NULL`
- `function TEXT NOT NULL`
- `success BOOLEAN NOT NULL`
- `status_code INTEGER NOT NULL`
- `chi_used BIGINT NOT NULL`
- `result JSONB`
- `payload JSONB NOT NULL`
- `raw_record JSONB`
- `created TIMESTAMPTZ NOT NULL`

Notes:

- `payload` should be stored directly, not only as part of a whole-record blob
- `raw_record` can exist for debugging, but should be optional and not required
  for normal queries

Suggested indexes:

- `(block_height, tx_index)`
- `(sender, nonce)`
- `(contract, function, block_height DESC)`
- `(success, block_height DESC)`
- `(created DESC)`

### `state_current`

Rename `state` conceptually to a current-state projection.

Suggested columns:

- `key TEXT PRIMARY KEY`
- `value JSONB`
- `value_numeric NUMERIC`
- `last_change_id BIGINT NOT NULL`
- `last_tx_hash TEXT NOT NULL`
- `last_block_height BIGINT NOT NULL`
- `updated_at TIMESTAMPTZ NOT NULL`

Why:

- current-state reads should not require historical reconstruction
- every current-state row should point to the latest change that produced it

### `state_changes`

This is the core of the redesign.

Suggested columns:

- `change_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY`
- `block_height BIGINT NOT NULL REFERENCES blocks(height)`
- `block_hash TEXT NOT NULL`
- `block_time BIGINT NOT NULL`
- `tx_hash TEXT`
- `tx_index INTEGER NOT NULL`
- `write_index INTEGER NOT NULL`
- `key TEXT NOT NULL`
- `new_value JSONB`
- `new_value_numeric NUMERIC`
- `previous_change_id BIGINT`
- `previous_tx_hash TEXT`
- `created_at TIMESTAMPTZ NOT NULL`
- optional `origin_type TEXT NOT NULL`
  - values like `transaction`, `genesis`, `state_patch`

Suggested indexes:

- `(key, block_height DESC, tx_index DESC, write_index DESC)`
- `(tx_hash, write_index)`
- `(block_height, tx_index, write_index)`
- `(previous_change_id)`

Why:

- this gives proper key lineage
- state history becomes cheap and explicit
- block diff reconstruction becomes direct

### `events`

Keep events, but strengthen structure and indexes.

Suggested columns:

- `id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY`
- `block_height BIGINT NOT NULL REFERENCES blocks(height)`
- `tx_hash TEXT NOT NULL`
- `tx_index INTEGER NOT NULL`
- `event_index INTEGER NOT NULL`
- `contract TEXT NOT NULL`
- `event TEXT NOT NULL`
- `signer TEXT NOT NULL`
- `caller TEXT NOT NULL`
- `data_indexed JSONB NOT NULL`
- `data JSONB NOT NULL`
- `created_at TIMESTAMPTZ NOT NULL`

Suggested indexes:

- `(tx_hash, event_index)`
- `(contract, event, block_height DESC)`
- `(created_at DESC)`
- `GIN(data_indexed)`

### `rewards`

Keep rewards, but tie them to block and tx ordering.

Suggested columns:

- `id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY`
- `block_height BIGINT NOT NULL REFERENCES blocks(height)`
- `tx_hash TEXT`
- `tx_index INTEGER NOT NULL`
- `reward_index INTEGER NOT NULL`
- `type TEXT NOT NULL`
- `recipient_key TEXT`
- `value NUMERIC NOT NULL`
- `created_at TIMESTAMPTZ NOT NULL`

### `contracts`

Keep contracts, but store clearer provenance.

Suggested columns:

- `name TEXT PRIMARY KEY`
- `last_tx_hash TEXT NOT NULL`
- `submitted_at_block BIGINT NOT NULL`
- `submitted_at TIMESTAMPTZ NOT NULL`
- `code TEXT NOT NULL`
- `xsc0001 BOOLEAN NOT NULL DEFAULT FALSE`

### `state_patches`

Keep state patches, but align them with the same block/state-change model.

Suggested columns:

- `hash TEXT PRIMARY KEY`
- `block_height BIGINT NOT NULL REFERENCES blocks(height)`
- `block_hash TEXT NOT NULL`
- `block_time BIGINT NOT NULL`
- `patch_count INTEGER NOT NULL`
- `patches JSONB NOT NULL`
- `created_at TIMESTAMPTZ NOT NULL`

### `addresses`

This table should be reevaluated.

Current behavior:

- it is a deduplicated discovery list of currency balance holders

Decision to make during implementation:

- either remove it and derive addresses from indexed state
- or keep it explicitly as a convenience projection with a clear purpose

Default recommendation:

- remove it unless a concrete query path proves it is necessary

## Serialization / Encoding Redesign

## Current Problem

`CustomEncoder` is custom, lossy, and separate from the runtime’s canonical
encoding rules.

## Target

BDS should use one explicit serializer module, for example:

- `xian.services.bds.serializer`

It should:

- build on `xian_runtime_types`
- preserve deterministic Xian runtime semantics
- preserve queryability intentionally
- stop stringifying everything blindly

Suggested rules:

- JSON-like structures remain JSON-like
- `ContractingDecimal` stores as JSON string plus optional numeric projection
  where useful
- big integers preserve exactness
- `Datetime` stores as ISO string in JSON and as structured block columns when
  promoted to first-class metadata
- bytes store as hex string

Important distinction:

- BDS storage encoding does not need to be byte-identical to storage encoding
- but it must be explicit, stable, and well documented

## Integration Redesign In `xian-abci`

### Current

- `FinalizeBlock` calls BDS per tx via `create_task`
- BDS accumulates SQL statements in a mutable batch
- block commit is implicit

### Target

Introduce a block-scoped sink:

- `BdsBlockCollector` inside ABCI
- `BdsWriter` inside the BDS service

Suggested responsibilities:

- `FinalizeBlock`:
  - build ordered canonical BDS payload
  - hand off once per block
- `BDS`:
  - validate payload shape
  - persist via one explicit DB transaction
  - expose read/query methods

This separates:

- chain execution
- BDS payload construction
- BDS persistence

## Configuration Redesign

Replace `src/xian/services/bds/config.json` completely.

Target sources:

- `config.toml` / `xian` section for node-level BDS enablement
- environment variables for secrets and container wiring

Suggested config keys:

- `xian.block_service_mode = true|false`
- `xian.bds_dsn`
- or
  - `xian.bds_host`
  - `xian.bds_port`
  - `xian.bds_database`
  - `xian.bds_user`
  - `xian.bds_password`

Optional operational keys:

- `xian.bds_pool_min_size`
- `xian.bds_pool_max_size`
- `xian.bds_statement_timeout_ms`
- `xian.bds_application_name`

In Docker / `xian-stack`:

- pass these via env
- remove hardcoded passwords from compose files
- stop creating readonly roles from app SQL

If a read-only PostGraphile role is still wanted:

- create it as part of explicit DB bootstrap/migration tooling
- configure it via env, not hardcoded SQL in app code

## Performance Priorities

### High-value changes

1. Move from per-tx task scheduling to per-block persistence
2. Write the full block in one SQL transaction
3. Add the missing indexes for blockchain query paths
4. Stop double-serializing entire tx records unnecessarily
5. Replace the custom encoder with a cheaper explicit serializer

### Medium-value changes

1. Bulk insert where appropriate
2. Tune asyncpg pool size and statement reuse
3. Avoid storing redundant blobs when structured columns already exist
4. Consider partitioning `state_changes`, `events`, and `transactions` by block
   range later if chain size justifies it

### Low-value or non-goals for now

- premature sharding
- over-abstracting BDS into a separate service repo
- trying to make BDS consensus-critical

## Security / Operational Rules

- BDS failures must never alter consensus state
- BDS credentials must not live in checked-in JSON
- BDS writes must be bounded and observable
- schema/bootstrap changes must be explicit and testable
- readonly/query access must be configured intentionally, not created from
  hardcoded app SQL

## Recommended Implementation Order

### Phase 1: Clean Operational Foundation

1. Remove `config.json`
2. Replace BDS config loading with config/env-driven settings
3. Make `DB.batch` instance-local
4. Use explicit database transactions for block writes
5. Remove `enforce_table_limits()`
6. Remove hardcoded readonly role creation from runtime SQL

This is the safest first slice and should happen before schema redesign.

### Phase 2: Integration Boundary Cleanup

1. Replace per-tx `create_task(...)` calls with block-scoped handoff
2. Define a `BdsBlockPayload` structure
3. Write one block at a time in one DB transaction
4. Add profiling around BDS write cost

### Phase 3: Schema V2

1. Add `blocks`
2. Redesign `transactions`
3. Replace `state` with `state_current`
4. Redesign `state_changes` with full lineage/order metadata
5. Redesign `events`
6. Reevaluate or remove `addresses`
7. Preserve `state_patches` but align it to the same model

### Phase 4: Serializer Cleanup

1. Delete `CustomEncoder`
2. Introduce a dedicated BDS serializer module
3. Base it on `xian_runtime_types`
4. Add tests for numeric, datetime, bytes, nested structures, and big ints

### Phase 5: Query Surface And Docs

1. Update BDS query helpers for the new schema
2. Update dashboard/PostGraphile expectations
3. Update `xian-stack` runtime wiring
4. Document the data model in `xian-docs-web`

## Acceptance Criteria

The rework is complete when:

- no checked-in BDS credentials/config file remains
- BDS ingest is block-scoped and explicitly transactional
- `state_changes` supports exact per-key lineage
- common explorer/dashboard queries are index-backed
- `CustomEncoder` is gone
- BDS schema and config are documented
- BDS integration adds minimal overhead to `FinalizeBlock`

## First Concrete Slice To Implement Next

If continuing immediately, start here:

1. remove `config.json`
2. replace BDS config with node config/env
3. fix `DB` batching/transaction semantics
4. remove `enforce_table_limits()`
5. remove runtime readonly-role creation

That is the best first slice because it is clearly correct, low risk, and
improves the operational shape before touching the deeper schema.
