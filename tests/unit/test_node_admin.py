import gzip
import hashlib
import io
import json
import os
import stat
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from full_node_store_fixtures import (
    Response,
    fixture_inspector,
    reference_config,
    seed_application,
)
from xian_accounts import Ed25519Account

from xian.execution_policy import validate_persisted_execution_policy
from xian.full_node_store import full_node_store_manifest_payload
from xian.node_admin import (
    ExistingHomeOptions,
    apply_snapshot_archive,
    configure_existing_home,
    fetch_seed_node_status,
    resolve_p2p_seeds,
)
from xian.node_setup import (
    AppLoggingOptions,
    BdsOptions,
    GenesisIntegrityError,
    MetricsOptions,
    NodeConfigOptions,
    SimulationOptions,
    genesis_sha256,
    render_node_configs,
    write_json,
    write_toml,
)
from xian.toml_utils import load as load_toml
from xian.toml_utils import loads as load_toml_string


def _current_application_archive() -> bytes:
    stream = io.BytesIO()
    with tempfile.TemporaryDirectory() as storage_home:
        point = seed_application(Path(storage_home))
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            payload = b"new state"
            info = tarfile.TarInfo("data/new.txt")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
            payload = json.dumps(point).encode()
            info = tarfile.TarInfo("data/native-checkpoint.json")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
            archive.add(Path(storage_home) / "lmdb/data.mdb", arcname="xian/lmdb/data.mdb")
    return stream.getvalue()


def _signed_snapshot_manifest(
    *,
    archive_url: str,
    archive_bytes: bytes,
    chain_id: str = "xian-test-1",
    height: int = 123,
    signing_account: Ed25519Account | None = None,
) -> tuple[dict[str, object], Ed25519Account]:
    account = signing_account or Ed25519Account.generate()
    manifest = {
        "manifest_version": 2,
        "snapshot_kind": "full_node_store",
        "genesis_sha256": genesis_sha256({"chain_id": chain_id, "initial_height": "1"}),
        "app_hash": "a" * 64,
        "block_hash": "b" * 64,
        "snapshot_bytes": len(archive_bytes),
        "uncompressed_bytes": len(gzip.decompress(archive_bytes))
        if archive_bytes[:2] == b"\x1f\x8b"
        else len(archive_bytes),
        "chain_id": chain_id,
        "height": height,
        "snapshot_url": archive_url,
        "snapshot_sha256": hashlib.sha256(archive_bytes).hexdigest(),
        "signing_public_key": account.public_key,
    }
    if archive_bytes[:2] == b"\x1f\x8b":
        with tarfile.open(fileobj=io.BytesIO(archive_bytes)) as archive:
            try:
                point = json.load(archive.extractfile("data/native-checkpoint.json"))
                manifest["app_hash"] = point["app_hash"]
            except KeyError:
                pass
    payload = full_node_store_manifest_payload(manifest)
    manifest["signature"] = account.sign_message(payload)
    return manifest, account


