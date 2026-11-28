"""The two validator suites Xian can encode for upstream CometBFT v0.40.0."""

import base64
import hashlib
from enum import Enum

from tendermint.crypto.keys_pb2 import PublicKey
from xian.formatting import vk_is_formatted

CONSENSUS_VALIDATORS_KEY = "validators.__consensus__"
SUITE_KEY = "validators.consensus_key_type"
PUBLIC_KEY_PREFIX = "validators.consensus_public_keys:"
ADDRESS_PREFIX = "validators.consensus_keys:"
MAX_MEMBERS = 64  # The validator contract's MAX_VALIDATOR_ACCOUNTS.
MAX_TOTAL_VOTING_POWER = (2**63 - 1) // 8


def consensus_address(raw: bytes) -> bytes:
    return hashlib.sha256(raw).digest()[:20]


class ValidatorSuite(Enum):
    ED25519 = ("ed25519", "tendermint/PubKeyEd25519", "ed25519", 32)
    ML_DSA_65 = ("ml_dsa_65", "cometbft/PubKeyMlDsa65", "mldsa65", 1952)

    def __init__(self, comet_name, json_name, proto_name, size):
        self.comet_name = comet_name
        self.json_name = json_name
        self.proto_name = proto_name
        self.size = size

    @classmethod
    def from_name(cls, name):
        for suite in cls:
            if name == suite.comet_name:
                return suite
        raise ValueError("unsupported CometBFT validator key type")

    @classmethod
    def from_genesis(cls, genesis):
        params = genesis.get("consensus_params", {}).get("validator", {})
        names = params.get("pub_key_types", ["ed25519"])
        if not isinstance(names, list) or len(names) != 1:
            raise ValueError("genesis must allow exactly one validator key type")
        return cls.from_name(names[0])

    @classmethod
    def from_json_key(cls, key):
        for suite in cls:
            if isinstance(key, dict) and key.get("type") == suite.json_name:
                return suite
        raise ValueError("unsupported CometBFT validator public key type")

    def decode_hex(self, value) -> bytes:
        if not isinstance(value, str) or len(value) != 2 * self.size:
            raise ValueError("invalid validator public key length")
        raw = bytes.fromhex(value)
        if value != raw.hex():
            raise ValueError("validator public key must be canonical lowercase hex")
        return raw

    def json_key(self, raw: bytes) -> dict:
        return {"type": self.json_name, "value": base64.b64encode(raw).decode("ascii")}

    def decode_json(self, key: dict) -> bytes:
        if not isinstance(key, dict) or key.get("type") != self.json_name:
            raise ValueError("validator public key type disagrees with genesis")
        raw = base64.b64decode(key.get("value", ""), validate=True)
        if len(raw) != self.size:
            raise ValueError("invalid validator public key length")
        return raw

    def proto_key(self, raw: bytes) -> PublicKey:
        return PublicKey(**{self.proto_name: raw})

    def member_key(self, member, read) -> bytes:
        if not vk_is_formatted(member):
            raise ValueError("invalid validator member")
        if self is ValidatorSuite.ED25519:
            return bytes.fromhex(member)
        raw = self.decode_hex(read(PUBLIC_KEY_PREFIX + member))
        if read(ADDRESS_PREFIX + consensus_address(raw).hex()) != member:
            raise ValueError("validator consensus address does not belong to its member")
        return raw


def state_suite(read) -> ValidatorSuite:
    configured = read(SUITE_KEY)
    return ValidatorSuite.from_name("ed25519" if configured is None else configured)


