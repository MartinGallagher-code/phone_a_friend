"""Cryptographic primitives for phone_a_friend.

All encryption happens client side:

* Identity keys are X25519 keypairs.
* Symmetric encryption is ChaCha20-Poly1305 (nonce prepended to ciphertext).
* "Sealed" messages (invites) use an ephemeral X25519 key against the
  recipient's static public key, so anything can be pushed to a user knowing
  only their public identity key.
* Direct-message conversations are encrypted with a key derived from the
  static-static Diffie-Hellman shared secret of the two participants, so
  without one of the two private keys (plus the peer's public key) the
  messages cannot be read.
* Group conversations use a random symmetric group key that is pushed to
  invitees inside a sealed invite.
* The per-user config file is encrypted with a key derived from the user's
  passphrase via scrypt; it protects the private identity key, contact keys,
  and group keys.
"""

from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

NONCE_LEN = 12
KEY_LEN = 32
PUB_LEN = 32

# scrypt parameters for the passphrase KDF
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_LEN = 16


class DecryptError(Exception):
    """Raised when a payload cannot be decrypted/authenticated."""


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(data: str) -> bytes:
    return base64.b64decode(data.encode("ascii"))


# ---------------------------------------------------------------- key pairs

def generate_private_key() -> X25519PrivateKey:
    return X25519PrivateKey.generate()


def private_key_bytes(priv: X25519PrivateKey) -> bytes:
    return priv.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )


def private_key_from_bytes(raw: bytes) -> X25519PrivateKey:
    return X25519PrivateKey.from_private_bytes(raw)


def public_key_bytes(priv: X25519PrivateKey) -> bytes:
    return priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


# ---------------------------------------------------------------- symmetric

def new_symmetric_key() -> bytes:
    return os.urandom(KEY_LEN)


def sym_encrypt(key: bytes, plaintext: bytes, aad: bytes = b"") -> bytes:
    nonce = os.urandom(NONCE_LEN)
    return nonce + ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad)


def sym_decrypt(key: bytes, blob: bytes, aad: bytes = b"") -> bytes:
    if len(blob) < NONCE_LEN + 16:
        raise DecryptError("ciphertext too short")
    try:
        return ChaCha20Poly1305(key).decrypt(blob[:NONCE_LEN], blob[NONCE_LEN:], aad)
    except InvalidTag as exc:
        raise DecryptError("authentication failed") from exc


# --------------------------------------------------------------- KDF helpers

def derive_passphrase_key(passphrase: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=KEY_LEN, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return kdf.derive(passphrase.encode("utf-8"))


def _hkdf(secret: bytes, info: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=KEY_LEN, salt=None, info=info
    ).derive(secret)


# --------------------------------------------------------------- sealed box

def seal(recipient_pub: bytes, plaintext: bytes) -> bytes:
    """Encrypt to a public key using an ephemeral sender key."""
    eph = X25519PrivateKey.generate()
    eph_pub = public_key_bytes(eph)
    shared = eph.exchange(X25519PublicKey.from_public_bytes(recipient_pub))
    key = _hkdf(shared, b"paf-seal|" + eph_pub + recipient_pub)
    return eph_pub + sym_encrypt(key, plaintext)


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


# ------------------------------------------------------------- pair-wise DM

def pair_key(my_priv: X25519PrivateKey, their_pub: bytes) -> bytes:
    """Conversation key both parties can derive from their own private key
    and the peer's public key. Identical on both sides."""
    shared = my_priv.exchange(X25519PublicKey.from_public_bytes(their_pub))
    my_pub = public_key_bytes(my_priv)
    lo, hi = sorted([my_pub, their_pub])
    return _hkdf(shared, b"paf-dm|" + lo + hi)
