"""Account authority in committed chain state. Only authenticated native transitions write records."""

from dataclasses import dataclass
from functools import cached_property, lru_cache
from hashlib import sha256
from re import fullmatch

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey
from xian_accounts import hybrid
from xian_runtime_types.encoding import decode, decode_wire_numbers
from xian_vm_core import verify_account_components

from xian.exceptions import TransactionException
from xian.utils.encoding import canonical_json_text

POLICY_KEY = "_accounts.policy"
RECORD_PREFIX = "_accounts.records:"
SYSTEM_CONTRACT = "_accounts"
# Canonical sorted JSON puts auth first. Both decoders independently validate
# the complete envelope; this prefix only selects a decoder.
COMPACT_PREFIX = hybrid.COMPACT_TAG
ACCOUNT_WIRE_PREFIX = b'{"metadata":{"auth":'.hex().encode("ascii")


@dataclass(frozen=True, slots=True)
class RegistryPolicy:
    version: int
    enrollment_open: bool
    transition_chi: int

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict) or type(value.get("version")) is not int:
            raise ValueError("invalid account registry policy")
        version = value["version"]
        if version == 1:
            hybrid.exact_dict(value, {"version", "enrollment_open", "transition_chi"})
            if type(value["enrollment_open"]) is not bool:
                raise ValueError("invalid enrollment policy")
        elif version == 2:
            hybrid.exact_dict(value, {"version", "transition_chi"})
        else:
            raise ValueError("unsupported account registry policy")
        cost = hybrid.uint64(value["transition_chi"])
        if cost == 0:
            raise ValueError("registry transition cost must be positive")
        return cls(version, value.get("enrollment_open", False), cost)


def _account_setting(key):
    return isinstance(key, str) and (
        key.startswith("_accounts.") or key in {"__runtime_features.zk", "__fee_policy"}
    )


def _genesis_entries(state):
    entries = {}
    for entry in state.get("genesis", []):
        key = entry.get("key") if isinstance(entry, dict) else None
        if not _account_setting(key):
            continue
        if key in entries:
            raise ValueError(f"duplicate genesis key {key}")
        entries[key] = decode_wire_numbers(entry.get("value"))
    for entry in state.get("rewards", []):
        key = entry["key"]
        if _account_setting(key):
            raise ValueError("genesis rewards cannot override account registry, fee or zk policy")
    return entries


def validate_account_genesis(state):
    entries = _genesis_entries(state)
    policy = RegistryPolicy.parse(entries[POLICY_KEY]) if POLICY_KEY in entries else None
    validate_policy_change(None, policy, entries.get("__runtime_features.zk", False))
    validate_transition_budget(policy, entries.get("__fee_policy"))
    for key, value in entries.items():
        if key in {POLICY_KEY, "__runtime_features.zk", "__fee_policy"}:
            continue
        address = key.removeprefix(RECORD_PREFIX)
        if (
            policy is None
            or not key.startswith(RECORD_PREFIX)
            or not fullmatch(r"[0-9a-f]{64}", address)
        ):
            raise ValueError("invalid genesis account registry entry")
        hybrid.AccountRecord.from_dict(value)
    return policy


def validate_persisted_account_policy(driver, genesis_policy, marker_key):
    if driver.value_from_disk(marker_key) is None:
        return  # InitChain still has to install the rooted genesis state.
    stored = driver.committed_storage()[0].get(POLICY_KEY.encode())
    current = None if stored is None else RegistryPolicy.parse(decode(stored))
    validate_policy_change(genesis_policy, current, driver.value_from_disk("__runtime_features.zk"))


def require_custody(authorities, records):
    missing = sorted({key for key in authorities if key not in records})
    if missing:
        raise ValueError(f"strict genesis lacks account custody records for {', '.join(missing)}")


def stage_account_genesis(driver, policy, records):
    records = records or {}
    if policy is None and not records:
        return
    entries = [{"key": POLICY_KEY, "value": policy}] if policy is not None else []
    entries.extend({"key": RECORD_PREFIX + key, "value": record} for key, record in records.items())
    parsed = validate_account_genesis(
        {
            "genesis": [
                *entries,
                {"key": "__fee_policy", "value": driver.get("__fee_policy")},
                {"key": "__runtime_features.zk", "value": driver.get("__runtime_features.zk")},
            ]
        }
    )
    if parsed is not None and parsed.version == 2:
        prefix = "currency.balances:"
        require_custody(
            (
                key[len(prefix) :]
                for key, balance in driver.iter_items(prefix)
                if balance and fullmatch(r"[0-9a-f]{64}", key[len(prefix) :])
            ),
            records,
        )
    for entry in entries:
        driver.set(entry["key"], entry["value"])


