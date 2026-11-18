import os
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from xian.services.bds.config import BdsConfig


class BdsConfigTests(unittest.TestCase):
    def test_missing_and_invalid_settings_follow_config_defaults(self):
        @dataclass(frozen=True)
        class CustomDefaults(BdsConfig):
            host: str = "local-db"
            port: int = 5544
            application_name: str = "custom-indexer"
            catchup_enabled: bool = False
            catchup_poll_seconds: float = 2.5

        for settings in ({}, {"host": "", "port": "invalid", "catchup_poll_seconds": "nan"}):
            with self.subTest(settings=settings):
                self.assertEqual(
                    CustomDefaults.from_runtime_settings({"bds": settings}),
                    CustomDefaults(),
                )

    def test_runtime_settings_are_independent_of_the_environment(self):
        with patch.dict(os.environ, {"XIAN_BDS_HOST": "ambient-db"}, clear=True):
            config = BdsConfig.from_runtime_settings({"bds": {"host": "configured-db"}})
        self.assertEqual(config.host, "configured-db")

    def test_unknown_bds_settings_are_rejected_without_echoing_values(self):
        with self.assertRaises(ValueError) as raised:
            BdsConfig.from_runtime_settings({"bds": {"typo": "secret", "another": "private"}})
        self.assertEqual(str(raised.exception), "unknown bds key(s): another, typo")

    def test_dsn_carrying_a_password_is_rejected(self):
        for dsn in (
            "postgresql://user:pw@db:5432/xian",
            "postgres://user:@db/xian",
            "postgresql://user@db/xian?sslmode=require&password=pw",
        ):
            with self.subTest(dsn=dsn):
                with self.assertRaisesRegex(ValueError, "bds.dsn must not contain a password"):
                    BdsConfig.from_runtime_settings({"bds": {"dsn": dsn}})

    def test_dsn_without_password_is_kept(self):
        config = BdsConfig.from_runtime_settings(
            {"bds": {"dsn": "postgresql://indexer@db:5432/xian?sslmode=require"}}
        )

        self.assertEqual(config.dsn, "postgresql://indexer@db:5432/xian?sslmode=require")

    def test_password_is_read_from_the_password_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "bds-password"
            path.write_text("s3cr=t value\n", encoding="utf-8")
            config = BdsConfig.from_runtime_settings({"bds": {"password_file": str(path)}})

            self.assertEqual(config.password_file, str(path))
            self.assertEqual(config.read_password(), "s3cr=t value")

    def test_without_password_file_the_password_is_empty(self):
        config = BdsConfig.from_runtime_settings({"bds": {"password_file": ""}})

        self.assertIsNone(config.password_file)
        self.assertEqual(config.read_password(), "")

    def test_unreadable_password_file_names_the_setting(self):
        config = BdsConfig(password_file="/nonexistent/bds-password")

        with self.assertRaisesRegex(
            ValueError, "bds.password_file /nonexistent/bds-password cannot be read"
        ):
            config.read_password()

    def test_invalid_nonfinite_numeric_settings_use_existing_defaults(self):
        for value in (float("nan"), float("inf"), float("-inf"), "nan", "inf", "-inf"):
            with self.subTest(value=value):
                config = BdsConfig.from_runtime_settings(
                    {"bds": {"port": value, "catchup_poll_seconds": value}},
                )
                self.assertEqual(config.port, 5432)
                self.assertEqual(config.catchup_poll_seconds, 1.0)

    def test_overflowing_poll_interval_uses_existing_default(self):
        config = BdsConfig.from_runtime_settings({"bds": {"catchup_poll_seconds": 10**1000}})
        self.assertEqual(config.catchup_poll_seconds, 1.0)

    def test_non_mapping_bds_settings_use_defaults(self):
        self.assertEqual(BdsConfig.from_runtime_settings({"bds": "invalid"}), BdsConfig())

    def test_from_runtime_settings_prefers_nested_config(self):
        config = BdsConfig.from_runtime_settings(
            {
                "bds": {
                    "dsn": "",
                    "host": "db.internal",
                    "port": 5544,
                    "database": "xian_index",
                    "user": "indexer",
                    "password_file": "/run/secrets/xian_bds_password",
                    "pool_min_size": 2,
                    "pool_max_size": 6,
                    "statement_timeout_ms": 5000,
                    "application_name": "xian-bds-test",
                    "queue_max_size": 42,
                    "catchup_enabled": False,
                    "catchup_poll_seconds": 2.5,
                    "rpc_url": "http://127.0.0.1:26657",
                    "spool_dir": "/var/lib/xian/bds-spool",
                    "spool_warn_entries": 512,
                    "spool_warn_bytes": 1073741824,
                    "disk_free_warn_bytes": 4294967296,
                }
            },
        )

        self.assertEqual(config.host, "db.internal")
        self.assertEqual(config.port, 5544)
        self.assertEqual(config.database, "xian_index")
        self.assertEqual(config.user, "indexer")
        self.assertEqual(config.password_file, "/run/secrets/xian_bds_password")
        self.assertEqual(config.pool_min_size, 2)
        self.assertEqual(config.pool_max_size, 6)
        self.assertEqual(config.statement_timeout_ms, 5000)
        self.assertEqual(config.application_name, "xian-bds-test")
        self.assertEqual(config.queue_max_size, 42)
        self.assertFalse(config.catchup_enabled)
        self.assertEqual(config.catchup_poll_seconds, 2.5)
        self.assertEqual(config.rpc_url, "http://127.0.0.1:26657")
        self.assertEqual(config.spool_dir, "/var/lib/xian/bds-spool")
        self.assertEqual(config.spool_warn_entries, 512)
        self.assertEqual(config.spool_warn_bytes, 1_073_741_824)
        self.assertEqual(config.disk_free_warn_bytes, 4_294_967_296)

    def test_from_runtime_settings_handles_invalid_catchup_poll_seconds(self):
        config = BdsConfig.from_runtime_settings({"bds": {"catchup_poll_seconds": "not-a-float"}})

        self.assertEqual(config.catchup_poll_seconds, 1.0)


if __name__ == "__main__":
    unittest.main()
