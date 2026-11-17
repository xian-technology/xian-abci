"""Full-store restore must validate both stores before publishing either."""

import copy
import gzip
import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from contracting.storage.driver import Driver
from full_node_store_fixtures import (
    Response,
    fixture_inspector,
    reference_config,
    seed_application,
)
from test_node_admin import _current_application_archive, _signed_snapshot_manifest
from test_snapshot_restore import archive_bytes
from xian_accounts import Ed25519Account

from xian.full_node_store import (
    FullNodeStoreLimits,
    NativeStoreInspectionLimits,
    _read_inspector_process,
    full_node_store_manifest_payload,
    read_full_node_store_checkpoint,
    verify_full_node_store_checkpoint,
    verify_full_node_store_manifest,
)
from xian.node_admin import _verify_snapshot_manifest, apply_snapshot_archive
from xian.node_setup import genesis_sha256


def test_old_signed_manifest_cannot_authorize_full_store_bootstrap():
    manifest, account = _signed_snapshot_manifest(
        archive_url="https://invalid/s.tar.gz", archive_bytes=b"x"
    )
    manifest["manifest_version"] = 1
    with pytest.raises(ValueError, match="manifest_version"):
        _verify_snapshot_manifest(
            manifest, trusted_manifest_public_keys=[account.public_key], expected_chain_id=None
        )


def test_trusted_checksum_does_not_make_app_only_archive_a_full_node_store(tmp_path):
    payload = _current_application_archive()
    with patch("xian.node_admin.urlopen", return_value=Response(payload)):
        with pytest.raises(ValueError, match="genesis|full.node|checkpoint"):
            apply_snapshot_archive(
                "https://invalid/s.tar.gz",
                tmp_path,
                expected_sha256=hashlib.sha256(payload).hexdigest(),
            )
    assert not (tmp_path / "data").exists()


