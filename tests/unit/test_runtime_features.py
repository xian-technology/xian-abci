import unittest

from contracting.runtime_features import runtime_feature_state_key

from xian.runtime_features import resolve_runtime_features


class RuntimeFeatureTests(unittest.TestCase):
    def test_resolve_runtime_features_defaults_zk_false_without_state(self):
        resolution = resolve_runtime_features(genesis={"abci_genesis": {"genesis": []}})

        self.assertEqual(resolution.features["zk"], False)
        self.assertEqual(resolution.source, "default")

    def test_resolve_runtime_features_reads_genesis_state_key(self):
        resolution = resolve_runtime_features(
            genesis={
                "abci_genesis": {
                    "genesis": [
                        {
                            "key": runtime_feature_state_key("zk"),
                            "value": True,
                        }
                    ]
                }
            }
        )

        self.assertEqual(resolution.features["zk"], True)
        self.assertEqual(resolution.source, "genesis")

    def test_resolve_runtime_features_does_not_infer_from_contract_ir(self):
        resolution = resolve_runtime_features(
            genesis={
                "abci_genesis": {
                    "genesis": [
                        {
                            "key": "con_private.__xian_ir_v1__",
                            "value": '{"host_dependencies":[{"category":"zk"}]}',
                        }
                    ]
                }
            }
        )

        self.assertEqual(resolution.features["zk"], False)
        self.assertEqual(resolution.source, "default")


if __name__ == "__main__":
    unittest.main()
