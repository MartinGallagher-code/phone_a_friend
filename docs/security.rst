.. SPDX-License-Identifier: GPL-3.0-or-later
.. SPDX-FileCopyrightText: 2026 Martin J. Gallagher

Security model
==============

* Access control is layered: the filesystem permissions of the shared
  directory decide *who can see the files at all*; encryption decides *who
  can read the messages*.
* Direct messages are encrypted with a key derived from the static-static
  P-256 Diffie-Hellman secret of the two participants — readable only by
  someone holding one of the two private keys **and** the peer's public key.
  That holds while the key you hold for a contact really is theirs: see
  *What is authenticated*, below.
* Group messages are encrypted with a key derived from the group's symmetric
  key, held only by members who accepted an invite.
* Invites and invite replies are "sealed" (ephemeral P-256 ECDH → HKDF →
  AES-256-GCM) to the recipient's public identity key.
* All ciphertexts are authenticated (AEAD); tampered files are ignored.
* Group removal is cooperative: the removed client discards its keys when it
  sees the notice (the group key is not rotated).

What is authenticated
---------------------

Anyone who can write to the shared directory can drop a file into another
user's ``invites/<name>/`` or ``replies/<name>/`` (they are drop boxes,
mode 1777), and seal it to that user's public key. Before 2.1 a client
believed what such a file said, so another user of the share could put their
own key in place of a contact's and read what was sent "to" that contact,
invite people in someone else's name, end anyone's chat or throw anyone out
of a group (PAF-009). Since 2.1 every invite and reply is checked first:

* **The sender must be a registered user**: a valid name, not yours, with a
  ``users/<name>/identity.json``.
* **On a multi-user system, the file must be the sender's**: a plain file
  (not a symlink) owned by the same Unix account as the sender's
  ``identity.json``. No one else can make such a file. Where everyone shares
  one account (a service account, NFS with ``all_squash``, a Syncthing
  folder), every file passes, and the checks below are what count.
* **A key it carries must be the sender's**: an invite's or an acceptance's
  ``from_pub`` must be the P-256 key the sender's ``identity.json``
  publishes (or, from a 0.x client, the X25519 key it lists as replaced, or
  publishes while still on 0.x).
* **An acceptance must answer an invite you sent**: one with no matching
  invite in your config (in memory, or on disk, where another device of
  yours may have written it) is dropped, and your contacts do not change.
* **Replies that carry no key are signed**: a decline, a group join, an
  unfriend notice and a group removal carry
  ``"auth"``, an HMAC-SHA256 over the reply under a key derived with HKDF
  from the static-static ECDH secret of sender and recipient, which only
  those two can compute. A group removal's key also takes in the group key,
  so only a member of the group can send one. The recipient checks it
  against the key it holds for the sender: their stored key if they are a
  contact, otherwise the one their ``identity.json`` publishes.
* **Clients before 2.1 sign nothing.** Their unsigned decline, join or
  unfriend notice is still honoured when the file's owner shows it is
  theirs: on a multi-user system, where the sender's ``identity.json``
  belongs to another account than yours. Elsewhere, and for any unsigned
  group removal (an account cannot show that its owner is in the group), it
  is ignored with a notice. Upgrade everyone who shares a directory.
* **A contact's key is never replaced** (trust on first use). Your client
  compares each contact's stored key with the one their ``identity.json``
  publishes, every time it polls. A different key, there or in an invite or
  reply, is not taken: it is recorded, the contact is marked ``!`` and you
  are told once, "bob's key changed — /verify bob". Messages keep going to
  the old key, which the holder of the new one cannot read.

Anything refused is deleted from your drop box, with a notice that names the
sender it claimed to be from ("refused a chat invite claiming to be from
alice: the key in it is not alice's"). A reply about nothing of yours (an
answer to no invite, a removal from a group you are not in) is dropped
without one.

``users/``, ``invites/`` and ``replies/`` themselves get the sticky bit
(mode 1777 when the client makes them; on login it adds the bit to one its
user owns), so no one can rename another user's directory away and put an
``identity.json`` of their own in its place. On a share set up before 2.1 by
someone else, the directories' owner, or root, can do it:
``chmod +t users invites replies``.

Verifying a contact
-------------------

