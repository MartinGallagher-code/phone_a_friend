.. SPDX-FileCopyrightText: 2026 Martin Gallagher
..
.. SPDX-License-Identifier: GPL-3.0-or-later

phone_a_friend
==============

Serverless, end-to-end-encrypted chat for people who share access to the same
Linux directory (an NFS mount, a group-writable ``/srv/chat``, a shared home
server, ...). There is **no server process**: every client reads and writes
plain files in the shared directory and does all encryption, decryption,
sending and receiving itself. A curses TUI runs in any bash terminal.

.. code-block:: text

   ┌─ INVITES ────────────┬─ chat with bob ────────────────────────────┐
   │ ✉ carol (chat)      │ 09:12 bob:   lunch?                        │
   │ CHATS                │ 09:13 alice: sure - where?                 │
   │  bob                 │ 09:14 bob:   the usual                     │
   │  dave ●2             │                                            │
   │ GROUPS               │                                            │
   │  #book-club ●1       ├────────────────────────────────────────────┤
   │ USERS                │ > see you at noo▊                         │
   │  + erin              │                                            │
   └──────────────────────┴────────────────────────────────────────────┘

Features
--------

* **Register** with a username + passphrase; an X25519 identity keypair is
  generated for you.
* **Invite people to chat** — key exchange is push-based: the invite pushes
  your public key to them; accepting pushes their public key back to you.
  Without an accepted exchange, messages cannot be sent or decrypted.
* **Create groups and invite people** — each group has a random symmetric
  key; inviting someone pushes the group key to them (sealed to their public
  key). Any member can invite others.
* **Unfriend and remove** — stop chatting with a contact (``/unfriend``); a
  future invite rebuilds the friendship and the previous chat becomes
  readable again. Any group member can remove another member (``/gremove``),
  mirroring invites; removing yourself leaves the group.
* **See who's around** — registered users you have not connected with yet
  are listed under USERS in the left pane; select one to send a chat invite.
* **Send/receive messages** to users or groups, with mouse support and
  unread badges.
* **Encrypted per-user config** — each client maintains its user's config
  file (private key, contact keys, group keys, read state) in the shared
  directory, encrypted with a key derived from the passphrase (scrypt +
  ChaCha20-Poly1305).

Contents
--------

.. toctree::
   :maxdepth: 2

   installation
   usage
   security
   storage
   api

Links
-----

* `Project website <https://martingallagher-code.github.io/phone_a_friend/>`_
* `Source code on GitHub <https://github.com/MartinGallagher-code/phone_a_friend>`_
* `Package on PyPI <https://pypi.org/project/phoneafriend/>`_
* `Issue tracker <https://github.com/MartinGallagher-code/phone_a_friend/issues>`_

Licensing
---------

Licensed under GPL-3.0-or-later. The repository is compliant with the
`REUSE Specification <https://reuse.software/>`_: every file carries SPDX
copyright and license information, and license texts live in ``LICENSES/``.