def validate_transition_budget(policy, fees):
    from xian.fee_policy import DEFAULT_MAX_TX_CHI, TxFeePolicy

    if policy is not None:
        cap = DEFAULT_MAX_TX_CHI if fees is None else TxFeePolicy.from_state(fees).max_tx_chi
        if policy.transition_chi > cap:
            raise ValueError("registry transition exceeds the transaction chi cap")


def validate_policy_change(current, candidate, zk_enabled):
    if (
        current is not None
        and current.version == 2
        and (candidate is None or candidate.version != 2)
    ):
        raise ValueError("strict account registry cannot return to legacy authorization")
    if candidate is not None and candidate.version == 2 and zk_enabled:
        raise ValueError("strict account registry requires disabled zk proofs")


ACCOUNT_PREFIXES = (COMPACT_PREFIX, ACCOUNT_WIRE_PREFIX)


def is_registry_transaction(tx):
    return (
        tx.get("metadata", {}).get("tx_format") == hybrid.TX_FORMAT
        and tx["payload"]["contract"] == SYSTEM_CONTRACT
    )


@lru_cache(maxsize=256)
def _committed_record(stored):
    # Stored bytes, rather than the address or epoch, name this immutable parse.
    # Each entry retains about 8 KiB of JSON and 4 KiB of validated public keys.
    return hybrid.AccountRecord.from_dict(decode(stored))


class AccountRegistry:
    """One proposal, finalized block or CheckTx view, pinned to committed state."""

    def __init__(self, driver, chain_id, proofs=None, *, strict_genesis=False):
        self.environment = driver.committed_storage()[0]
        self.chain_id = chain_id
        self.strict_genesis = strict_genesis
        self._transitioned = set()
        self._records = {}
        self._proofs = proofs

    @cached_property
    def _stored_policy(self):
        return self.environment.get(POLICY_KEY.encode())

    @cached_property
    def policy(self):
        return (
            None
            if self._stored_policy is None
            else RegistryPolicy.parse(decode(self._stored_policy))
        )

    def committed_authority(self, sender):
        """The exact record and policy bytes read when authorizing this sender."""
        return self._record(sender), self._stored_policy

    def _record(self, sender):
        if sender not in self._records:
            self._records[sender] = self.environment.get((RECORD_PREFIX + sender).encode())
        return self._records[sender]

    def accept(self, tx):
        if is_registry_transaction(tx):
            self._transitioned.add(tx["payload"]["sender"])

    def authorize(self, tx, raw=None):
        try:
            self._authorize(tx, raw)
        except (ValueError, BadSignatureError) as exc:
            raise TransactionException(f"Account authorization failed: {exc}") from exc

    def _authorize(self, tx, raw):
        payload = tx["payload"]
        sender = payload["sender"]
        if sender in self._transitioned:
            raise ValueError("account key change requires the next committed block")
        current = self._record(sender)
        policy = self.policy
        if "tx_format" not in tx["metadata"]:
            if (
                self.strict_genesis
                or current is not None
                or (policy is not None and policy.version == 2)
            ):
                raise ValueError("hybrid authorization required")
            return  # The native legacy decoder verified the signature.
        if (
            policy is None
            or (self.strict_genesis and policy.version != 2)
            or payload["chain_id"] != self.chain_id
        ):
            raise ValueError("inactive account registry or incorrect chain")
        transition = is_registry_transaction(tx)
        compact = raw is not None and raw.startswith(COMPACT_PREFIX)
        if compact != (policy.version == 2 and not transition):
            raise ValueError("incorrect wire encoding for committed account policy")
        self._verify_current(tx, current, raw, transition, compact)

    def _verify_current(self, tx, current, raw, transition, compact):
        # Cache only a positive proof for the exact wire bytes, record and policy.
        # The block barrier, current authority, fee and nonce checks always run.
        key = None
        if self._proofs is not None and raw is not None:
            key = (sha256(raw).digest(), sha256(current or b"").digest(), self.policy)
            if key in self._proofs:
                self._proofs.move_to_end(key)
                return
        record = None if current is None else _committed_record(current)
        if transition:
            _verify_transition(tx["payload"], tx["metadata"]["auth"], record, self.policy)
        else:
            _verify_spend(tx["payload"], tx["metadata"]["auth"], record, raw if compact else None)
        if key is not None:
            self._proofs[key] = None
            if len(self._proofs) > 8192:
                self._proofs.popitem(last=False)


