from unittest.mock import patch

from contracting.execution.runtime import rt

from xian.shielded_preverify import (
    ShieldedPreverifyStats,
    build_verification_request,
    warm_shielded_proof_cache,
)


class _Driver:
    def __init__(self, values):
        self.values = values

    def get_var(self, contract, variable, arguments=None):
        return self.values.get((contract, variable, tuple(arguments or [])))


def test_warm_shielded_proof_cache_binds_supplied_driver_for_zk_lookup():
    driver = object()
    previous_driver = object()
    request = {
        "vk_id": "demo",
        "proof_hex": "0xabcd",
        "public_inputs": ["0x" + "00" * 32],
    }
    rt.env["__Driver"] = previous_driver

    try:
        with (
            patch(
                "xian.shielded_preverify.zk_bridge.is_available",
                return_value=True,
            ),
            patch(
                "xian.shielded_preverify.build_verification_request",
                return_value=request,
            ),
            patch(
                "xian.shielded_preverify.zk_bridge.warm_verified_proofs"
            ) as warm_verified_proofs,
        ):

            def _warm(requests):
                assert requests == [request]
                assert rt.env.get("__Driver") is driver
                return [True]

            warm_verified_proofs.side_effect = _warm
            stats = warm_shielded_proof_cache(
                driver=driver,
                txs=[{"payload": {"kwargs": {"proof_hex": "0xabcd"}}}],
            )

        assert stats == ShieldedPreverifyStats(
            candidate_count=1,
            verified_count=1,
            failed_count=0,
        )
        assert rt.env.get("__Driver") is previous_driver
    finally:
        rt.env.pop("__Driver", None)


def test_warm_shielded_proof_cache_noops_when_zk_bridge_is_unavailable():
    with patch(
        "xian.shielded_preverify.zk_bridge.is_available", return_value=False
    ):
        stats = warm_shielded_proof_cache(
            driver=object(),
            txs=[{"payload": {"kwargs": {"proof_hex": "0xabcd"}}}],
        )

    assert stats == ShieldedPreverifyStats()


def test_warm_shielded_proof_cache_counts_failed_preverification():
    request = {
        "vk_id": "demo",
        "proof_hex": "0xabcd",
        "public_inputs": ["0x" + "00" * 32],
    }

    with (
        patch(
            "xian.shielded_preverify.zk_bridge.is_available", return_value=True
        ),
        patch(
            "xian.shielded_preverify.build_verification_request",
            return_value=request,
        ),
        patch(
            "xian.shielded_preverify.zk_bridge.warm_verified_proofs",
            return_value=[True, False],
        ),
    ):
        stats = warm_shielded_proof_cache(
            driver=object(),
            txs=[
                {"payload": {"kwargs": {"proof_hex": "0x01"}}},
                {"payload": {"kwargs": {"proof_hex": "0x02"}}},
            ],
        )

    assert stats == ShieldedPreverifyStats(
        candidate_count=2,
        verified_count=1,
        failed_count=1,
    )


def test_warm_shielded_proof_cache_skips_malformed_candidates():
    with (
        patch(
            "xian.shielded_preverify.zk_bridge.is_available", return_value=True
        ),
        patch(
            "xian.shielded_preverify.build_verification_request",
            side_effect=AssertionError("bad shielded payload"),
        ),
        patch(
            "xian.shielded_preverify.zk_bridge.warm_verified_proofs"
        ) as warm_verified_proofs,
    ):
        stats = warm_shielded_proof_cache(
            driver=object(),
            txs=[{"payload": {"kwargs": {"proof_hex": "0x01"}}}],
        )

    assert stats == ShieldedPreverifyStats()
    warm_verified_proofs.assert_not_called()


def test_build_verification_request_for_deposit_defaults_empty_payloads():
    driver = _Driver({("con_token", "vk_ids", ("deposit",)): "deposit-vk"})
    tx = {
        "payload": {
            "contract": "con_token",
            "function": "deposit_shielded",
            "sender": "sender",
            "kwargs": {
                "proof_hex": "0xproof",
                "old_root": "0xroot",
                "amount": 10,
                "output_commitments": ["0xcommit"],
            },
        }
    }

    with (
        patch(
            "xian.shielded_preverify.zk_bridge.shielded_output_payload_hashes",
            return_value=["0xpayloadhash"],
        ) as payload_hashes,
        patch(
            "xian.shielded_preverify.zk_bridge.shielded_deposit_public_inputs",
            return_value=["0xpublic"],
        ) as deposit_inputs,
    ):
        request = build_verification_request(driver, tx)

    assert request == {
        "vk_id": "deposit-vk",
        "proof_hex": "0xproof",
        "public_inputs": ["0xpublic"],
    }
    payload_hashes.assert_called_once_with([""])
    deposit_inputs.assert_called_once_with(
        "con_token",
        "0xroot",
        10,
        ["0xcommit"],
        ["0xpayloadhash"],
    )
