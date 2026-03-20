import unittest

from xian.services.bds.config import BdsConfig


class BdsConfigTests(unittest.TestCase):
    def test_from_runtime_settings_prefers_nested_config(self):
        config = BdsConfig.from_runtime_settings(
            {
                "bds": {
                    "dsn": "",
                    "host": "db.internal",
                    "port": 5544,
                    "database": "xian_index",
                    "user": "indexer",
                    "password": "secret",
                    "pool_min_size": 2,
                    "pool_max_size": 6,
                    "statement_timeout_ms": 5000,
                    "application_name": "xian-bds-test",
                    "queue_max_size": 42,
                    "spool_dir": "/var/lib/xian/bds-spool",
                    "spool_warn_entries": 512,
                    "spool_warn_bytes": 1073741824,
                    "disk_free_warn_bytes": 4294967296,
                }
            },
            {
                "XIAN_BDS_HOST": "ignored-host",
                "XIAN_BDS_USER": "ignored-user",
            },
        )

        self.assertEqual(config.host, "db.internal")
        self.assertEqual(config.port, 5544)
        self.assertEqual(config.database, "xian_index")
        self.assertEqual(config.user, "indexer")
        self.assertEqual(config.password, "secret")
        self.assertEqual(config.pool_min_size, 2)
        self.assertEqual(config.pool_max_size, 6)
        self.assertEqual(config.statement_timeout_ms, 5000)
        self.assertEqual(config.application_name, "xian-bds-test")
        self.assertEqual(config.queue_max_size, 42)
        self.assertEqual(config.spool_dir, "/var/lib/xian/bds-spool")
        self.assertEqual(config.spool_warn_entries, 512)
        self.assertEqual(config.spool_warn_bytes, 1_073_741_824)
        self.assertEqual(config.disk_free_warn_bytes, 4_294_967_296)

    def test_from_runtime_settings_falls_back_to_environment(self):
        config = BdsConfig.from_runtime_settings(
            {},
            {
                "XIAN_BDS_DSN": "",
                "XIAN_BDS_HOST": "postgres",
                "XIAN_BDS_PORT": "5433",
                "XIAN_BDS_DATABASE": "xian",
                "XIAN_BDS_USER": "xian",
                "XIAN_BDS_PASSWORD": "xian",
                "XIAN_BDS_POOL_MIN_SIZE": "3",
                "XIAN_BDS_POOL_MAX_SIZE": "9",
                "XIAN_BDS_STATEMENT_TIMEOUT_MS": "2500",
                "XIAN_BDS_APPLICATION_NAME": "xian-bds-stack",
                "XIAN_BDS_QUEUE_MAX_SIZE": "256",
                "XIAN_BDS_SPOOL_DIR": "/tmp/xian-bds-spool",
                "XIAN_BDS_SPOOL_WARN_ENTRIES": "1024",
                "XIAN_BDS_SPOOL_WARN_BYTES": "2147483648",
                "XIAN_BDS_DISK_FREE_WARN_BYTES": "8589934592",
            },
        )

        self.assertEqual(config.host, "postgres")
        self.assertEqual(config.port, 5433)
        self.assertEqual(config.database, "xian")
        self.assertEqual(config.user, "xian")
        self.assertEqual(config.password, "xian")
        self.assertEqual(config.pool_min_size, 3)
        self.assertEqual(config.pool_max_size, 9)
        self.assertEqual(config.statement_timeout_ms, 2500)
        self.assertEqual(config.application_name, "xian-bds-stack")
        self.assertEqual(config.queue_max_size, 256)
        self.assertEqual(config.spool_dir, "/tmp/xian-bds-spool")
        self.assertEqual(config.spool_warn_entries, 1024)
        self.assertEqual(config.spool_warn_bytes, 2_147_483_648)
        self.assertEqual(config.disk_free_warn_bytes, 8_589_934_592)


if __name__ == "__main__":
    unittest.main()
