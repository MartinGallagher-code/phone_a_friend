.. SPDX-License-Identifier: GPL-3.0-or-later
.. SPDX-FileCopyrightText: 2026 Martin J. Gallagher

Shared-directory layout
=======================

Everything lives as plain files under the shared directory; there is no
database and no server process. The layout:

.. code-block:: text

   <shared>/
     users/<name>/identity.json     public identity (name + public key)
     users/<name>/config.enc        that user's client config, encrypted
     invites/<name>/<id>.json       sealed invites pushed TO <name>
     replies/<name>/<id>.json       sealed invite replies pushed TO <name>
     dm/<a>__<b>/<ts>-<rand>.json   direct messages, pair-key encrypted
     groups/<gid>/meta.json         group metadata, name encrypted
     groups/<gid>/msgs/<...>.json   group messages, group-key encrypted

Every encrypted file (``config.enc``, invites, replies, messages) is a JSON
object with a ``"v"`` field naming its format, currently ``2``, and its
ciphertext in base64 under ``"blob"`` or ``"sealed"``. ``identity.json``
carries ``"v"`` and the P-256 public key, and, for an account migrated from
0.x, ``"legacy_pub"``: the X25519 key it replaced. Files written by 0.x have
no ``"v"`` (:doc:`security`, *Migrating from 0.x*).

``meta.json`` holds the group's id, its owner (the user who created it,
whose directory it is) and when it was made, in the clear, and its name
encrypted (``"name_blob"``: AES-256-GCM under a key derived from the group
key), so only members can read the name. A ``meta.json`` written before 1.2
has its name in the clear; the owner's client encrypts it the next time it
runs.

**Renaming and deleting a group** (PAF-004). Only the owner may, and the
filesystem agrees: the group directory and its ``meta.json`` belong to them.
A rename rewrites ``meta.json`` (atomically, so a reader never sees half a
file), and each member's client takes up the new name on its next poll.
``/delete`` writes ``"deleted_at"``: clients show the group as "(deleted)",
readable and read-only, for **3 days** (``GROUP_GRACE_DAYS``). After that
the next client to run forgets it, renames ``groups/<gid>`` aside to
``groups/.deleted-<gid>-<rand>`` (so a client in the middle of reading finds
it gone, not half-gone) and removes it. Two clients doing that at once are
harmless: one rename wins and the other has nothing to do. With the sticky
drop directories, only the group's owner can remove other people's message
files, so where ``groups/`` is not writable by everyone the directory goes
when the owner's client next runs; every member's client stops showing it
either way.

Drop-box directories (``invites/<name>/``, ``replies/<name>/``) are created
with the sticky bit so users cannot delete each other's files.

The on-disk operations are implemented in :mod:`phone_a_friend.store`.
