import json
import os

from contracting.compilation.compiler import ContractingCompiler
from loguru import logger
from xian_runtime_types.encoding import convert_dict

from xian.utils.encoding import hash_bytes


def build_contract_artifacts_from_source(s: dict):
    """Build canonical source and canonical runtime code for a contract."""

    contract_name = s["key"].split(".")[0]
    compiler = ContractingCompiler(module_name=contract_name)

    normalized_source = compiler.normalize_source(s["value"])
    transformed_code = compiler.parse_to_code(s["value"])

    logger.info(f"Built contract artifacts for {contract_name}")
    return normalized_source, transformed_code


def hash_from_state_changes(state_changes):
    """Generate a hash from a list of state changes."""
    # Convert state changes to a serializable format
    serialized_changes = []
    for change in state_changes:
        serialized_change = {
            "key": change["key"],
            "value": json.dumps(change["value"], sort_keys=True),
            # Note: We exclude comments from the hash as they don't affect state
        }
        serialized_changes.append(serialized_change)

    serialized_changes.sort(key=lambda x: x["key"])

    changes_str = json.dumps(serialized_changes, sort_keys=True)

    hash_obj = hash_bytes(changes_str.encode())
    if isinstance(hash_obj, bytes):
        return hash_obj.hex()
    return hash_obj


class StatePatchManager:
    def __init__(self, raw_driver):
        self.patches = {}
        self.raw_driver = raw_driver
        self.loaded = False

    def load_patches(self, patch_file_path):
        """Load all state patches from the specified JSON file."""
        if not os.path.exists(patch_file_path):
            logger.info(f"No state patches file found at {patch_file_path}")
            self.loaded = True
            return

        try:
            with open(patch_file_path, "r") as f:
                patch_data = json.load(f)

            # Convert string keys (block heights) to integers for easier comparison
            self.patches = {
                int(height): patches for height, patches in patch_data.items()
            }
            logger.info(f"Loaded patches for {len(self.patches)} blocks")
            self.loaded = True
        except Exception as e:
            logger.error(f"Error loading state patches: {e}")
            # Initialize empty to avoid errors
            self.patches = {}
            self.loaded = False

    def build_applied_patches_for_block(
        self, height
    ) -> tuple[str | None, list[dict]]:
        """Build the BDS-facing patch payload for a block without mutating state."""
        if not self.loaded or height not in self.patches:
            return None, []

        patches = self.patches[height]
        if not patches:
            return None, []

        applied_patches = []

        for patch in patches:
            key = patch["key"]
            comment = patch.get("comment", "No comment provided")
            applied_patch = {
                "key": key,
                "value": patch["value"],
                "comment": comment,
            }
            applied_patches.append(applied_patch)

            parts = key.split(".")
            if len(parts) > 1 and parts[1] == "__source__":
                contract_name = parts[0]
                try:
                    _, transformed_code = build_contract_artifacts_from_source(
                        patch
                    )
                    applied_patches.append(
                        {
                            "key": f"{contract_name}.__code__",
                            "value": transformed_code,
                            "comment": f"Canonical runtime code for {comment}",
                        }
                    )
                except Exception as e:
                    logger.error(
                        f"Failed to build contract artifacts for {contract_name}: {e}"
                    )
                    logger.error(
                        "Skipping derived contract patch and continuing"
                    )

        patch_hash = hash_from_state_changes(patches)
        return patch_hash, applied_patches

    def apply_patches_for_block(self, height, nanos) -> tuple[str | None, list]:
        """Apply any patches for the specified block height and return hash and applied patches."""
        if not self.loaded or height not in self.patches:
            return None, []

        patches = self.patches[height]
        if not patches:
            return None, []

        logger.info(f"Applying {len(patches)} state patches for block {height}")

        patch_hash, applied_patches = self.build_applied_patches_for_block(
            height
        )

        for patch in patches:
            key = patch["key"]
            value = patch["value"]
            comment = patch.get("comment", "No comment provided")

            logger.info(f"Applying patch: {key} -> {value} ({comment})")

            # Check if this is a contract code patch
            # Contract code key format: con_contract_name.__code__
            parts = key.split(".")
            if len(parts) > 1 and parts[1] == "__source__":
                contract_name = parts[0]

                logger.info(
                    f"Processing contract source patch: {contract_name}"
                )

                try:
                    normalized_source, transformed_code = (
                        build_contract_artifacts_from_source(patch)
                    )

                    self.raw_driver.set(key, normalized_source)
                    self.raw_driver.set(
                        f"{contract_name}.__code__", transformed_code
                    )

                    logger.info(
                        f"Contract source patch applied for {contract_name}"
                    )
                except Exception as e:
                    # Log the error but continue processing other patches
                    logger.error(
                        f"Failed to build contract artifacts for {contract_name}: {e}"
                    )
                    logger.error(
                        "Skipping this patch and continuing with others"
                    )
            else:
                # Handle all other (non-code) patches
                # Convert dict values if needed
                if isinstance(value, dict):
                    value = convert_dict(value)

                # Apply the patch to state
                self.raw_driver.set(key, value)

        # Finalize changes
        self.raw_driver.hard_apply(nanos)

        logger.info(f"Generated hash for state patches: {patch_hash}")

        return patch_hash, applied_patches
