import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xian.state_export import (
    build_exported_state,
    export_state,
    hash_state_changes,
)
from xian.state_root import compute_exported_state_root


class StateExportTests(unittest.TestCase):
    def test_hash_state_changes_supports_bytes(self):
        digest = hash_state_changes(
            [{"key": "con_seed.binary", "value": b"\x01\x02"}]
        )

        self.assertEqual(len(digest), 64)

    def test_build_exported_state_sorts_entries_and_signs_origin(self):
        founder_private_key = (
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        )
        contract_state = {
            "con_b.value": 2,
            "con_a.value": 1,
            "con_none.value": None,
        }
        run_state = {
            "__n.alice": 7,
            "other": 9,
        }

        exported_state = build_exported_state(
            founder_private_key=founder_private_key,
            contract_state=contract_state,
            run_state=run_state,
            latest_block_hash=b"",
            latest_block_height=12,
        )

        self.assertEqual(
            exported_state["hash"],
            compute_exported_state_root(exported_state).hex(),
        )
        self.assertEqual(exported_state["number"], 12)
        self.assertEqual(
            [item["key"] for item in exported_state["genesis"]],
            ["con_a.value", "con_b.value"],
        )
        self.assertEqual(
            exported_state["nonces"],
            [{"key": "alice", "value": 7}],
        )
        self.assertTrue(exported_state["origin"]["sender"])
        self.assertTrue(exported_state["origin"]["signature"])

    def test_export_state_writes_encoded_payload(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir)
            expected_state = {
                "genesis": [{"key": "con_a.value", "value": 1}],
                "nonces": [{"key": "alice", "value": 7}],
            }
            expected_hash = compute_exported_state_root(expected_state)
            with patch(
                "xian.state_export.fetch_filebased_state",
                return_value=(
                    {"con_a.value": 1},
                    {"__n.alice": 7},
                ),
            ):
                with patch(
                    "xian.state_export.get_latest_block_hash",
                    return_value=expected_hash,
                ):
                    with patch(
                        "xian.state_export.get_latest_block_height",
                        return_value=9,
                    ):
                        output_path = export_state(output_dir=output_dir)

            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(output_path, output_dir / "exported_state.json")
            self.assertEqual(payload["hash"], expected_hash.hex())
            self.assertEqual(payload["number"], 9)
            self.assertEqual(
                payload["genesis"],
                [{"key": "con_a.value", "value": 1}],
            )


if __name__ == "__main__":
    unittest.main()