def validate_genesis_validator_keys(genesis):
    suite = ValidatorSuite.from_genesis(genesis)
    block = genesis["abci_genesis"]
    state = {entry["key"]: entry["value"] for entry in block["genesis"]}
    if any(entry["key"].startswith("validators.") for entry in block.get("rewards", [])):
        raise ValueError("genesis rewards cannot override validator state")
    strict = (state.get("_accounts.policy") or {}).get("version") == 2
    if strict and suite is not ValidatorSuite.ML_DSA_65:
        raise ValueError("strict genesis requires ML-DSA-65 validators")
    if state_suite(state.get) is not suite:
        raise ValueError("CometBFT and application genesis validator suites disagree")
    entries = genesis.get("validators", [])
    if suite is ValidatorSuite.ED25519:
        # Historical Ed networks take powers and keys from the application's
        # InitChain response. Their serialized genesis list can be stale.
        for entry in entries:
            if ValidatorSuite.from_json_key(entry["pub_key"]) is not suite:
                raise ValueError("validator public key type disagrees with genesis")
        return
    expected = _pq_member_powers(suite, state, strict and int(block.get("number", 0)) == 0)
    if _pq_comet_powers(suite, entries) != expected:
        raise ValueError("ML-DSA genesis validator set must match application members and powers")
    _require_block_capacity(genesis["consensus_params"])


def _pq_member_powers(suite, state, require_records):
    members = state.get("validators.active_validators")
    if not isinstance(members, list) or not 1 <= len(members) <= MAX_MEMBERS:
        raise ValueError("invalid number of ML-DSA genesis validators")
    powers = {}
    member_powers = {}
    for member in members:
        raw = suite.member_key(member, state.get)
        power = state.get("validators.powers:" + member, 10)
        if raw in powers or type(power) is not int or power <= 0:
            raise ValueError("invalid or duplicate ML-DSA genesis member")
        powers[raw] = power
        member_powers[member] = power
    baseline = state.get(CONSENSUS_VALIDATORS_KEY)
    if baseline is not None and baseline != member_powers:
        raise ValueError("genesis committed validator baseline differs from the initial set")
    if sum(powers.values()) > MAX_TOTAL_VOTING_POWER:
        raise ValueError("ML-DSA genesis exceeds the CometBFT voting power limit")
    if require_records:
        from xian.account_registry import RECORD_PREFIX, require_custody

        authorities = set(members)
        authorities.update(
            state.get("validators.reward_keys:" + member) or member for member in members
        )
        require_custody(
            authorities, {k[len(RECORD_PREFIX) :] for k in state if k.startswith(RECORD_PREFIX)}
        )
    return powers


def _pq_comet_powers(suite, entries):
    powers = {}
    for entry in entries:
        raw = suite.decode_json(entry["pub_key"])
        if raw in powers:
            raise ValueError("duplicate ML-DSA validator key")
        if (
            entry.get("address")
            and entry["address"].upper() != consensus_address(raw).hex().upper()
        ):
            raise ValueError("incorrect CometBFT genesis validator address")
        value = entry["power"]
        if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
            raise ValueError("CometBFT genesis validator power must be a quoted integer")
        powers[raw] = int(value)
    return powers


def _require_block_capacity(params):
    # types/block.go: header=626, framing=11, commit=94+3358*members.
    # Reserve the contract's maximum future set, even if genesis starts with one signer.
    block_limit = int(params["block"]["max_bytes"])
    minimum = 731 + 3358 * MAX_MEMBERS + int(params["evidence"]["max_bytes"])
    if block_limit == -1:
        block_limit = 104857600  # upstream MaxBlockSizeBytes
    if block_limit <= minimum:
        raise ValueError("block size cannot accommodate evidence and the maximum ML-DSA commit")


def validate_init_validator_request(req, genesis):
    suite = ValidatorSuite.from_genesis(genesis)
    if suite is ValidatorSuite.ED25519:
        return
    if req is None or req.chain_id != genesis["chain_id"]:
        raise ValueError("InitChain chain ID disagrees with ML-DSA genesis")
    if list(req.consensus_params.validator.pub_key_types) != [suite.comet_name]:
        raise ValueError("InitChain validator key type disagrees with ML-DSA genesis")
    local = {suite.decode_json(e["pub_key"]): int(e["power"]) for e in genesis["validators"]}
    requested = {v.pub_key.mldsa65: v.power for v in req.validators}
    if len(requested) != len(req.validators) or requested != local:
        raise ValueError("InitChain validators disagree with ML-DSA genesis")
