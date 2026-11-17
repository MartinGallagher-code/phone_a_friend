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
     dm/<hidden>/<ts>-<rand>.json   direct messages, pair-key encrypted
     groups/<gid>/meta.json         group metadata, name encrypted
     groups/<gid>/msgs/<...>.json   group messages, group-key encrypted
     beacons/<name>.<device>.json   presence, typing and read receipts

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

**DM directories** (PAF-008) are named ``dm/<32 hex characters>``,
derived from the pair key, which only the two people can compute
(:doc:`security`, *What a listing still shows*). Directories named
``dm/<a>__<b>`` by earlier versions are still read; each client moves the
message files it owns into the new directory and removes the old one when
it is empty. A contact still on 0.x keeps the old name until they upgrade.

**Beacons** (PAF-005). Each client keeps one file,
``beacons/<name>.<device>.json``, where ``<device>`` is eight hex characters
of a hash of the host name (not the name itself). Its modification time is
the user's presence: written in the last minute is *active*, in the last 15
minutes *idle*, otherwise *away*. Inside, one entry per conversation, filed
under a tag derived from that conversation's key and encrypted under another
key derived from it: the user's read marker there, and when they last typed
in it. The TUI rewrites it at most every 15 seconds while you are active,
sooner (but not within 2 seconds) when what it says changes, every 5 seconds
while you type, and not at all once you have done nothing for 5 minutes. To
read everyone's, each poll does one directory listing of ``beacons/`` and a
``stat`` per file, and opens a file only when its modification time moved.
A typing indicator lapses after 8 seconds without a refresh.

**On NFS**, attribute caching (``acregmin``/``acdirmin``, 3 to 60 seconds by
default) delays what other clients see, so presence is coarse and a typing
indicator can arrive late or not at all. Read receipts are not affected
beyond that delay.

Drop-box directories (``invites/<name>/``, ``replies/<name>/``) are created
with the sticky bit so users cannot delete each other's files.

The on-disk operations are implemented in :mod:`phone_a_friend.store`.
