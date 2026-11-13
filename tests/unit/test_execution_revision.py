import json
from unittest.mock import patch

import pytest
from contracting.storage.lmdb_environment import standalone_environment

from xian import execution_policy as policy
from xian.state_export import import_state
from xian.state_root import merkle_root_from_items


def policy_state(value=None):
    return {
        "genesis": [
            {"key": "__execution_policy", "value": value or policy.current_execution_policy()}
        ]
    }


def test_policy_requires_exact_current_identifiers():
    expected = {
        "execution_revision": "xvm-execution-12",
        "gas_schedule": "xvm-gas-6",
        "bytecode_version": "xvm-2",
        "set_codec": "xian-set-1",
        "compiler_admission_revision": "xian-admission-2",
        "tx_admission_revision": "xian-fastpath-admission-2",
    }
    assert policy.current_execution_policy() == expected
    policy.validate_execution_policy(expected)
    for invalid in (None, {}, expected | {"gas_schedule": "xvm-gas-1"}, expected | {"extra": 1}):
        with pytest.raises(ValueError, match="execution policy"):
            policy.validate_execution_policy(invalid)


def test_genesis_requires_one_protected_policy():
    policy.validate_genesis_execution_policy({"abci_genesis": policy_state()})
    for invalid in ({"genesis": []}, {"genesis": policy_state()["genesis"] * 2}):
        with pytest.raises(ValueError, match="execution policy"):
            policy.validate_genesis_execution_policy(invalid)


def test_policy_is_part_of_the_state_commitment():
    old = {"execution_revision": "old"}
    assert merkle_root_from_items([("__execution_policy", old)]) != merkle_root_from_items([])
    assert merkle_root_from_items([("__execution_policy", old)]) != merkle_root_from_items(
        [("__execution_policy", policy.current_execution_policy())]
    )


@pytest.mark.parametrize("stored_policy", [None, {"execution_revision": "old"}])
def test_legacy_storage_is_rejected_without_mutation(tmp_path, stored_policy):
    path = tmp_path / "lmdb"
    path.mkdir()
    changes = [(b"con_demo.value", b"7")]
    if stored_policy is not None:
        changes.append((b"__execution_policy", json.dumps(stored_policy).encode()))
    with standalone_environment(path) as env:
        env.write(changes)
    before = (path / "data.mdb").read_bytes()
    with pytest.raises(ValueError, match="execution policy"):
        policy.validate_persisted_execution_policy(tmp_path)
    assert (path / "data.mdb").read_bytes() == before


def test_empty_storage_preflight_does_not_create_files(tmp_path):
    home = tmp_path / "absent"
    policy.validate_persisted_execution_policy(home)
    assert not home.exists()


def test_fresh_bootstrap_records_policy_before_contract_helpers(tmp_path):
    policy.initialize_execution_policy(tmp_path)
    assert policy.validate_persisted_execution_policy(tmp_path)
    before = (tmp_path / "lmdb/data.mdb").read_bytes()
    policy.initialize_execution_policy(tmp_path)
    assert (tmp_path / "lmdb/data.mdb").read_bytes() == before


def test_state_patch_cannot_change_execution_policy(tmp_path):
    from xian.utils.state_patches import StatePatchManager

    path = tmp_path / "patch.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "patch_id": "policy-change",
                "activation_height": 5,
                "changes": [{"key": "__execution_policy", "value": {}}],
            }
        )
    )
    with pytest.raises(ValueError, match="execution policy"):
        StatePatchManager(None)._load_bundle_file(path)


def test_incompatible_import_is_rejected_before_opening_driver():
    with patch("xian.state_export.Driver") as driver:
        with pytest.raises(ValueError, match="execution policy"):
            import_state(exported_state={"genesis": [], "hash": ""})
    driver.assert_not_called()


@pytest.mark.parametrize("valid", [True, False])
def test_open_driver_policy_check_preserves_bytes_and_cache(tmp_path, valid):
    from contracting.storage.driver import Driver

    driver = Driver(vm_profile="xian_vm_v2", storage_home=tmp_path)
    try:
        driver.set(
            "__execution_policy", policy.current_execution_policy() if valid else {"old": True}
        )
        driver.commit()
        driver.set("con_demo.pending", 9)
        before = (tmp_path / "lmdb/data.mdb").read_bytes()
        if valid:
            assert policy.validate_persisted_execution_policy(tmp_path)
        else:
            with pytest.raises(ValueError, match="execution policy"):
                policy.validate_persisted_execution_policy(tmp_path)
        assert (tmp_path / "lmdb/data.mdb").read_bytes() == before
        assert driver.get("con_demo.pending") == 9
    finally:
        driver.close()


