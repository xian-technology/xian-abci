# Tests

## Purpose

This folder contains the verification surface for `xian-abci`.

## Contents

- `unit/`: isolated module, helper, CLI, and service behavior
- `abci_methods/`: request-handler, query, startup, and finalize-block coverage
- `integration/`: multi-component runtime behavior such as processor,
  rollback, and replay-parity flows
- `governance/`: governance paths that interact with node behavior
- `system/`: higher-level contract/runtime scenarios

## Environment Notes

- `tests/conftest.py` assigns a unique temporary `HOME` while pytest collects
  modules and restores the environment when pytest exits. Every test then gets
  its own `HOME`, CometBFT home, and default contract storage under `tmp_path`.
- Use `node_home` for an empty node directory and `node_constants` for the
  matching node paths. Tests needing a committed node fixture request
  `node_fixture`; it copies `fixtures/.cometbft-fixture` beside that test module
  into the private node directory. On a unittest class, mark the class with
  `@pytest.mark.usefixtures("node_fixture")` and use `self.constants`.
- Resolve test resources from `__file__` or the test's `request.path`. Keep
  clients, drivers, and apps scoped with context managers, `finally`, or unittest
  cleanup methods so storage is closed before its directory is removed.
- Independent pytest processes can run concurrently without sharing default
  storage or node directories. From `xian-meta`, run
  `python3 scripts/verify_test_isolation.py --help` for the synchronized
  persistence check that exercises the real pytest fixtures in parallel.
- The preferred release-grade entrypoint is `../scripts/validate-release.sh`.
- `tests/integration/test_vm_processor_fuzz.py` adds property-based
  Python-vs-native transaction replay parity coverage when the VM bindings are
  installed.
- CI provisions Postgres at `postgres://postgres:1234@localhost:5432/xian` for
  the BDS-backed paths.

## Next

- Start with the test group closest to the code you are changing.
