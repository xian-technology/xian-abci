# Tests

## Purpose

This folder contains the verification surface for `xian-abci`.

## Contents

- `unit/`: isolated module, helper, CLI, and service behavior
- `abci_methods/`: request-handler, query, startup, and finalize-block coverage
- `integration/`: multi-component runtime behavior such as processor,
  parallel-execution, rollback, and replay-parity flows
- `governance/`: governance paths that interact with node behavior
- `system/`: higher-level contract/runtime scenarios

## Environment Notes

- `tests/conftest.py` redirects `HOME` into `./.tmp-home` so tests do not write
  into a real user CometBFT home.
- The preferred release-grade entrypoint is `../scripts/validate-release.sh`.
- `tests/integration/test_vm_processor_fuzz.py` adds property-based
  Python-vs-native transaction replay parity coverage when the VM bindings are
  installed.
- CI provisions Postgres at `postgres://postgres:1234@localhost:5432/xian` for
  the BDS-backed paths.

## Next

- Start with the test group closest to the code you are changing.
