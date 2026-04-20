# Backlog

This file tracks follow-up work that still looks relevant on current `main`.

## BDS Follow-Ups

- benchmark the current index strategy and query plans on realistic chain data
- decide whether startup should wait for spool catch-up or keep the current
  eventual-consistency model
- document archival-RPC reindex and backfill as an explicit operator flow
- document Postgres storage policy, retention, and sizing guidance
- evaluate whether operators need an optional durable buffer ahead of Postgres
  beyond the current spool/recovery model

## Snapshot And Recovery Follow-Ups

- decide whether periodic snapshot export belongs in `xian-abci` or in
  higher-level tooling such as `xian-cli` or `xian-stack`
- document retention and cleanup policy for application snapshots and BDS
  snapshots

## Runtime Follow-Ups

- parallel execution optimization beyond the current conservative design
- monitoring and operator ergonomics
