# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher

"""Read-only access to data written by phone_a_friend 0.x.

phone_a_friend 0.x used X25519, ChaCha20-Poly1305 and scrypt, none of which
is FIPS-approved. This module keeps exactly what is needed to *read* that
data once, so that :mod:`phone_a_friend.store` can migrate it to the
approved primitives in :mod:`phone_a_friend.crypto`. Nothing here encrypts,
seals or generates a key: no new data is ever written with these
algorithms.

It is imported only when 0.x data is met. An OpenSSL in FIPS mode refuses
these algorithms, so every function raises :class:`LegacyUnavailable` there:
migrate once on a host where OpenSSL is not in FIPS mode (docs/security.rst).
"""

from __future__ import annotations

from cryptography.exceptions import InternalError, InvalidTag, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .crypto import DecryptError, LegacyUnavailable

NONCE_LEN = 12
KEY_LEN = 32
PUB_LEN = 32


def _guard(fn, *args):
    try:
        return fn(*args)
    except (UnsupportedAlgorithm, InternalError) as exc:
        raise LegacyUnavailable(str(exc)) from exc


def private_key_from_bytes(raw: bytes) -> X25519PrivateKey:
    return _guard(X25519PrivateKey.from_private_bytes, raw)


def public_key_bytes(priv: X25519PrivateKey) -> bytes:
    return priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


def derive_passphrase_key(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    def run():
        kdf = Scrypt(salt=salt, length=KEY_LEN, n=n, r=r, p=p)
        return kdf.derive(passphrase.encode("utf-8"))

    return _guard(run)


def sym_decrypt(key: bytes, blob: bytes, aad: bytes = b"") -> bytes:
    if len(blob) < NONCE_LEN + 16:
        raise DecryptError("ciphertext too short")

    def run():
        return ChaCha20Poly1305(key).decrypt(blob[:NONCE_LEN], blob[NONCE_LEN:], aad)

    try:
        return _guard(run)
    except InvalidTag as exc:
        raise DecryptError("authentication failed") from exc


def _hkdf(secret: bytes, info: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=KEY_LEN, salt=None, info=info
    ).derive(secret)


def unseal(priv: X25519PrivateKey, blob: bytes) -> bytes:
    if len(blob) < PUB_LEN + NONCE_LEN + 16:
        raise DecryptError("sealed blob too short")
    eph_pub = blob[:PUB_LEN]
    my_pub = public_key_bytes(priv)
    try:
        shared = priv.exchange(X25519PublicKey.from_public_bytes(eph_pub))
    except ValueError as exc:
        raise DecryptError("bad ephemeral key") from exc
    key = _hkdf(shared, b"paf-seal|" + eph_pub + my_pub)
    return sym_decrypt(key, blob[PUB_LEN:])


def pair_key(my_priv: X25519PrivateKey, their_pub: bytes) -> bytes:
    """The 0.x DM key: HKDF over the static-static X25519 secret."""
    shared = my_priv.exchange(X25519PublicKey.from_public_bytes(their_pub))
    my_pub = public_key_bytes(my_priv)
    lo, hi = sorted([my_pub, their_pub])
    return _hkdf(shared, b"paf-dm|" + lo + hi)