def test_current_import_rejects_legacy_destination_before_flush(tmp_path):
    from contracting.storage.driver import Driver

    from xian.state_root import compute_exported_state_root

    exported = policy_state()
    exported["hash"] = compute_exported_state_root(exported).hex()
    driver = Driver(vm_profile="xian_vm_v2", storage_home=tmp_path)
    try:
        driver.set("con_old.value", 7)
        driver.commit()
        before = (tmp_path / "lmdb/data.mdb").read_bytes()
        with pytest.raises(ValueError, match="execution policy"):
            import_state(exported_state=exported, storage_home=tmp_path)
        assert (tmp_path / "lmdb/data.mdb").read_bytes() == before
        assert driver.get("con_old.value") == 7
    finally:
        driver.close()


def test_fresh_bootstrap_creates_missing_ancestors(tmp_path):
    home = tmp_path / "missing" / "child"
    policy.initialize_execution_policy(home)
    assert policy.validate_persisted_execution_policy(home)


def test_fresh_bootstrap_accepts_existing_empty_database(tmp_path):
    path = tmp_path / "lmdb"
    path.mkdir()
    with standalone_environment(path):
        pass
    assert policy.validate_persisted_execution_policy(tmp_path) is False
    policy.initialize_execution_policy(tmp_path)
    assert policy.validate_persisted_execution_policy(tmp_path)


@pytest.fixture(
    params=[
        "xvm-execution-2",
        "xvm-execution-3",
        "xvm-execution-4",
        "xvm-execution-5",
        "xvm-execution-6",
        "xvm-execution-7",
        "xvm-execution-8",
        "xvm-execution-9",
        "xvm-execution-10",
        "xvm-execution-11",
    ]
)
def previous_policy(request):
    return policy.current_execution_policy() | {"execution_revision": request.param}


def test_previous_revision_is_rejected_even_with_matching_gas_and_codec(previous_policy):
    with pytest.raises(ValueError, match="execution policy"):
        policy.validate_execution_policy(previous_policy)
    with pytest.raises(ValueError, match="execution policy"):
        policy.validate_genesis_execution_policy({"abci_genesis": policy_state(previous_policy)})


def test_previous_revision_database_is_rejected_without_mutation(tmp_path, previous_policy):
    path = tmp_path / "lmdb"
    path.mkdir()
    with standalone_environment(path) as env:
        env.write(
            [
                (b"con_demo.value", b"7"),
                (b"__execution_policy", json.dumps(previous_policy).encode()),
            ]
        )
    before = (path / "data.mdb").read_bytes()
    with pytest.raises(ValueError, match="execution policy"):
        policy.initialize_execution_policy(tmp_path)
    assert (path / "data.mdb").read_bytes() == before


def test_previous_revision_import_is_rejected_before_driver_open(previous_policy):
    with patch("xian.state_export.Driver") as driver:
        with pytest.raises(ValueError, match="execution policy"):
            import_state(exported_state=policy_state(previous_policy))
    driver.assert_not_called()


def test_previous_native_revision_is_rejected_even_when_gas_is_supported(previous_policy):
    from types import SimpleNamespace

    from xian.execution_engine import build_vm_runtime

    bindings = SimpleNamespace(
        runtime_info=lambda _profile: {"vm_profile": "xian_vm_v2", **previous_policy},
        supports_execution_policy=lambda *_args: True,
    )
    with patch("xian.execution_engine._load_vm_runtime_bindings", return_value=bindings):
        with pytest.raises(ValueError, match="execution policy"):
            build_vm_runtime()


def test_startup_fences_compiler_and_fastpath_admission_revisions():
    from types import SimpleNamespace

    from xian import execution_engine

    def compiler(revision):
        return SimpleNamespace(
            compiler_version=lambda: {"admission_revisions": {"xian_vm_v2": revision}}
        )

    current_compiler = compiler("xian-admission-2")
    current_fastpath = SimpleNamespace(ADMISSION_REVISION="xian-fastpath-admission-2")
    execution_engine.validate_admission_runtimes(
        compiler=current_compiler, fastpath=current_fastpath
    )
    # Without the extension the Python fallback, which applies the same
    # canonical admission rule, is authoritative.
    execution_engine.validate_admission_runtimes(compiler=current_compiler, fastpath=None)
    with pytest.raises(ValueError, match="compiler admission"):
        execution_engine.validate_admission_runtimes(
            compiler=compiler("xian-admission-1"), fastpath=current_fastpath
        )
    with pytest.raises(ValueError, match="fastpath admission"):
        execution_engine.validate_admission_runtimes(
            compiler=current_compiler,
            fastpath=SimpleNamespace(ADMISSION_REVISION="xian-fastpath-admission-1"),
        )
    with pytest.raises(ValueError, match="fastpath admission"):
        execution_engine.validate_admission_runtimes(
            compiler=current_compiler, fastpath=SimpleNamespace()
        )


def test_installed_native_runtimes_match_the_node_policy():
    from xian.execution_engine import build_vm_runtime

    runtime = build_vm_runtime()
    assert runtime.runtime_info["execution_revision"] == "xvm-execution-12"