The first key you hold for someone comes from their ``identity.json``, on
trust. To check it, compare **safety numbers**: ``/verify bob`` in the TUI
(or Verify in a chat's menu), or ``paf verify bob``. The safety number is 60
digits in 12 groups of five, from SHA-256 over both public keys, sorted, so
bob sees the same number for you. Compare every digit, in person or by phone
(not through phone_a_friend itself): if they match, each of you holds the
other's real key. Say so, and paf marks the contact verified: ``✓`` in the
left pane and the chat's title, "verified" in ``paf status``. The mark is
for that key: unfriending clears it, and so does a key change.

When a contact's key changes, ``/verify`` shows the safety number for the
**new** key. Accept it only if it matches the one they read you; paf then
moves the contact to the new key, marks it verified, and sends them a chat
invite, since whoever made the new key no longer has yours. If the key
changes again while the number is on screen, nothing is accepted.

Mixing versions
---------------

2.1 reads everything 2.0 writes, and 2.0 reads everything 2.1 writes (it
ignores the ``"auth"`` field and the new config keys, and keeps them when it
saves). Only forged files are newly refused, and the unsigned notices of a
2.0 client, as described above.

Out of scope
------------

* Forward secrecy / key rotation.
* Authenticating each message beyond conversation-key possession: anyone
  who holds a group's key can write in it as any member.
* Your first key for someone, which comes from their ``identity.json`` on
  trust: compare safety numbers.
* Replays where everyone shares one Unix account: someone who copied a
  signed notice from your drop box before your client read it can deliver
  it again later (on a multi-user system the copy is theirs, and refused).
* Traffic analysis. DM directory names no longer show who talks to whom
  (*What a listing still shows*, below), but file names reveal timing, and
  file ownership reveals who writes where. Group names are encrypted in ``meta.json``, but its owner
  and the number and timing of a group's messages are visible.
* Revoking group keys.

What a listing still shows
--------------------------

Since 2.0, a DM directory is named ``dm/<32 hex characters>``, the first
16 bytes of ``HKDF-SHA256(pair key, "paf-dm-dir-v2")``. The pair key comes
from the two users' P-256 ECDH, so only those two can compute the name or
tell whose it is; the AEAD's associated data still names the pair, so a file
cannot be moved into another pair's directory unnoticed. Anyone who can list
the shared directory still sees:

* how many DMs and groups exist, and how many messages each holds;
* sizes and times: message file names begin with their send time, and every
  file has a size and modification time;
* who wrote each file: on a multi-user system each message file is owned by
  its sender's Unix account, so ``ls -l`` in a DM directory shows the two
  accounts writing there. Encryption cannot hide this; only a shared service
  account or a setuid helper could;
* user names, group owners and presence (below);
* while a pair moves from 1.x, the old ``dm/<a>__<b>`` directory, until both
  have run 2.0: each client moves the files it owns (the sticky bit lets
  only a file's owner move it) and removes the old directory once empty.

Search
------

``/search`` decrypts messages in memory, through the same cache the TUI
keeps for open conversations, and matches there. Nothing decrypted is
written: there is no index, on the shared directory or in your home, which
would undo the encryption. A search therefore costs one decryption per
message the first time in a session, and none after.

Presence
--------

Presence, typing and read receipts are **on by default**. They are written
to ``beacons/<name>.<device>.json``; ``/presence off`` in the TUI, or
``paf presence off``, stops writing it and deletes yours. Anyone who can
list the shared directory sees, from that file's name and modification time,
*that* a user is active and roughly when, and how many conversations they
have (one entry each). Only people in a conversation can tell which entry is
theirs or read it: the read marker and typing time in it are encrypted with
keys derived from that conversation's key.

Cryptographic primitives
------------------------

The building blocks live in :mod:`phone_a_friend.crypto`. Every one is
FIPS-approved:

.. list-table::
   :header-rows: 1

   * - Use
     - Primitive
     - Standard
   * - identity keys, DM key agreement, sealed invites
     - ECDH on NIST P-256 (static-static for DMs, ephemeral-static for
       invites); public keys as uncompressed X9.62 points
     - SP 800-56A
   * - messages, invites and the config file
     - AES-256-GCM, 96-bit random nonce prepended, 128-bit tag
     - SP 800-38D
   * - key derivation from a shared secret or a group key
     - HKDF-SHA256
     - SP 800-56C
   * - passphrase to config key
     - PBKDF2-HMAC-SHA256, 600,000 iterations, 128-bit salt (the count is
       stored beside the config, so it can be raised later)
     - SP 800-132
   * - signatures on replies that carry no key
     - HMAC-SHA256 under an HKDF-SHA256 key from the static-static ECDH
       secret (and, for a group removal, the group key)
     - FIPS 198-1
   * - safety numbers
     - SHA-256 over both public keys
     - FIPS 180-4
   * - private keys
     - generated by OpenSSL (``ec.generate_private_key``)
     - FIPS 186
   * - symmetric keys, nonces, salts
     - ``os.urandom`` (the kernel's ``getrandom``; see below)
     - SP 800-90A

Every encrypted record is a JSON wrapper with a ``"v"`` field naming its
format (currently ``2``); ``config.enc`` also records ``"kdf"`` (algorithm
and iteration count) and ``"aead"``. A record whose ``"v"`` this client does
not know is skipped, and a ``config.enc`` of an unknown format is refused,
never guessed at.

The FIPS boundary
-----------------

The algorithms are FIPS-approved wherever paf runs; whether they run *inside
a FIPS-validated module* is decided by the host:

* All the cryptography is done by the OpenSSL that the ``cryptography``
  package is bound to. On RHEL 9 or Rocky 9 booted with ``fips=1``, the
  distribution's ``python3-cryptography`` uses the system OpenSSL 3 and its
  FIPS provider, which is the validated module (:doc:`installation`).
* The wheels of ``cryptography`` on PyPI bundle their own OpenSSL, which is
  not validated and not in FIPS mode.
* ``cryptography`` offers no public call into OpenSSL's random generator, so
  symmetric keys, nonces and salts come from the kernel (``getrandom``). On
  RHEL 9 and Rocky 9 in FIPS mode that is the kernel crypto API's
  SP 800-90A DRBG, part of the kernel's own validated module. Private keys
  are generated by OpenSSL. Message and temporary file names use Python's
  ``secrets``; they need to be unique, not secret.

At start paf checks whether that OpenSSL is in FIPS mode. When it is not,
it **warns and runs** (the owner's choice): every command prints one line on
stderr, the TUI shows it on its status line, and ``PAF_FIPS_WARNING=0``
silences it. ``paf --version`` always ends with the OpenSSL version and its
FIPS mode.

CI runs the whole suite on Rocky Linux 9's own ``python3-cryptography`` with
OpenSSL in FIPS mode (``OPENSSL_FORCE_FIPS_MODE=1``, RHEL's switch for the
same FIPS provider without booting the host with ``fips=1``). There, X25519
and ChaCha20 are refused by OpenSSL, which is how the job knows FIPS mode is
real.

Migrating from 0.x
------------------

phone_a_friend 0.x used X25519, ChaCha20-Poly1305 and scrypt. Those live on
only in :mod:`phone_a_friend.legacy`, which can **read** 0.x data and never
writes any; it is imported only when 0.x data is met.

* **Your account** moves the first time you log in with 1.0: your
  ``config.enc`` is decrypted the 0.x way and written again with PBKDF2 and
  AES-256-GCM, you get a new P-256 identity key, and your ``identity.json``
  publishes it beside your old X25519 key (``legacy_pub``). The old private
  key stays inside your encrypted config, to read old messages.
* **Your contacts** move when they log in with 1.0 too. Your client swaps a
  contact's old key for the new one only when their ``identity.json`` (which
  only they can write) names the old key you hold as the one it replaced.
  Until a contact has upgraded you can read everything they sent, but cannot
  send to them or invite them: their 0.x client could not read it.
* **Messages** are re-encrypted by their sender: each client rewrites its own
  0.x messages (only a file's owner may replace it in the sticky directory)
  at its first 1.0 login, and again whenever it reads one, as soon as the
  conversation has a new key. ``paf migrate`` does it for every conversation
  at once and lists what is left in the 0.x format: messages from someone
  who has not upgraded yet.
* **Invites** sealed to your old key can still be read. Accepting one waits
  until its sender has upgraded, because the reply is sealed to their new
  key; declining one from a sender still on 0.x just drops it.
* **Mixing versions**: a 0.x client cannot read anything 1.0 writes, which
  is why this is version 1.0.0. Upgrade everyone who shares a directory.

**On a FIPS host**, OpenSSL refuses the 0.x algorithms, so an account made
by 0.x cannot be migrated there: ``paf`` says so and changes nothing. Log in
once where OpenSSL is not in FIPS mode (for example with the PyPI wheel of
``cryptography`` in a virtual environment), then go back to the FIPS host.
0.x messages from contacts who never migrate stay unreadable on a FIPS host.
