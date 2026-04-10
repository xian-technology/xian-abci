from unittest.mock import patch

from contracting.execution.runtime import rt

from xian.shielded_preverify import (
    ShieldedPreverifyStats,
    warm_shielded_proof_cache,
)


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
