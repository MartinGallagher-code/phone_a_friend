.. SPDX-License-Identifier: GPL-3.0-or-later
.. SPDX-FileCopyrightText: 2026 Martin J. Gallagher

Usage
=====

Launching the TUI
-----------------

.. code-block:: bash

   paf --dir /srv/paf                 # launch the TUI (register on first run)
   PAF_DIR=/srv/paf paf               # same, via environment variable

Keys
----

==================  ========================================================
Key                 Action
==================  ========================================================
↑ / ↓               select a chat, group, user or invite in the left pane
Enter               open selection — or send, if the input line has text
Ctrl-N              invite a user to chat (pushes your public key)
Ctrl-G              create a group
Ctrl-O              invite a user to the open group (pushes the group key)
PgUp / PgDn         scroll message history, or the key reference
← / →               move the cursor in the input line
Home / End          start / end of the input line (also Ctrl-A / Ctrl-E)
Backspace / Delete  delete the character before / under the cursor
Esc                 clear input line / quit
==================  ========================================================

Pressing Enter on an incoming invite prompts you to accept (``y``) or
decline (``n``); on a name under USERS it prompts to send them a chat
invite.

The left pane scrolls when it has more rows than the terminal; ▲ and ▼ on
its border show that rows are hidden above or below. The bottom row is the
status line, which always ends with ``/help: keys``. While no conversation is
open, the right pane shows the key reference: the keys, the mouse and the
slash commands. It scrolls with PgUp/PgDn and the wheel, and ``/help`` shows
the same list over an open conversation.

F1, F2, F3, F4, F9 and F10 still do what they did in 0.2.0 (help, invite,
group, group invite, menu, quit), but the screen no longer names them. Many
terminals keep some function keys for themselves: GNOME Terminal, Konsole and
xfce4-terminal open their own manual on F1, often a web page, and some take
F10 for their menu bar. So paf points to ``/help`` and the Ctrl keys, which
reach it in any terminal. 0.2.0's key bar along the bottom is gone.

Typing takes any character — accents, CJK, emoji — exactly as typed, and
widths are counted in terminal columns, so a line of double-width characters
wider than the pane scrolls sideways with the cursor kept on screen. Emoji
widths differ between terminals, so on some the cursor can sit a column off
after an emoji; the text sent is unaffected.

Mouse
-----

============  ==============================================================
Mouse         Action
============  ==============================================================
click         open a chat or group; on an invite or a name under USERS,
              select it
double-click  open or act on any item: answer an invite, invite a user to
              chat
right-click   a menu of the item's actions (below)
wheel         scroll what is under the pointer: the conversation, or the
              left pane (3 lines a notch)
============  ==============================================================

A single click on an invite or a name under USERS only selects it, so a
stray click cannot pop up a question; double-click it, or press Enter.
Scrolling the conversation back to the bottom marks it read, as PgDn does.

The menu (right-click or ``/menu``) offers, for

* a chat: Open, Unfriend, Invite to a group;
* a group: Open, Invite a user, Remove a member, Leave;
* an invite: Accept, Decline;
* a name under USERS: Send a chat invite.

Each item does exactly what its key or slash command does. Esc, or a click
outside the menu, closes it without doing anything.

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
   /leave           leave the open (or selected) group
   /rename NAME     rename the open group (its owner only)
   /delete          delete the open group for everyone (its owner only)
   /menu            the selected item's menu
   /help            the key reference, over an open conversation
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
   paf -d /srv/paf -u alice rename-group "book club" "reading group"
   paf -d /srv/paf -u alice delete-group "reading group"
   paf -d /srv/paf -u bob   status                # contacts, groups, unread
