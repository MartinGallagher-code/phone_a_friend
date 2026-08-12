.. SPDX-FileCopyrightText: 2026 Martin Gallagher
..
.. SPDX-License-Identifier: GPL-3.0-or-later

Usage
=====

Launching the TUI
-----------------

.. code-block:: bash

   paf --dir /srv/paf                 # launch the TUI (register on first run)
   PAF_DIR=/srv/paf paf               # same, via environment variable

Keys
----

===============  ========================================================
Key              Action
===============  ========================================================
↑ / ↓ / click    select a chat, group, user or invite in the left pane
Enter            open selection — or send, if the input line has text
F2 or Ctrl-N     invite a user to chat (pushes your public key)
F3 or Ctrl-G     create a group
F4 or Ctrl-O     invite a user to the open group (pushes the group key)
F10              quit
PgUp / PgDn      scroll message history
Esc              clear input line / quit
===============  ========================================================

Selecting an incoming invite prompts you to accept (``y``) or decline
(``n``); selecting a name under USERS prompts to send them a chat invite.

Slash commands
--------------

Every action is also available as a **slash command** typed into the input
line — these work in any terminal, including ones whose host application
intercepts Ctrl or function keys (the VS Code integrated terminal binds
Ctrl-N/Ctrl-G/Ctrl-O itself):

.. code-block:: text

   /invite USER     invite a user to chat
   /unfriend USER   stop chatting with a user (a new invite can restore it)
   /group NAME      create a group
   /ginvite USER    invite a user to the open (or selected) group
   /gremove USER    remove a user from the open (or selected) group
   /quit            exit

Scripting / headless use
------------------------

Every operation is also available as a subcommand, which is handy for
testing and automation (``--passphrase`` / ``$PAF_PASSPHRASE`` avoid the
prompt):

.. code-block:: bash

   paf -d /srv/paf -u alice register
   paf -d /srv/paf -u alice invite bob            # chat invite
   paf -d /srv/paf -u bob   invites               # list incoming
   paf -d /srv/paf -u bob   accept --from alice
   paf -d /srv/paf -u alice send --to bob "hello"
   paf -d /srv/paf -u bob   read --to alice
   paf -d /srv/paf -u alice create-group "book club"
   paf -d /srv/paf -u alice invite bob --group "book club"
   paf -d /srv/paf -u alice send --group "book club" "welcome"
   paf -d /srv/paf -u alice remove bob            # unfriend
   paf -d /srv/paf -u alice remove bob --group "book club"
   paf -d /srv/paf -u alice remove alice --group "book club"   # leave
   paf -d /srv/paf -u bob   status                # contacts, groups, unread
