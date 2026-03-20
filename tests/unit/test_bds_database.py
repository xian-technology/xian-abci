import unittest

from xian.services.bds.config import BdsConfig
from xian.services.bds.database import DB


class BdsDatabaseTests(unittest.TestCase):
    def test_pool_kwargs_use_dsn_when_present(self):
        db = DB(
            BdsConfig(
                dsn="postgresql://user:pass@db.example:5432/xian",
                pool_min_size=2,
                pool_max_size=8,
                statement_timeout_ms=1500,
                application_name="xian-bds-test",
            )
        )

        kwargs = db._pool_kwargs()

        self.assertEqual(
            kwargs["dsn"], "postgresql://user:pass@db.example:5432/xian"
        )
        self.assertEqual(kwargs["min_size"], 2)
        self.assertEqual(kwargs["max_size"], 8)
        self.assertEqual(
            kwargs["server_settings"]["application_name"], "xian-bds-test"
        )
        self.assertEqual(
            kwargs["server_settings"]["statement_timeout"], "1500ms"
        )
        self.assertNotIn("host", kwargs)

    def test_validate_database_name_rejects_invalid_names(self):
        with self.assertRaisesRegex(ValueError, "invalid database name"):
            DB._validate_database_name("xian;drop database")


if __name__ == "__main__":
    unittest.main()
