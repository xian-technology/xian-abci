"""Real app-state fixtures plus an explicit native-inspector test double.

The Go inspector has separate native database tests and full network coverage;
this JSON test double lets Python exercise all cross-store binding checks.
"""

import io
import json
from email.message import Message
from pathlib import Path

from contracting.storage.driver import Driver

from xian.constants import Constants
from xian.evidence_policy import EVIDENCE_POLICY_KEY, EvidencePolicy
from xian.execution_policy import EXECUTION_POLICY_KEY, current_execution_policy
from xian.node_setup import write_json
from xian.state_root import compute_driver_state_root


def reference_config(home):
    (home / "config").mkdir(parents=True, exist_ok=True)
    path = home / "config/config.toml"
    if not path.exists():
        path.write_text('db_backend="goleveldb"\ndb_dir="data"\n')
    write_json(
        home / "config/genesis.json",
        {"chain_id": "xian-test-1", "initial_height": "1"},
        overwrite=True,
    )


def seed_application(storage_home):
    policy = EvidencePolicy.seed(1, nanos=1_700_000_000_123_456_789)
    with Driver(storage_home=storage_home, vm_profile="xian_vm_v2") as driver:
        driver.set(EXECUTION_POLICY_KEY, current_execution_policy())
        driver.set(EVIDENCE_POLICY_KEY, policy.to_state())
        driver.set("demo.value", 7)
        root = compute_driver_state_root(driver).hex()
        driver.set(
            Constants.LATEST_BLOCK_KEY,
            {"height": 123, "hash": root, "nanos": policy.growth_start_nanos},
        )
        driver.commit()
    return {
        "schema_version": 1,
        "chain_id": "xian-test-1",
        "height": "123",
        "app_hash": root,
        "block_hash": "b" * 64,
        "last_block_nanos": str(policy.growth_start_nanos),
        "evidence": policy.comet_json(),
        "block_store_base": "1",
        "block_store_height": "123",
        "history_first_verified": "1",
        "history_first_nanos": "1700000000000000000",
        "initial_height": "1",
    }


def fixture_inspector(db_dir, backend, genesis_file):
    assert Path(genesis_file).is_file()
    assert backend == "goleveldb"
    return json.loads((db_dir / "native-checkpoint.json").read_text())


class Response(io.BytesIO):
    def __init__(self, payload, *, content_length=None):
        super().__init__(payload)
        self.headers = Message()
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