def _payload_bytes(payload):
    message = canonical_json_text(payload).encode("utf-8")
    if len(message) > hybrid.MAX_PAYLOAD_BYTES:
        raise ValueError("hybrid payload exceeds size limit")
    return message


def _verify_spend(payload, auth, current, compact_wire):
    hybrid.exact_dict(auth, {"scheme", "signature"})
    if current is None or auth["scheme"] != hybrid.SCHEME:
        raise ValueError("hybrid account required")
    if compact_wire is None:
        signature = hybrid.hex_bytes(auth["signature"], hybrid.AUTH_BYTES)
        message = _payload_bytes(payload)
    else:
        # The native decoder already verified the frame and canonical payload.
        start = len(COMPACT_PREFIX)
        end = start + hybrid.AUTH_BYTES
        signature, message = compact_wire[start:end], compact_wire[end:]
    if not hybrid.verify(
        current.active, current.epoch, message, signature, verifier=verify_account_components
    ):
        raise ValueError("invalid hybrid transaction signature")


def _verify_transition(payload, auth, current, policy):
    operation = payload["function"]
    if operation not in {"create", "enroll", "rotate", "recover"}:
        raise ValueError("unsupported registry operation")
    if payload["chi_supplied"] < policy.transition_chi:
        raise ValueError("insufficient registry chi budget")
    if operation == "enroll" and not policy.enrollment_open:
        raise ValueError("legacy enrollment is unavailable")
    if (current is None) != (operation in {"create", "enroll"}):
        raise ValueError("invalid account transition for current authority")
    fields = {"scheme", "signature", "recovery_proof"}
    if operation != "create":
        fields.add("active_proof")
    hybrid.exact_dict(auth, fields)
    hybrid.exact_dict(payload["kwargs"], {"record"})
    candidate = hybrid.AccountRecord.from_dict(decode_wire_numbers(payload["kwargs"]["record"]))
    if candidate.epoch != (0 if current is None else current.epoch + 1):
        raise ValueError("invalid next key epoch")
    active_field = "signature" if operation == "create" else "active_proof"
    active = hybrid.hex_bytes(auth[active_field], hybrid.AUTH_BYTES)
    recovery = hybrid.hex_bytes(auth["recovery_proof"], hybrid.AUTH_BYTES)
    message = _payload_bytes(payload)
    sender = payload["sender"]
    if operation == "create":
        if auth["scheme"] != hybrid.CREATE_SCHEME or sender != hybrid.account_address(candidate):
            raise ValueError("invalid self-certifying account address or scheme")
    else:
        _verify_authority(operation, auth, sender, message, current, candidate)
    if not hybrid.verify(
        candidate.active,
        candidate.epoch,
        message,
        active,
        role="new_active",
        verifier=verify_account_components,
    ):
        raise ValueError("invalid active possession proof")
    if not hybrid.verify(
        candidate.recovery,
        candidate.epoch,
        message,
        recovery,
        role="new_recovery",
        verifier=verify_account_components,
    ):
        raise ValueError("invalid recovery possession proof")


def _verify_authority(operation, auth, sender, message, current, candidate):
    if current is None:
        if auth["scheme"] != hybrid.ENROLL_SCHEME:
            raise ValueError("invalid enrollment scheme")
        old_ed, old_pq = {bytes.fromhex(sender)}, set()
        VerifyKey(bytes.fromhex(sender)).verify(
            hybrid.enrollment_transcript(message), hybrid.hex_bytes(auth["signature"], 64)
        )
    else:
        if auth["scheme"] != hybrid.SCHEME:
            raise ValueError("invalid authority scheme")
        old_ed = {current.active.ed25519, current.recovery.ed25519}
        old_pq = {current.active.ml_dsa_65, current.recovery.ml_dsa_65}
        authority = current.recovery if operation == "recover" else current.active
        signature = hybrid.hex_bytes(auth["signature"], hybrid.AUTH_BYTES)
        if not hybrid.verify(
            authority,
            current.epoch,
            message,
            signature,
            role="authority",
            verifier=verify_account_components,
        ):
            raise ValueError("invalid current authority")
    if any(
        keys.ed25519 in old_ed or keys.ml_dsa_65 in old_pq
        for keys in (candidate.active, candidate.recovery)
    ):
        raise ValueError("replacement keys must be fresh")


def registry_for_app(app):
    return AccountRegistry(
        app.client.raw_driver,
        app.chain_id,
        app.account_proof_cache,
        strict_genesis=app.strict_accounts,
    )