class NodeAdminTests(unittest.TestCase):
    def test_resolve_p2p_seeds_uses_explicit_seed(self):
        self.assertEqual(
            resolve_p2p_seeds(p2p_seeds=("abc@127.0.0.1:26656",)),
            ["abc@127.0.0.1:26656"],
        )

    def test_resolve_p2p_seeds_queries_status_endpoint(self):
        with patch(
            "xian.node_admin.fetch_seed_node_status",
            return_value={"result": {"node_info": {"id": "node-123"}}},
        ):
            self.assertEqual(
                resolve_p2p_seeds(discover_seeds=("127.0.0.1",)),
                ["node-123@127.0.0.1:26656"],
            )

    def test_fetch_seed_node_status_brackets_ipv6_status_url(self):
        with patch(
            "xian.node_admin._fetch_json_url",
            return_value={"result": {"node_info": {"id": "node-123"}}},
        ) as fetch:
            self.assertEqual(
                fetch_seed_node_status("::1", attempts=1),
                {"result": {"node_info": {"id": "node-123"}}},
            )

        fetch.assert_called_once_with("http://[::1]:26657/status", timeout=3.0)

    def test_fetch_seed_node_status_accepts_bracketed_ipv6_port(self):
        with patch(
            "xian.node_admin._fetch_json_url",
            return_value={"result": {"node_info": {"id": "node-123"}}},
        ) as fetch:
            fetch_seed_node_status("[::1]:36657", attempts=1)

        fetch.assert_called_once_with("http://[::1]:36657/status", timeout=3.0)

    def test_fetch_seed_node_status_preserves_explicit_https_ipv6_url(self):
        with patch(
            "xian.node_admin._fetch_json_url",
            return_value={"result": {"node_info": {"id": "node-123"}}},
        ) as fetch:
            fetch_seed_node_status("https://::1:36657", attempts=1)

        fetch.assert_called_once_with("https://[::1]:36657/status", timeout=3.0)

    def test_resolve_p2p_seeds_brackets_discovered_ipv6_seed(self):
        with patch(
            "xian.node_admin.fetch_seed_node_status",
            return_value={"result": {"node_info": {"id": "node-123"}}},
        ):
            self.assertEqual(
                resolve_p2p_seeds(discover_seeds=("::1",)),
                ["node-123@[::1]:26656"],
            )

    def test_resolve_p2p_seeds_brackets_explicit_ipv6_seed(self):
        self.assertEqual(
            resolve_p2p_seeds(p2p_seeds=("abc@::1:26656",)),
            ["abc@[::1]:26656"],
        )

    def test_apply_snapshot_archive_verifies_signed_manifest_and_extracts_tar(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            (home / "data").mkdir()
            (home / "data" / "old.txt").write_text("old", encoding="utf-8")
            (home / "xian").mkdir()
            (home / "xian" / "old.txt").write_text("old", encoding="utf-8")

            reference_config(home)
            archive_bytes = _current_application_archive()
            manifest, account = _signed_snapshot_manifest(
                archive_url="https://example.invalid/snapshot.tar.gz",
                archive_bytes=archive_bytes,
            )

            with (
                patch(
                    "xian.node_admin._fetch_json_url",
                    return_value=manifest,
                ),
                patch(
                    "xian.node_admin.urlopen",
                    return_value=Response(archive_bytes),
                ),
            ):
                archive_path = apply_snapshot_archive(
                    "https://example.invalid/snapshot-manifest.json",
                    home,
                    trusted_manifest_public_keys=[account.public_key],
                    expected_chain_id="xian-test-1",
                    native_store_inspector=fixture_inspector,
                )

            self.assertTrue((home / "data" / "new.txt").exists())
            self.assertTrue(validate_persisted_execution_policy(home / "xian"))
            self.assertFalse((home / "data" / "old.txt").exists())
            self.assertFalse((home / "xian" / "old.txt").exists())
            self.assertEqual(archive_path, "snapshot.tar.gz")

    def test_apply_snapshot_archive_rejects_unsigned_remote_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)

            with self.assertRaisesRegex(
                ValueError,
                "requires expected_sha256 or trusted snapshot signing keys",
            ):
                apply_snapshot_archive(
                    "https://example.invalid/snapshot.tar.gz",
                    home,
                )

    def test_snapshot_trust_accepts_only_canonical_lowercase_keys(self):
        # One signing key must have one spelling in trust lists and manifests.
        manifest, account = _signed_snapshot_manifest(
            archive_url="https://example.invalid/snapshot.tar.gz",
            archive_bytes=b"archive",
        )
        with self.assertRaisesRegex(ValueError, "invalid snapshot signing key"):
            apply_snapshot_archive(
                "https://example.invalid/snapshot-manifest.json",
                Path(tempfile.gettempdir()),
                trusted_manifest_public_keys=[account.public_key.upper()],
            )
        uppercase_signer = {**manifest, "signing_public_key": account.public_key.upper()}
        with patch("xian.node_admin._fetch_json_url", return_value=uppercase_signer):
            with self.assertRaisesRegex(ValueError, "lowercase 64-hex"):
                apply_snapshot_archive(
                    "https://example.invalid/snapshot-manifest.json",
                    Path(tempfile.gettempdir()),
                    trusted_manifest_public_keys=[account.public_key],
                )

    def test_apply_snapshot_archive_rejects_untrusted_manifest_signer(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            archive_stream = io.BytesIO()
            with tarfile.open(fileobj=archive_stream, mode="w:gz") as archive:
                payload = b"new state"
                info = tarfile.TarInfo("data/new.txt")
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            manifest, _account = _signed_snapshot_manifest(
                archive_url="https://example.invalid/snapshot.tar.gz",
                archive_bytes=archive_stream.getvalue(),
            )

            with patch(
                "xian.node_admin._fetch_json_url",
                return_value=manifest,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "signer is not trusted",
                ):
                    apply_snapshot_archive(
                        "https://example.invalid/snapshot-manifest.json",
                        home,
                        trusted_manifest_public_keys=["b" * 64],
                    )

    def test_apply_snapshot_archive_verifies_expected_sha256(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)

            reference_config(home)
            archive_bytes = _current_application_archive()
            expected_sha256 = hashlib.sha256(archive_bytes).hexdigest()

            with patch(
                "xian.node_admin.urlopen",
                return_value=Response(archive_bytes),
            ):
                archive_path = apply_snapshot_archive(
                    "https://example.invalid/snapshot.tar.gz",
                    home,
                    expected_sha256=expected_sha256,
                    native_store_inspector=fixture_inspector,
                )

            self.assertEqual(archive_path, "snapshot.tar.gz")
            self.assertTrue((home / "data" / "new.txt").exists())
            self.assertTrue(validate_persisted_execution_policy(home / "xian"))

    def test_apply_snapshot_archive_rejects_wrong_sha256(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)

            archive_stream = io.BytesIO()
            with tarfile.open(fileobj=archive_stream, mode="w:gz") as archive:
                payload = b"new state"
                info = tarfile.TarInfo("data/new.txt")
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))

            with patch(
                "xian.node_admin.urlopen",
                return_value=Response(archive_stream.getvalue()),
            ):
                with self.assertRaises(ValueError):
                    apply_snapshot_archive(
                        "https://example.invalid/snapshot.tar.gz",
                        home,
                        expected_sha256="deadbeef",
                    )

    def test_configure_existing_home_refuses_an_enabled_state_sync_setting(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config_path = home / "config/config.toml"
            config_path.parent.mkdir()
            config = render_node_configs(options=NodeConfigOptions(moniker="node"))["cometbft"]
            config["statesync"]["enable"] = True
            write_toml(config_path, config)
            original = config_path.read_bytes()

            with self.assertRaisesRegex(ValueError, "ordinary replay.*verified full-node-store"):
                configure_existing_home(ExistingHomeOptions(moniker="updated", home=home))

            self.assertEqual(config_path.read_bytes(), original)
            self.assertFalse((home / "config/xian.toml").exists())

    def test_configure_existing_home_renders_config_and_writes_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            home = tmp_path / ".cometbft"
            config_path = home / "config" / "config.toml"
            config_path.parent.mkdir(parents=True)

            existing_config = load_toml_string(
                """
version = "0.39.3"
proxy_app = "unix:///tmp/abci.sock"
moniker = "initial-node"
db_backend = "rocksdb"
db_dir = "custom-data"
log_level = "debug"
log_format = "json"
genesis_file = "config/genesis.json"
priv_validator_key_file = "config/priv_validator_key.json"
priv_validator_state_file = "data/priv_validator_state.json"
node_key_file = "config/node_key.json"
abci = "socket"
filter_peers = false

[rpc]
laddr = "tcp://0.0.0.0:30057"
cors_allowed_origins = ["*"]

[p2p]
laddr = "tcp://0.0.0.0:30056"
seeds = ""

[consensus]
create_empty_blocks = false
create_empty_blocks_interval = "0s"

[instrumentation]
prometheus = false
""".strip()
            )
            write_toml(config_path, existing_config)

            configs_dir = tmp_path / "xian-configs"
            network_dir = configs_dir / "networks" / "local"
            network_dir.mkdir(parents=True)
            genesis_payload = {
                "chain_id": "xian-mainnet-next",
                "validators": [],
                "abci_genesis": {},
            }
            (network_dir / "genesis.json").write_text(
                json.dumps(genesis_payload),
                encoding="utf-8",
            )

            with patch.dict("os.environ", {"XIAN_CONFIGS_DIR": str(configs_dir)}):
                result = configure_existing_home(
                    ExistingHomeOptions(
                        moniker="updated-node",
                        home=home,
                        validator_private_key_hex=(
                            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
                        ),
                        p2p_seeds=("seed-1@127.0.0.1:26656",),
                        p2p_persistent_peers=("peer-1@127.0.0.1:26656",),
                        genesis_source="local",
                        node_config=NodeConfigOptions(
                            moniker="updated-node",
                            enable_pruning=True,
                            blocks_to_keep=5000,
                            allow_cors=False,
                            metrics=MetricsOptions(
                                enabled=True,
                                host="0.0.0.0",
                                port=9208,
                                bds_refresh_seconds=7.5,
                            ),
                            transaction_trace_logging=True,
                            app_logging=AppLoggingOptions(
                                level="warning",
                                json_logging=True,
                                rotation_hours=4,
                                retention_days=12,
                                max_file_mb=20,
                                max_total_mb=100,
                            ),
                            simulation=SimulationOptions(
                                enabled=False,
                                max_concurrency=4,
                                timeout_ms=4500,
                                max_chi=750000,
                            ),
                            bds=BdsOptions(
                                host="postgres",
                                port=5544,
                                database="xian_index",
                                user="indexer",
                                password_file="/run/secrets/custom",
                                pool_min_size=2,
                                pool_max_size=6,
                                statement_timeout_ms=5000,
                                acquire_timeout_ms=15000,
                                application_name="xian-bds-test",
                                queue_max_size=321,
                                catchup_enabled=False,
                                catchup_poll_seconds=2.5,
                                rpc_url="http://rpc.internal:26657",
                                spool_dir="/var/lib/xian/bds-spool",
                                spool_warn_entries=512,
                                spool_warn_bytes=1_073_741_824,
                                disk_free_warn_bytes=4_294_967_296,
                            ),
                        ),
                    )
                )

            rendered_config = load_toml(config_path)
            rendered_xian_config = load_toml(home / "config" / "xian.toml")
            rendered_validator_key = json.loads(
                (home / "config" / "priv_validator_key.json").read_text(encoding="utf-8")
            )
            rendered_genesis = json.loads(
                (home / "config" / "genesis.json").read_text(encoding="utf-8")
            )

            self.assertEqual(rendered_config["moniker"], "updated-node")
            self.assertEqual(
                rendered_config["p2p"]["seeds"],
                "seed-1@127.0.0.1:26656",
            )
            self.assertEqual(
                rendered_config["p2p"]["persistent_peers"],
                "peer-1@127.0.0.1:26656",
            )
            self.assertEqual(rendered_config["rpc"]["laddr"], "tcp://0.0.0.0:30057")
            self.assertEqual(rendered_config["p2p"]["laddr"], "tcp://0.0.0.0:30056")
            self.assertEqual(rendered_config["db_backend"], "rocksdb")
            self.assertEqual(rendered_config["db_dir"], "custom-data")
            self.assertEqual(rendered_config["rpc"]["cors_allowed_origins"], [])
            self.assertEqual(rendered_config["statesync"], {"enable": False})
            self.assertNotIn("xian", rendered_config)
            self.assertTrue(rendered_xian_config["pruning_enabled"])
            self.assertEqual(rendered_xian_config["blocks_to_keep"], 5000)
            self.assertNotIn("tracer_mode", rendered_xian_config)
            self.assertTrue(rendered_xian_config["metrics_enabled"])
            self.assertEqual(rendered_xian_config["metrics_host"], "0.0.0.0")
            self.assertEqual(rendered_xian_config["metrics_port"], 9208)
            self.assertEqual(rendered_xian_config["metrics_bds_refresh_seconds"], 7.5)
            self.assertTrue(rendered_xian_config["transaction_trace_logging"])
            self.assertEqual(rendered_xian_config["app_log_level"], "WARNING")
            self.assertTrue(rendered_xian_config["app_log_json"])
            self.assertEqual(rendered_xian_config["app_log_rotation_hours"], 4)
            self.assertEqual(rendered_xian_config["app_log_retention_days"], 12)
            self.assertEqual(rendered_xian_config["app_log_max_file_mb"], 20)
            self.assertEqual(rendered_xian_config["app_log_max_total_mb"], 100)
            self.assertFalse(rendered_xian_config["simulation_enabled"])
            self.assertEqual(rendered_xian_config["simulation_max_concurrency"], 4)
            self.assertEqual(rendered_xian_config["simulation_timeout_ms"], 4500)
            self.assertEqual(rendered_xian_config["simulation_max_chi"], 750000)
            self.assertNotIn("tx_fee_mode", rendered_xian_config)
            self.assertEqual(rendered_xian_config["bds"]["host"], "postgres")
            self.assertEqual(rendered_xian_config["bds"]["port"], 5544)
            self.assertEqual(rendered_xian_config["bds"]["database"], "xian_index")
            self.assertEqual(rendered_xian_config["bds"]["user"], "indexer")
            self.assertEqual(rendered_xian_config["bds"]["password_file"], "/run/secrets/custom")
            self.assertEqual(rendered_xian_config["bds"]["pool_min_size"], 2)
            self.assertEqual(rendered_xian_config["bds"]["pool_max_size"], 6)
            self.assertEqual(rendered_xian_config["bds"]["statement_timeout_ms"], 5000)
            self.assertEqual(rendered_xian_config["bds"]["acquire_timeout_ms"], 15000)
            self.assertEqual(
                rendered_xian_config["bds"]["application_name"],
                "xian-bds-test",
            )
            self.assertEqual(rendered_xian_config["bds"]["queue_max_size"], 321)
            self.assertFalse(rendered_xian_config["bds"]["catchup_enabled"])
            self.assertEqual(rendered_xian_config["bds"]["catchup_poll_seconds"], 2.5)
            self.assertEqual(
                rendered_xian_config["bds"]["rpc_url"],
                "http://rpc.internal:26657",
            )
            self.assertEqual(
                rendered_xian_config["bds"]["spool_dir"],
                "/var/lib/xian/bds-spool",
            )
            self.assertEqual(rendered_xian_config["bds"]["spool_warn_entries"], 512)
            self.assertEqual(
                rendered_xian_config["bds"]["spool_warn_bytes"],
                1_073_741_824,
            )
            self.assertEqual(
                rendered_xian_config["bds"]["disk_free_warn_bytes"],
                4_294_967_296,
            )
            self.assertEqual(rendered_genesis["chain_id"], "xian-mainnet-next")
            self.assertIn("address", rendered_validator_key)
            self.assertEqual(
                result["config_path"],
                str(home / "config" / "config.toml"),
            )
            self.assertEqual(
                result["xian_config_path"],
                str(home / "config" / "xian.toml"),
            )
            self.assertEqual(result["p2p_seeds"], ["seed-1@127.0.0.1:26656"])
            self.assertEqual(
                result["p2p_persistent_peers"],
                ["peer-1@127.0.0.1:26656"],
            )

    def test_configure_existing_home_accepts_options_object(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            home = tmp_path / ".cometbft"
            config_path = home / "config" / "config.toml"
            config_path.parent.mkdir(parents=True)

            existing_config = load_toml_string(
                """
version = "0.39.3"
proxy_app = "unix:///tmp/abci.sock"
moniker = "initial-node"
db_backend = "goleveldb"
db_dir = "data"
log_level = "info"
log_format = "plain"
genesis_file = "config/genesis.json"
priv_validator_key_file = "config/priv_validator_key.json"
priv_validator_state_file = "data/priv_validator_state.json"
node_key_file = "config/node_key.json"
abci = "socket"
filter_peers = false

[rpc]
laddr = "tcp://127.0.0.1:26657"
cors_allowed_origins = ["*"]

[p2p]
laddr = "tcp://0.0.0.0:26656"
seeds = ""
""".strip()
            )
            write_toml(config_path, existing_config)

            result = configure_existing_home(
                ExistingHomeOptions(
                    moniker="updated-node",
                    validator_private_key_hex=(
                        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
                    ),
                    home=home,
                    p2p_seeds=["seed-1@127.0.0.1:26656"],
                    node_config=NodeConfigOptions(
                        moniker="updated-node",
                        allow_cors=False,
                        metrics=MetricsOptions(port=9208),
                        bds=BdsOptions(
                            application_name="xian-bds-test",
                        ),
                    ),
                )
            )

            rendered_config = load_toml(config_path)
            rendered_xian_config = load_toml(home / "config" / "xian.toml")
            self.assertEqual(rendered_config["moniker"], "updated-node")
            self.assertEqual(
                rendered_config["p2p"]["seeds"],
                "seed-1@127.0.0.1:26656",
            )
            self.assertEqual(rendered_config["rpc"]["cors_allowed_origins"], [])
            self.assertNotIn("xian", rendered_config)
            self.assertEqual(rendered_xian_config["metrics_port"], 9208)
            self.assertEqual(
                rendered_xian_config["bds"]["application_name"],
                "xian-bds-test",
            )
            self.assertEqual(result["p2p_seeds"], ["seed-1@127.0.0.1:26656"])

    def test_configure_existing_home_rewrites_secret_files_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir) / ".cometbft"
            config_path = home / "config" / "config.toml"
            config_path.parent.mkdir(parents=True)
            write_toml(
                config_path,
                load_toml_string(
                    """
version = "0.39.3"
proxy_app = "unix:///tmp/abci.sock"
moniker = "initial-node"
db_backend = "goleveldb"
db_dir = "data"
log_level = "info"
log_format = "plain"
genesis_file = "config/genesis.json"
priv_validator_key_file = "config/priv_validator_key.json"
priv_validator_state_file = "data/priv_validator_state.json"
node_key_file = "config/node_key.json"
abci = "socket"
filter_peers = false

[rpc]
laddr = "tcp://127.0.0.1:26657"
cors_allowed_origins = ["*"]

[p2p]
laddr = "tcp://0.0.0.0:26656"
seeds = ""
""".strip()
                ),
            )
            # Homes written by older releases kept secrets world-readable.
            for name in ("priv_validator_key.json", "xian.toml"):
                legacy = home / "config" / name
                legacy.write_text("{}", encoding="utf-8")
                legacy.chmod(0o644)

            previous_umask = os.umask(0o022)
            try:
                result = configure_existing_home(
                    ExistingHomeOptions(
                        moniker="updated-node",
                        validator_private_key_hex="01" * 32,
                        home=home,
                        p2p_seeds=["seed-1@127.0.0.1:26656"],
                        node_config=NodeConfigOptions(
                            moniker="updated-node",
                            bds=BdsOptions(dsn="postgresql://indexer@db/xian"),
                        ),
                    )
                )
            finally:
                os.umask(previous_umask)

            for path in (result["priv_validator_key_path"], result["xian_config_path"]):
                with self.subTest(path=path):
                    self.assertEqual(stat.S_IMODE(Path(path).stat().st_mode), 0o600)

    def test_configure_existing_home_accepts_network_first_genesis_source(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            home = tmp_path / ".cometbft"
            config_path = home / "config" / "config.toml"
            config_path.parent.mkdir(parents=True)

            existing_config = load_toml_string(
                """
version = "0.39.3"
proxy_app = "unix:///tmp/abci.sock"
moniker = "initial-node"
db_backend = "goleveldb"
db_dir = "data"
log_level = "info"
log_format = "plain"
genesis_file = "config/genesis.json"
priv_validator_key_file = "config/priv_validator_key.json"
priv_validator_state_file = "data/priv_validator_state.json"
node_key_file = "config/node_key.json"
abci = "socket"
filter_peers = false

[rpc]
laddr = "tcp://127.0.0.1:26657"
cors_allowed_origins = ["*"]

[p2p]
laddr = "tcp://0.0.0.0:26656"
seeds = ""

[consensus]
create_empty_blocks = false
create_empty_blocks_interval = "0s"

[instrumentation]
prometheus = false
""".strip()
            )
            write_toml(config_path, existing_config)

            configs_dir = tmp_path / "xian-configs"
            network_dir = configs_dir / "networks" / "mainnet"
            network_dir.mkdir(parents=True)
            genesis_payload = {
                "chain_id": "xian-local-1",
                "validators": [],
                "abci_genesis": {},
            }
            (network_dir / "genesis.json").write_text(
                json.dumps(genesis_payload),
                encoding="utf-8",
            )

            with patch.dict("os.environ", {"XIAN_CONFIGS_DIR": str(configs_dir)}):
                result = configure_existing_home(
                    ExistingHomeOptions(
                        moniker="updated-node",
                        home=home,
                        genesis_source="mainnet",
                    )
                )

            rendered_genesis = json.loads(
                (home / "config" / "genesis.json").read_text(encoding="utf-8")
            )
            self.assertEqual(rendered_genesis["chain_id"], "xian-local-1")
            self.assertEqual(
                result["genesis_path"],
                str(home / "config" / "genesis.json"),
            )

    def test_configure_existing_home_accepts_genesis_payload(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            home = tmp_path / ".cometbft"
            config_path = home / "config" / "config.toml"
            config_path.parent.mkdir(parents=True)

            existing_config = load_toml_string(
                """
version = "0.39.3"
proxy_app = "unix:///tmp/abci.sock"
moniker = "initial-node"
db_backend = "goleveldb"
db_dir = "data"
log_level = "info"
log_format = "plain"
genesis_file = "config/genesis.json"
priv_validator_key_file = "config/priv_validator_key.json"
priv_validator_state_file = "data/priv_validator_state.json"
node_key_file = "config/node_key.json"
abci = "socket"
filter_peers = false

[rpc]
laddr = "tcp://127.0.0.1:26657"
cors_allowed_origins = ["*"]

[p2p]
laddr = "tcp://0.0.0.0:26656"
seeds = ""

[consensus]
create_empty_blocks = false
create_empty_blocks_interval = "0s"

[instrumentation]
prometheus = false
""".strip()
            )
            write_toml(config_path, existing_config)

            genesis_payload = {
                "chain_id": "xian-preset-1",
                "validators": [
                    {
                        "address": "ABC",
                        "pub_key": {
                            "type": "tendermint/PubKeyEd25519",
                            "value": "pub",
                        },
                        "power": "10",
                        "name": "",
                    }
                ],
                "abci_genesis": {
                    "hash": "abc",
                    "number": "0",
                    "genesis": [],
                    "origin": {"sender": "", "signature": ""},
                },
            }

            result = configure_existing_home(
                ExistingHomeOptions(
                    moniker="updated-node",
                    home=home,
                    genesis_payload=genesis_payload,
                )
            )

            rendered_genesis = json.loads(
                (home / "config" / "genesis.json").read_text(encoding="utf-8")
            )
            self.assertEqual(rendered_genesis, genesis_payload)
            self.assertEqual(
                result["genesis_path"],
                str(home / "config" / "genesis.json"),
            )


if __name__ == "__main__":
    unittest.main()


PINNED_GENESIS = {"chain_id": "xian-pinned-1", "validators": [], "abci_genesis": {}}
OTHER_GENESIS = PINNED_GENESIS | {"chain_id": "xian-other-1"}


class ConfigureExistingHomeGenesisPinTests(unittest.TestCase):
    """configure_existing_home (xian-configure-node, xian-deploy) enforces genesis_sha256."""

    def _home(self, tmp_path: Path, *, installed_genesis: dict | None = None) -> Path:
        home = tmp_path / ".cometbft"
        (home / "config").mkdir(parents=True)
        initial = render_node_configs(options=NodeConfigOptions(moniker="initial"))
        write_toml(home / "config" / "config.toml", initial["cometbft"])
        if installed_genesis is not None:
            write_json(home / "config" / "genesis.json", installed_genesis)
        return home

    def _files(self, home: Path) -> dict[Path, bytes]:
        return {path: path.read_bytes() for path in home.rglob("*") if path.is_file()}

    def _sources(self, tmp_path: Path) -> dict[str, dict]:
        configs_dir = tmp_path / "xian-configs"
        (configs_dir / "networks" / "other").mkdir(parents=True)
        (configs_dir / "networks" / "other" / "genesis.json").write_text(
            json.dumps(OTHER_GENESIS, indent=2), encoding="utf-8"
        )
        return {
            "genesis_source": {"genesis_source": "other"},
            "genesis_payload": {"genesis_payload": OTHER_GENESIS},
        }

    def test_a_requested_genesis_other_than_the_pin_is_refused_before_any_write(self):
        for name in ("genesis_source", "genesis_payload"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir)
                home = self._home(tmp_path)
                request = self._sources(tmp_path)[name]
                before = self._files(home)
                env = {"XIAN_CONFIGS_DIR": str(tmp_path / "xian-configs")}
                with patch.dict("os.environ", env), self.assertRaises(GenesisIntegrityError):
                    configure_existing_home(
                        ExistingHomeOptions(
                            moniker="pinned",
                            home=home,
                            expected_genesis_sha256=genesis_sha256(PINNED_GENESIS),
                            **request,
                        )
                    )
                self.assertEqual(self._files(home), before)

    def test_the_pinned_genesis_is_installed(self):
        for name in ("genesis_source", "genesis_payload"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir)
                home = self._home(tmp_path)
                request = self._sources(tmp_path)[name]
                env = {"XIAN_CONFIGS_DIR": str(tmp_path / "xian-configs")}
                with patch.dict("os.environ", env):
                    result = configure_existing_home(
                        ExistingHomeOptions(
                            moniker="pinned",
                            home=home,
                            expected_genesis_sha256=genesis_sha256(OTHER_GENESIS),
                            **request,
                        )
                    )
                installed = json.loads(Path(result["genesis_path"]).read_text())
                self.assertEqual(installed, OTHER_GENESIS)

    def test_without_a_requested_genesis_the_installed_one_is_checked(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = self._home(Path(tmp_dir), installed_genesis=OTHER_GENESIS)
            before = self._files(home)
            with self.assertRaises(GenesisIntegrityError):
                configure_existing_home(
                    ExistingHomeOptions(
                        moniker="pinned",
                        home=home,
                        expected_genesis_sha256=genesis_sha256(PINNED_GENESIS),
                    )
                )
            self.assertEqual(self._files(home), before)
            result = configure_existing_home(
                ExistingHomeOptions(
                    moniker="pinned",
                    home=home,
                    expected_genesis_sha256=genesis_sha256(OTHER_GENESIS),
                )
            )
            self.assertIsNone(result["genesis_path"])
