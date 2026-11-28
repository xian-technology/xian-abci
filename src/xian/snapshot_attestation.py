"""Offline publisher keys for full-node-store manifests.

Run python -m xian.snapshot_attestation NEW_KEY_FILE to create a private key
and print its public trust pin. ML-DSA uses FIPS 204 pure mode, empty context.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from xian_accounts import Ed25519Account, is_canonical_hex, is_valid_ed25519_key
from xian_accounts.hybrid import PQ_PUBLIC_BYTES, backend

PQ_KEY_PREFIX = "ml-dsa-65:"
PQ_KEY_FORMAT = "xian-snapshot-ml-dsa-65-v1"
PQ_DOMAIN = "XIAN-FULL-NODE-STORE\0"
MAX_KEY_FILE_BYTES = 16384


def is_pq_pin(pin: str) -> bool:
    return pin.startswith(PQ_KEY_PREFIX)


def validate_publisher_pins(values: list[str]) -> list[str]:
    pins = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("invalid snapshot signing key")
        pin = value.strip()
        if is_pq_pin(pin):
            if not is_canonical_hex(pin[len(PQ_KEY_PREFIX) :], 2 * PQ_PUBLIC_BYTES):
                raise ValueError("invalid ML-DSA-65 snapshot signing key")
        elif not is_valid_ed25519_key(pin):
            raise ValueError("invalid snapshot signing key")
        pins.append(pin)
    if any(map(is_pq_pin, pins)) and not all(map(is_pq_pin, pins)):
        raise ValueError("ML-DSA-65 snapshot pins cannot be mixed with legacy pins")
    return pins


@dataclass(frozen=True, repr=False)
class _PqPublisher:
    public_key: str
    secret: bytes

    def sign_message(self, message: str) -> str:
        return backend().sign(self.secret, message.encode("utf-8")).hex()


def load_publisher_key(path: Path) -> _PqPublisher | Ed25519Account:
    with path.open("rb") as stream:
        if os.fstat(stream.fileno()).st_mode & 0o077:
            raise ValueError("snapshot signing key file must be private (mode 0600)")
        data = stream.read(MAX_KEY_FILE_BYTES + 1)
    if len(data) > MAX_KEY_FILE_BYTES:
        raise ValueError("snapshot signing key file exceeds the size limit")
    value = data.decode("ascii").strip()
    if not value.startswith("{"):
        return Ed25519Account(value)
    key = json.loads(value)
    if set(key) != {"format", "public_key", "secret_key"} or key["format"] != PQ_KEY_FORMAT:
        raise ValueError("unsupported snapshot signing key format")
    if not is_canonical_hex(key["public_key"], 2 * PQ_PUBLIC_BYTES):
        raise ValueError("invalid ML-DSA-65 snapshot public key")
    if not is_canonical_hex(key["secret_key"], 2 * backend().SECRET_KEY_SIZE):
        raise ValueError("invalid ML-DSA-65 snapshot secret key")
    return _PqPublisher(PQ_KEY_PREFIX + key["public_key"], bytes.fromhex(key["secret_key"]))


def generate_publisher_key(path: Path) -> str:
    public, secret = backend().generate_keypair()
    data = {"format": PQ_KEY_FORMAT, "public_key": public.hex(), "secret_key": secret.hex()}
    # Exclusive creation prevents replacing an existing publisher or following a symlink.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="ascii") as stream:
        json.dump(data, stream)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return PQ_KEY_PREFIX + public.hex()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("new_key_file", type=Path)
    print(generate_publisher_key(parser.parse_args().new_key_file))
