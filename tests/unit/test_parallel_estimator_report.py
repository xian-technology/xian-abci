import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xian.cli.parallel_estimator_report import (
    load_snapshots,
    main,
    summarize_snapshots,
)


class ParallelEstimatorReportTests(unittest.TestCase):
    def test_summarize_snapshots_ranks_unknown_shapes(self):
        summary = summarize_snapshots(
            [
                {
                    "recent_blocks": [
                        {
                            "metadata": {
                                "parallel_enabled": True,
                                "parallel_estimated_known_transactions": 3,
                                "parallel_estimated_unknown_transactions": 2,
                                "parallel_estimated_known_shapes": [
                                    {
                                        "contract": "currency",
                                        "function": "transfer",
                                        "count": 3,
                                    }
                                ],
                                "parallel_estimated_unknown_shapes": [
                                    {
                                        "contract": "members",
                                        "function": "join",
                                        "count": 1,
                                    },
                                    {
                                        "contract": "vault",
                                        "function": "deposit",
                                        "count": 1,
                                    },
                                ],
                            }
                        },
                        {
                            "metadata": {
                                "parallel_enabled": True,
                                "parallel_estimated_known_transactions": 0,
                                "parallel_estimated_unknown_transactions": 4,
                                "parallel_estimated_unknown_shapes": [
                                    {
                                        "contract": "members",
                                        "function": "join",
                                        "count": 4,
                                    }
                                ],
                            }
                        },
                    ]
                }
            ],
            limit=2,
        )

        self.assertEqual(summary["blocks_seen"], 2)
        self.assertEqual(summary["parallel_blocks"], 2)
        self.assertEqual(summary["estimated_known_transactions"], 3)
        self.assertEqual(summary["estimated_unknown_transactions"], 6)
        self.assertEqual(
            summary["unknown_shapes"],
            [
                {"contract": "members", "function": "join", "count": 5},
                {"contract": "vault", "function": "deposit", "count": 1},
            ],
        )
        self.assertEqual(
            summary["known_shapes"],
            [{"contract": "currency", "function": "transfer", "count": 3}],
        )

    def test_load_snapshots_reads_multiple_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first = Path(temp_dir) / "first.json"
            second = Path(temp_dir) / "second.json"
            first.write_text(
                json.dumps({"recent_blocks": []}), encoding="utf-8"
            )
            second.write_text(json.dumps([{"metadata": {}}]), encoding="utf-8")

            snapshots = load_snapshots([first, second])

        self.assertEqual(snapshots, [{"recent_blocks": []}, [{"metadata": {}}]])

    def test_main_emits_json_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "perf.json"
            path.write_text(
                json.dumps(
                    {
                        "recent_blocks": [
                            {
                                "metadata": {
                                    "parallel_enabled": True,
                                    "parallel_estimated_unknown_shapes": [
                                        {
                                            "contract": "foo",
                                            "function": "bar",
                                            "count": 2,
                                        },
                                        {
                                            "contract": "foo",
                                            "function": "bad_count",
                                            "count": "not-a-number",
                                        },
                                    ],
                                }
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with patch("builtins.print") as mocked_print:
                exit_code = main(["--json", str(path)])

        self.assertEqual(exit_code, 0)
        output = json.loads(mocked_print.call_args.args[0])
        self.assertEqual(
            output["unknown_shapes"],
            [{"contract": "foo", "function": "bar", "count": 2}],
        )


if __name__ == "__main__":
    unittest.main()
