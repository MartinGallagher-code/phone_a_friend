# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher

"""Write a shared directory exactly as phone_a_friend 0.3.0 did.

The package no longer has the 0.x primitives for writing (only
phone_a_friend.legacy, for reading), so the migration tests build their 0.x
fixtures with this copy of what 0.3.0's crypto.py and store.py wrote:
X25519 identities, ChaCha20-Poly1305, scrypt N=2^14 r=8 p=1, and wrappers
with no "v" field.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


def b64e(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _raw_pub(priv) -> bytes:
    return priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


def _enc(key: bytes, plain: bytes, aad: bytes = b"") -> bytes:
    nonce = os.urandom(12)
    return nonce + ChaCha20Poly1305(key).encrypt(nonce, plain, aad)


def _hkdf(secret: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(
        secret
    )


def _fname() -> str:
    return f"{time.time_ns():020d}-{secrets.token_hex(4)}.json"


def _write(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(obj, fh)


class V0User:
    """One 0.3.0 user in a shared directory."""

    def __init__(self, root: str, name: str, passphrase: str):
        self.root, self.name, self.passphrase = root, name, passphrase
        self.priv = X25519PrivateKey.generate()
        self.pub = _raw_pub(self.priv)
        self.config = {
            "name": name,
            "priv": b64e(
                self.priv.private_bytes(
                    serialization.Encoding.Raw,
                    serialization.PrivateFormat.Raw,
                    serialization.NoEncryption(),
                )
            ),
            "contacts": {},
            "groups": {},
            "last_read": {},
            "pending": [],
        }
        for sub in ("invites", "replies"):
            os.makedirs(os.path.join(root, sub, name), exist_ok=True)
        _write(
            os.path.join(root, "users", name, "identity.json"),
            {"name": name, "pub": b64e(self.pub)},
        )
        self.save()

    def save(self) -> None:
        salt = os.urandom(16)
        key = Scrypt(salt=salt, length=32, n=2 ** 14, r=8, p=1).derive(
            self.passphrase.encode()
        )
        blob = _enc(key, json.dumps(self.config).encode(), b"paf-config")
        _write(
            os.path.join(self.root, "users", self.name, "config.enc"),
            {
                "salt": b64e(salt),
                "kdf": {"n": 2 ** 14, "r": 8, "p": 1},
                "blob": b64e(blob),
            },
        )

    def befriend(self, other: "V0User") -> None:
        self.config["contacts"][other.name] = b64e(other.pub)
        other.config["contacts"][self.name] = b64e(self.pub)
        self.save()
        other.save()

    def _dm_dir(self, other: str) -> str:
        lo, hi = sorted([self.name, other])
        return os.path.join(self.root, "dm", f"{lo}__{hi}")

    def send_dm(self, other: "V0User", text: str) -> str:
        shared = self.priv.exchange(X25519PublicKey.from_public_bytes(other.pub))
        lo, hi = sorted([self.pub, other.pub])
        key = _hkdf(shared, b"paf-dm|" + lo + hi)
        directory = self._dm_dir(other.name)
        aad = b"paf-dm|" + os.path.basename(directory).encode()
        msg = {"from": self.name, "ts": time.time(), "text": text}
        fname = _fname()
        _write(
            os.path.join(directory, fname),
            {"blob": b64e(_enc(key, json.dumps(msg).encode(), aad))},
        )
        return fname

    def create_group(self, name: str, members) -> str:
        gid = secrets.token_hex(8)
        key = os.urandom(32)
        _write(
            os.path.join(self.root, "groups", gid, "meta.json"),
            {"id": gid, "name": name, "owner": self.name, "created": time.time()},
        )
        for user in [self, *members]:
            user.config["groups"][gid] = {"name": name, "key": b64e(key)}
            user.save()
        return gid

    def send_group(self, gid: str, text: str) -> str:
        key = base64.b64decode(self.config["groups"][gid]["key"])
        msg = {"from": self.name, "ts": time.time(), "text": text}
        fname = _fname()
        _write(
            os.path.join(self.root, "groups", gid, "msgs", fname),
            {"blob": b64e(_enc(key, json.dumps(msg).encode(), b"paf-grp|" + gid.encode()))},
        )
        return fname

    def seal_to(self, box: str, recipient_pub: bytes, payload: dict) -> None:
        eph = X25519PrivateKey.generate()
        eph_pub = _raw_pub(eph)
        shared = eph.exchange(X25519PublicKey.from_public_bytes(recipient_pub))
        key = _hkdf(shared, b"paf-seal|" + eph_pub + recipient_pub)
        blob = eph_pub + _enc(key, json.dumps(payload).encode())
        _write(os.path.join(self.root, box, _fname()), {"sealed": b64e(blob)})

    def invite(self, other: "V0User") -> None:
        self.seal_to(
            os.path.join("invites", other.name),
            other.pub,
            {"type": "contact", "from": self.name, "from_pub": b64e(self.pub),
             "ts": time.time()},
        )
        self.config["pending"].append({"type": "contact", "to": other.name})
        self.save()
