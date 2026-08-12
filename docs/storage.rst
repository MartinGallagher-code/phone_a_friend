.. SPDX-FileCopyrightText: 2026 Martin Gallagher
..
.. SPDX-License-Identifier: GPL-3.0-or-later

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
     groups/<gid>/meta.json         public group metadata
     groups/<gid>/msgs/<...>.json   group messages, group-key encrypted

Drop-box directories (``invites/<name>/``, ``replies/<name>/``) are created
with the sticky bit so users cannot delete each other's files.

The on-disk operations are implemented in :mod:`phone_a_friend.store`.
