# Time Semantics

Xian contract time is chain time, not wall-clock time.

- `now` inside a contract is derived from the finalized CometBFT block time.
- Every transaction in the same block observes the same `now`.
- `now` is deterministic because it comes from the block header agreed by
  consensus.
- `now` is expressed in UTC and carried into contracts through the `Datetime`
  bridge type.

## Block Policies

Xian supports three deterministic block-production policies:

- `on_demand`: `create_empty_blocks = false`,
  `create_empty_blocks_interval = "0s"`
- `idle_interval`: `create_empty_blocks = false` and a positive
  `create_empty_blocks_interval`
- `periodic`: `create_empty_blocks = true` and a positive
  `create_empty_blocks_interval`

The only semantic difference is when chain time advances during otherwise idle
periods.

The empty-block interval is not an exact finalized block-time target. CometBFT
still applies normal consensus timing, including `timeout_commit`, and Xian must
finish block execution before the next height can finalize. For example, a local
chain with `timeout_commit = "1s"` and
`create_empty_blocks_interval = "1s"` should be expected to finalize idle blocks
at roughly a two-second cadence rather than exactly once per second.

Implications:

- under `on_demand`, contract time does not advance while no blocks commit
- time-based state changes do not happen "in the background"
- deadlines and expirations are enforced when a transaction is executed in a
  later block

This is the correct tradeoff for a deterministic chain that does not want
periodic empty blocks unless operators choose that policy explicitly.

## Evidence Retention And Empty Blocks

The authenticated `__evidence_policy` schema2 stores a retention schedule:
`base_max_age_duration`, `growth_start_nanos`, monotonic
`target_max_age_duration`, and configured base block-age/byte limits. At finalized
consensus time `t`, effective duration is
`min(target, base + t - growth_start)`. The timestamp and schedule use exact
nanoseconds; the anchor remains part of the consensus application root.

Only raising the target rewrites this schedule. The old authorized duration is
first evaluated at the current consensus timestamp, capped by its OLD target;
that value becomes the new base and the current timestamp the new anchor.
Stable or previously saturated periods cannot bank time toward a future
increase. Reducing the unbonding setting does not reduce the target or abandon
outstanding claims.

The rooted record also stores `security_checkpoint_height`,
`security_checkpoint_nanos` and nullable `previous_security_checkpoint_height`.
Let the current checkpoint be `(Hc,Tc)` and the previous floor be `Hp`.
After target increases, rotate once when `t - Tc >= target`: retain old `Hc`
as `Hp`, and set the current checkpoint to actual `(H,t)`. Zero is a valid
previous floor. At height H the effective block limit is
`max(configured_base, H - (Hp if present else Hc))`. The retained floor only
moves forward; a long idle jump cannot retire multiple synthetic epochs or
revive already expired history. This protects fresh faults whose BFT header
time is stale, even when the unbond request arrives at a later height.

Empty blocks leave the rooted record unchanged within a security epoch, even
during pending duration growth. One sparse epoch transition earns a proof
block; its successor can settle under all configured idle policies. Comet still
creates initial proof blocks and proof blocks following actual state changes;
this is not a guarantee of an exact block interval. The node returns a full idempotent evidence-only consensus
parameter update on each finalized block, including after effective saturation.
This adds fixed per-block parameter metadata/store work, without app-hash clock
churn. No previous local marker decides whether an update is emitted.

The derived time is consensus time, never local wall time. Under Comet0.40 BFT
Time the first block after a long quiet period may use the previous commit's
time; neither the configured interval nor this schedule guarantees wall-clock
freshness. Backwards/noncanonical finalized timestamps are rejected before
execution. Comet also validates header time upstream.

Comet's consensus-parameter hash covers block limits, not the evidence group.
Evidence-limit authenticity comes from the rooted schedule evaluated at the
verified finalized checkpoint height and timestamp, with native state/header consistency
checked during full-store restoration. `/evidence_policy` reports the actual
committed marker height/time and derived effective limits, not the older
schedule anchor. Schema1 records are rejected; these transition rules are for
the fresh chain, not replay of an older chain with changed node code.

## Contract Design Guidance

Contracts should treat time as a condition checked at execution time, not as a
scheduler.

Good patterns:

- `assert now < deadline`
- `assert now >= unlock_time`
- computing accrued value from `(now - start_time)` when a user interacts

Bad assumptions:

- expecting callbacks or autonomous execution at a precise wall-clock moment
- assuming time progresses between blocks

## Precision

Xian preserves CometBFT block timestamp precision up to Python `datetime`
microseconds. Consensus/evidence bookkeeping separately preserves exact
nanoseconds. Sub-microsecond contract nanoseconds are truncated because Python's
`datetime` does not support nanosecond storage.

## Genesis

Genesis builders accept an optional timezone-aware ISO `genesis_time`. The
combined bundle, local-network and single-validator builders normalize it to
UTC and use that same instant for the CometBFT header, submission metadata and
every contract constructor's `now`. Consensus genesis nanoseconds are preserved;
constructor `Datetime` observes that instant at microsecond precision.

When no time is supplied, the builder samples the current UTC time once for
the entire genesis. For reproducible genesis bytes and application hashes,
supply an explicit time and keep the other inputs and compiler/runtime versions
identical. Naive timestamps without a timezone are rejected.

## Simulation

Transaction simulation uses the latest known chain time when block metadata is
available. If no committed block time exists yet, simulation falls back to the
Unix epoch.

This keeps simulation behavior aligned with real execution as closely as
possible while still giving uninitialized local environments a deterministic
starting point.

## Recovery Checkpoint Trust

Positive-height application exports must include explicit exact checkpoint
`nanos`. Offline import treats its metadata as trusted input; the state root
alone does not authenticate the export's height/time. A recovery genesis now
requires `trusted_checkpoint=` independently obtained from an existing
schema1 `/evidence_policy` response or native-inspector output. Its canonical
`height`, `app_hash` and `last_block_nanos` must exactly match the export before
any growth is materialized. The backend command requires
`--trusted-checkpoint <file>`; do not synthesize it from the export itself.

Recovery materializes the old schedule at that independently verified export
checkpoint, then reanchors the remaining schedule to the agreed new genesis
time. The halted interval earns no growth. Normal full-store restore and replay
preserve the original schedule/anchor and use the real committed native time.

## Successor Staking Clocks

A positive-period staking challenge clock is anchored in a strictly later block
than the request or exit. The node invokes the coordinator's reserved
`process_time_anchors(max_work=32)` once before paid execution, after evidence
and liveness. Same-height jobs are skipped; pending slash jobs do not block this
timing-only work. Persistent FIFO state resumes across restart. A large queue
may cause a finite proof-block burst while batches of 32 jobs change actual stake
state; the empty path stops invoking the driver after the queue drains.

The hook is only one part of challenge safety: evidence retention must also
protect fresh faults whose first post-idle header timestamp is old. An anchor
alone is not proof that the corresponding evidence will remain valid.

Manual recovery creates a NEW genesis/history boundary: it materializes the
verified export duration, resets the security checkpoint to export height/new
genesis time with no previous floor, and retains the configured base block
limit. An application export cannot restore old native evidence headers or
validator history; manual recovery does not promise historical slashability.
Verified full-store restore instead preserves both rooted epochs and native
history exactly. Two checkpoints bound record size, not physical retention to
two wall-clock periods across arbitrarily long idle gaps. Upstream Comet
controls native proof-metadata pruning using the effective full limits triple.
