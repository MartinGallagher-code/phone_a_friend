.. SPDX-FileCopyrightText: 2026 Martin Gallagher
..
.. SPDX-License-Identifier: GPL-3.0-or-later

Security model
==============

* Access control is layered: the filesystem permissions of the shared
  directory decide *who can see the files at all*; encryption decides *who
  can read the messages*.
* Direct messages are encrypted with a key derived from the static-static
  X25519 Diffie-Hellman secret of the two participants — readable only by
  someone holding one of the two private keys **and** the peer's public key.
* Group messages are encrypted with the group's symmetric key, held only by
  members who accepted an invite.
* Invites and invite replies are "sealed" (ephemeral X25519 → HKDF →
  ChaCha20-Poly1305) to the recipient's public identity key.
* All ciphertexts are authenticated (AEAD); tampered files are ignored.
* Group removal is cooperative: the removed client discards its keys when it
  sees the notice (the group key is not rotated).

Out of scope for v1
-------------------

* Forward secrecy / key rotation.
* Sender authentication beyond conversation-key possession.
* Traffic analysis (filenames reveal timing; directory names reveal who
  talks to whom).
* Revoking group keys.

Cryptographic primitives
------------------------

The building blocks live in :mod:`phone_a_friend.crypto`:

* Identity keys are X25519 keypairs.
* Symmetric encryption is ChaCha20-Poly1305 (nonce prepended to ciphertext).
* Sealed messages (invites) use an ephemeral X25519 key against the
  recipient's static public key, so anything can be pushed to a user knowing
  only their public identity key.
* The per-user config file is encrypted with a key derived from the user's
  passphrase via scrypt; it protects the private identity key, contact keys,
  and group keys.