@pytest.fixture
def stores(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    reference_config(source)
    reference_config(target)
    native = seed_application(source / "xian")
    (source / "data").mkdir()
    (source / "data/native-checkpoint.json").write_text(json.dumps(native))
    (target / "data").mkdir()
    (target / "data/original").write_text("original native")
    (target / "xian").mkdir()
    (target / "xian/original").write_text("original app")
    entries = [
        (p.relative_to(source).as_posix(), p.read_bytes())
        for root in ("data", "xian")
        for p in (source / root).rglob("*")
        if p.is_file()
    ]
    payload = archive_bytes(entries)
    signer = Ed25519Account.generate()
    manifest = {
        "manifest_version": 2,
        "snapshot_kind": "full_node_store",
        "chain_id": "xian-test-1",
        "height": 123,
        "genesis_sha256": genesis_sha256({"chain_id": "xian-test-1", "initial_height": "1"}),
        "app_hash": native["app_hash"],
        "block_hash": native["block_hash"],
        "snapshot_url": "https://invalid/store.tar.gz",
        "snapshot_sha256": hashlib.sha256(payload).hexdigest(),
        "snapshot_bytes": len(payload),
        "uncompressed_bytes": len(gzip.decompress(payload)),
        "signing_public_key": signer.public_key,
    }
    manifest["signature"] = signer.sign_message(full_node_store_manifest_payload(manifest))
    return source, target, native, payload, manifest, signer


def test_checkpoint_read_is_readonly_and_binds_both_stores(stores):
    source, _, native, _, manifest, _ = stores
    before = {p: p.read_bytes() for p in source.rglob("*") if p.is_file()}
    result = verify_full_node_store_checkpoint(
        source, manifest, native_store_inspector=fixture_inspector
    )
    assert result["app_hash"] == native["app_hash"]
    assert result["height"] == 123
    assert before == {p: p.read_bytes() for p in source.rglob("*") if p.is_file()}


@pytest.mark.parametrize(
    "field,value",
    [
        ("height", True),
        ("snapshot_bytes", 0),
        ("uncompressed_bytes", -1),
        ("manifest_version", True),
        ("snapshot_kind", "application"),
        ("genesis_sha256", "A" * 64),
        ("signature", "ab"),
        ("snapshot_url", ""),
    ],
)
def test_manifest_strict_types_and_domain(stores, field, value):
    *_, manifest, signer = stores
    with pytest.raises(ValueError):
        invalid = {**manifest, field: value}
        if field != "signature":
            invalid["signature"] = signer.sign_message(
                json.dumps(
                    {k: v for k, v in invalid.items() if k != "signature"},
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        verify_full_node_store_manifest(invalid, trusted_manifest_public_keys=[signer.public_key])


@pytest.mark.parametrize(
    "field",
    [
        "chain_id",
        "height",
        "genesis_sha256",
        "app_hash",
        "block_hash",
        "snapshot_url",
        "snapshot_sha256",
        "snapshot_bytes",
        "uncompressed_bytes",
    ],
)
def test_signature_covers_every_security_field(stores, field):
    *_, manifest, signer = stores
    changed = copy.deepcopy(manifest)
    value = changed[field]
    changed[field] = (
        value + 1 if type(value) is int else ("c" * 64 if len(value) == 64 else value + "tampered")
    )
    with pytest.raises(ValueError, match="signature verification"):
        verify_full_node_store_manifest(changed, trusted_manifest_public_keys=[signer.public_key])


@pytest.mark.parametrize(
    "edit",
    [
        {"height": "124"},
        {"block_store_height": "124"},
        {"app_hash": "e" * 64},
        {"last_block_nanos": "1700000000123456790"},
        {"chain_id": "other"},
        {"initial_height": "2"},
        {"height": "0123"},
        {"schema_version": True},
        {"block_store_base": "124"},
        {"history_first_nanos": "1700000000123456790"},
        {"history_first_verified": "2"},
        {
            "evidence": {
                "max_age_num_blocks": "100000",
                "max_age_duration": "1",
                "max_bytes": "1048576",
            }
        },
    ],
)
def test_native_checkpoint_mismatches_cannot_publish(stores, edit):
    source, target, native, payload, _, _ = stores

    def inspect(*_):
        return {**native, **edit}

    with patch("xian.node_admin.urlopen", return_value=Response(payload)):
        with pytest.raises(ValueError):
            apply_snapshot_archive(
                "https://invalid/s.tar.gz",
                target,
                expected_sha256=hashlib.sha256(payload).hexdigest(),
                native_store_inspector=inspect,
            )
    assert (target / "data/original").read_text() == "original native"
    assert (target / "xian/original").read_text() == "original app"
    assert not list(target.glob(".snapshot-restore-*/backup"))


def test_recomputed_app_root_refuses_tampered_state(stores):
    source, *_ = stores
    with Driver(storage_home=source / "xian", vm_profile="xian_vm_v2") as driver:
        driver.set("demo.value", 999)
        driver.commit()
    with pytest.raises(ValueError, match="application root mismatch"):
        read_full_node_store_checkpoint(source, native_store_inspector=fixture_inspector)


def test_history_requires_both_age_limits_and_accepts_pruned_bodies(stores):
    source, _, native, *_ = stores
    native["history_first_verified"] = "2"
    native["history_first_nanos"] = str(
        int(native["last_block_nanos"]) - int(native["evidence"]["max_age_duration"]) - 1
    )

    def inspect(*_):
        return native

    with pytest.raises(ValueError, match="complete live window"):
        read_full_node_store_checkpoint(source, native_store_inspector=inspect)
    # Both limits exceeded; bodies start at H but retained proof metadata reaches 2.
    native.update(height="100003", block_store_height="100003", block_store_base="100003")
    with Driver(storage_home=source / "xian", vm_profile="xian_vm_v2") as driver:
        marker = driver.get("__latest_block")
        driver.set("__latest_block", {**marker, "height": 100003})
        driver.commit()
    assert (
        read_full_node_store_checkpoint(source, native_store_inspector=inspect)["height"] == 100003
    )


@pytest.mark.parametrize("signed", [False, True])
def test_full_store_restore_accepts_explicit_trust_anchor(stores, signed):
    _, target, _, payload, manifest, signer = stores
    response = Response(payload)
    with (
        patch("xian.node_admin.urlopen", return_value=response),
        patch("xian.node_admin._fetch_json_url", return_value=manifest),
    ):
        result = apply_snapshot_archive(
            "https://invalid/input",
            target,
            expected_sha256=manifest["snapshot_sha256"],
            trusted_manifest_public_keys=[signer.public_key] if signed else None,
            expected_chain_id="xian-test-1",
            native_store_inspector=fixture_inspector,
        )
    assert (target / "xian/lmdb/data.mdb").is_file()
    assert (target / "data/native-checkpoint.json").is_file()
    assert not (target / "data/original").exists()
    assert result == ("store.tar.gz" if signed else "input")


@pytest.mark.parametrize("advertised,actual", [(100000, 1), (1, 2), (10, 2)])
def test_download_size_mismatch_is_bounded_and_keeps_original(stores, advertised, actual):
    _, target, _, _, manifest, signer = stores
    manifest = {**manifest, "snapshot_bytes": 1}
    manifest["signature"] = signer.sign_message(full_node_store_manifest_payload(manifest))
    with (
        patch("xian.node_admin._fetch_json_url", return_value=manifest),
        patch(
            "xian.node_admin.urlopen",
            return_value=Response(b"x" * actual, content_length=advertised),
        ),
    ):
        with pytest.raises(ValueError, match="byte count|configured limit"):
            apply_snapshot_archive(
                "https://invalid/m",
                target,
                trusted_manifest_public_keys=[signer.public_key],
                native_store_inspector=fixture_inspector,
            )
    assert (target / "data/original").exists()


def test_compressed_bomb_and_member_limits_fail_before_publication(stores):
    _, target, _, payload, _, _ = stores
    for limits in (
        FullNodeStoreLimits(max_uncompressed_bytes=100),
        FullNodeStoreLimits(max_members=1),
    ):
        with patch("xian.node_admin.urlopen", return_value=Response(payload)):
            with pytest.raises(ValueError, match="configured limit"):
                apply_snapshot_archive(
                    "https://invalid/s",
                    target,
                    expected_sha256=hashlib.sha256(payload).hexdigest(),
                    native_store_inspector=fixture_inspector,
                    limits=limits,
                )
        assert (target / "data/original").exists()


def test_manifest_fetch_stops_at_64k_boundary():
    from xian.node_admin import _fetch_json_url

    response = Response(b"x" * 100000)
    with patch("xian.node_admin.urlopen", return_value=response):
        with pytest.raises(ValueError, match="64KiB"):
            _fetch_json_url("https://invalid/manifest", timeout=1)
    # A response read request is bounded even for a server omitting Content-Length.
    assert response.closed


def test_native_inspection_rejects_unbounded_output_and_timeout():
    with pytest.raises(ValueError, match="output exceeds"):
        _read_inspector_process([sys.executable, "-c", "print('x'*100000)"], timeout=1)
    with pytest.raises(ValueError, match="timed out"):
        _read_inspector_process([sys.executable, "-c", "import time;time.sleep(3)"], timeout=0.05)


def test_inspector_budget_is_operator_configurable(monkeypatch):
    monkeypatch.setenv("XIAN_NODE_STORE_INSPECT_MAX_HISTORY_HEIGHTS", "2000000")
    monkeypatch.setenv("XIAN_NODE_STORE_INSPECT_TIMEOUT_SECONDS", "600")
    limits = NativeStoreInspectionLimits.from_environment()
    assert limits.command_arguments() == ["--max-history-heights", "2000000", "--timeout", "600s"]
    monkeypatch.setenv("XIAN_NODE_STORE_INSPECT_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValueError):
        NativeStoreInspectionLimits.from_environment()


def test_configure_home_verifies_the_intended_genesis_before_restoring(stores):
    from test_node_home_rules import _initialized_home

    from xian.node_admin import ExistingHomeOptions, configure_existing_home

    _, target, _, payload, manifest, signer = stores
    _initialized_home(target.parent / "configured")
    target = target.parent / "configured/.cometbft"
    with (
        patch("xian.node_admin._fetch_json_url", return_value=manifest),
        patch("xian.node_admin.urlopen", return_value=Response(payload)),
    ):
        configure_existing_home(
            ExistingHomeOptions(
                moniker="node",
                home=target,
                genesis_payload={"chain_id": "xian-test-1", "initial_height": "1"},
                snapshot_url="https://invalid/manifest",
                snapshot_signing_public_keys=(signer.public_key,),
                native_store_inspector=fixture_inspector,
            )
        )
    assert (target / "data/native-checkpoint.json").is_file()
    assert json.loads((target / "config/genesis.json").read_text())["chain_id"] == "xian-test-1"


def test_raw_trusted_sha_remains_usable_with_a_configured_manifest_signer(stores):
    _, target, _, payload, manifest, signer = stores
    with patch("xian.node_admin.urlopen", side_effect=lambda *_args, **_kwargs: Response(payload)):
        apply_snapshot_archive(
            "https://invalid/raw.tar.gz",
            target,
            expected_sha256=manifest["snapshot_sha256"],
            trusted_manifest_public_keys=[signer.public_key],
            native_store_inspector=fixture_inspector,
        )
    assert (target / "data/native-checkpoint.json").is_file()


def test_failed_restore_rolls_back_the_newly_selected_genesis(stores):
    from test_node_home_rules import _initialized_home

    from xian.node_admin import ExistingHomeOptions, configure_existing_home

    source, target, _, _, manifest, signer = stores
    target = _initialized_home(target.parent / "configure-fail")
    original = b'{"chain_id":"previous"}\n'
    (target / "config/genesis.json").write_bytes(original)
    with (
        patch("xian.node_admin._fetch_json_url", return_value=manifest),
        patch("xian.node_admin.urlopen", return_value=Response(b"wrong archive")),
    ):
        with pytest.raises(ValueError):
            configure_existing_home(
                ExistingHomeOptions(
                    moniker="node",
                    home=target,
                    genesis_payload={"chain_id": "xian-test-1", "initial_height": "1"},
                    snapshot_url="https://invalid/m",
                    snapshot_signing_public_keys=(signer.public_key,),
                )
            )
    assert (target / "config/genesis.json").read_bytes() == original


def test_marker_blocks_a_crashed_publication_even_without_old_roots(stores):
    import shutil
    import subprocess

    from xian.snapshot_restore_status import validate_snapshot_restore_complete

    _, target, _, payload, _, _ = stores
    shutil.rmtree(target / "data")
    shutil.rmtree(target / "xian")
    archive = target / "archive.tar.gz"
    archive.write_bytes(payload)
    program = """
import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[3])
from full_node_store_fixtures import fixture_inspector
from xian.snapshot_restore import restore_application_archive
original=Path.rename
def crash_after_first_publish(path,destination):
    result=original(path,destination)
    if path.name=='data' and path.parent.name=='staged': os._exit(77)
    return result
Path.rename=crash_after_first_publish
restore_application_archive(Path(sys.argv[1]),Path(sys.argv[2]),native_store_inspector=fixture_inspector)
"""
    child = subprocess.run(
        [sys.executable, "-c", program, str(archive), str(target), str(Path(__file__).parent)],
        capture_output=True,
        timeout=15,
    )
    assert child.returncode == 77, child.stderr.decode()
    assert (target / "data/native-checkpoint.json").exists()
    assert not (target / "xian").exists()
    with pytest.raises(ValueError, match="incomplete snapshot restore"):
        validate_snapshot_restore_complete(target)


@pytest.mark.parametrize(
    "field,value",
    [
        ("manifest_version", 1),
        ("manifest_version", 3),
        ("chain_id", None),
        ("snapshot_bytes", (1 << 63)),
        ("height", 0),
    ],
)
def test_manifest_cannot_weaken_schema_or_integer_bounds(stores, field, value):
    *_, manifest, signer = stores
    with pytest.raises(ValueError):
        verify_full_node_store_manifest(
            {**manifest, field: value}, trusted_manifest_public_keys=[signer.public_key]
        )
    for changed in (
        {key: val for key, val in manifest.items() if key != "height"},
        {**manifest, "extra": 1},
    ):
        with pytest.raises(ValueError):
            verify_full_node_store_manifest(
                changed, trusted_manifest_public_keys=[signer.public_key]
            )


def test_decimal_and_limit_boundaries_remain_exact():
    from xian.evidence_policy import INT64_MAX, TIMESTAMP_MAX_NS, TIMESTAMP_MIN_NS
    from xian.full_node_store import canonical_integer, positive_integer

    assert positive_integer(1, "H") == 1
    assert positive_integer(INT64_MAX, "H") == INT64_MAX
    assert canonical_integer(str(INT64_MAX), "H") == INT64_MAX
    assert canonical_integer("0", "bytes", minimum=0) == 0
    for value in (True, 0, INT64_MAX + 1):
        with pytest.raises(ValueError):
            positive_integer(value, "H")
    for value in ("+1", "01", "-0", "x" * 33, str(INT64_MAX + 1), 1):
        with pytest.raises(ValueError):
            canonical_integer(value, "H")
    for nanos in (TIMESTAMP_MIN_NS, TIMESTAMP_MAX_NS):
        assert (
            canonical_integer(
                str(nanos), "timestamp", minimum=TIMESTAMP_MIN_NS, maximum=TIMESTAMP_MAX_NS
            )
            == nanos
        )
    for limits in ({"max_members": 0}, {"min_free_disk_bytes": -1}, {"max_archive_bytes": True}):
        with pytest.raises(ValueError):
            FullNodeStoreLimits(**limits)


def test_actual_manifest_uncompressed_count_is_exact(stores):
    _, target, _, payload, manifest, signer = stores
    manifest = {**manifest, "uncompressed_bytes": manifest["uncompressed_bytes"] + 1}
    manifest["signature"] = signer.sign_message(full_node_store_manifest_payload(manifest))
    with (
        patch("xian.node_admin._fetch_json_url", return_value=manifest),
        patch("xian.node_admin.urlopen", return_value=Response(payload)),
    ):
        with pytest.raises(ValueError, match="uncompressed archive byte count mismatch"):
            apply_snapshot_archive(
                "https://invalid/m",
                target,
                trusted_manifest_public_keys=[signer.public_key],
                native_store_inspector=fixture_inspector,
            )
    assert (target / "data/original").exists()


def test_full_store_disk_reserve_is_enforced_during_copy(tmp_path):
    import io
    from types import SimpleNamespace

    limits = FullNodeStoreLimits(min_free_disk_bytes=256)
    with patch(
        "xian.full_node_store_limits.shutil.disk_usage", return_value=SimpleNamespace(free=259)
    ):
        with pytest.raises(ValueError, match="insufficient free disk"):
            limits.copy_stream(
                io.BytesIO(b"data"), tmp_path / "blocked", maximum=4, label="download"
            )
    assert (tmp_path / "blocked").stat().st_size == 0
    with patch(
        "xian.full_node_store_limits.shutil.disk_usage", return_value=SimpleNamespace(free=260)
    ):
        size, digest = limits.copy_stream(
            io.BytesIO(b"data"), tmp_path / "allowed", maximum=4, label="download"
        )
    assert size == 4 and digest == hashlib.sha256(b"data").hexdigest()


def test_default_inspector_uses_the_same_configurable_budget_and_public_genesis(
    tmp_path, monkeypatch
):
    from xian.full_node_store import inspect_native_store

    monkeypatch.setenv("XIAN_NODE_STORE_INSPECT_TIMEOUT_SECONDS", "600")
    with patch("xian.full_node_store._read_inspector_process", return_value={}) as invoke:
        assert inspect_native_store(tmp_path / "data", "goleveldb", tmp_path / "genesis.json") == {}
    command = invoke.call_args.args[0]
    assert command[command.index("--genesis-file") + 1] == str(tmp_path / "genesis.json")
    assert command[command.index("--timeout") + 1] == "600s"
    assert invoke.call_args.kwargs["timeout"] == 610
    with pytest.raises(ValueError, match="only goleveldb"):
        inspect_native_store(tmp_path, "rocksdb", tmp_path / "genesis.json")
    with patch("xian.full_node_store._read_inspector_process", side_effect=FileNotFoundError):
        with pytest.raises(ValueError, match="is required"):
            inspect_native_store(tmp_path, "goleveldb", tmp_path / "genesis.json")


@pytest.mark.parametrize("block_age,time_extra", [(100000, 1), (100001, 0)])
def test_exact_evidence_expiry_boundary_remains_live(stores, block_age, time_extra):
    source, _, native, *_ = stores
    height = block_age + 2
    native.update(
        height=str(height),
        block_store_height=str(height),
        block_store_base=str(height),
        history_first_verified="2",
        history_first_nanos=str(
            int(native["last_block_nanos"])
            - int(native["evidence"]["max_age_duration"])
            - time_extra
        ),
    )
    with Driver(storage_home=source / "xian", vm_profile="xian_vm_v2") as driver:
        marker = driver.get("__latest_block")
        driver.set("__latest_block", {**marker, "height": height})
        driver.commit()
    with pytest.raises(ValueError, match="complete live window"):
        read_full_node_store_checkpoint(source, native_store_inspector=lambda *_: native)


def test_first_committed_block_has_complete_initial_history(stores):
    source, _, native, *_ = stores
    native.update(height="1", block_store_height="1", block_store_base="1")
    with Driver(storage_home=source / "xian", vm_profile="xian_vm_v2") as driver:
        marker = driver.get("__latest_block")
        driver.set("__latest_block", {**marker, "height": 1})
        driver.commit()
    assert (
        read_full_node_store_checkpoint(source, native_store_inspector=lambda *_: native)["height"]
        == 1
    )


def test_native_schema_rejects_extra_fields_at_the_parse_boundary(stores):
    from xian.full_node_store import _native_checkpoint, _native_evidence

    source, _, native, *_ = stores
    with pytest.raises(ValueError, match="noncanonical fields"):
        read_full_node_store_checkpoint(
            source, native_store_inspector=lambda *_: {**native, "extra": 1}
        )
    with pytest.raises(ValueError, match="full evidence limits"):
        _native_evidence({**native["evidence"], "extra": "1"})
    assert _native_evidence(
        {"max_age_num_blocks": "1", "max_age_duration": "1", "max_bytes": "0"}
    ) == {"max_age_num_blocks": 1, "max_age_duration": 1, "max_bytes": 0}
    with pytest.raises(ValueError):
        _native_checkpoint([])


def test_zero_size_and_cumulative_stream_limit_do_not_overread(tmp_path):
    import io

    limits = FullNodeStoreLimits(min_free_disk_bytes=0)
    limits.check_size(0, 0, "empty file")
    with pytest.raises(ValueError):
        limits.check_size(-1, 100, "file")
    maximum = 1024 * 1024 + 2
    stream = io.BytesIO(b"x" * maximum + b"trailing")
    with pytest.raises(ValueError, match="configured limit"):
        limits.copy_stream(stream, tmp_path / "capped", maximum=maximum, label="archive")
    assert stream.tell() == maximum + 1


def test_full_store_checkpoint_compares_derived_mid_ramp_limits_not_stored_base(stores):
    from xian.evidence_policy import DAY_NS, EVIDENCE_POLICY_KEY, EvidencePolicy
    from xian.state_root import compute_driver_state_root

    source, _, native, *_ = stores
    point = int(native["last_block_nanos"])
    schedule = EvidencePolicy.seed(7, nanos=point - DAY_NS).advance(
        nanos=point - DAY_NS, observed_days=[14]
    )
    with Driver(storage_home=source / "xian", vm_profile="xian_vm_v2") as driver:
        driver.set(EVIDENCE_POLICY_KEY, schedule.to_state())
        root = compute_driver_state_root(driver).hex()
        driver.set("__latest_block", {"height": 123, "hash": root, "nanos": point})
        driver.commit()
    native.update(app_hash=root, evidence=schedule.comet_json(nanos=point))
    assert native["evidence"]["max_age_duration"] == str(9 * DAY_NS)
    (source / "data/native-checkpoint.json").write_text(json.dumps(native))
    assert (
        read_full_node_store_checkpoint(source, native_store_inspector=fixture_inspector)[
            "app_hash"
        ]
        == root
    )
    native["evidence"]["max_age_duration"] = str(8 * DAY_NS)
    (source / "data/native-checkpoint.json").write_text(json.dumps(native))
    with pytest.raises(ValueError, match="evidence limits mismatch"):
        read_full_node_store_checkpoint(source, native_store_inspector=fixture_inspector)
