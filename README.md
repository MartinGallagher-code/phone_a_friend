# phone_a_friend

[![PyPI](https://img.shields.io/pypi/v/phoneafriend?label=PyPI)](https://pypi.org/project/phoneafriend/)
[![Python versions](https://img.shields.io/pypi/pyversions/phoneafriend)](https://pypi.org/project/phoneafriend/)
[![CI](https://github.com/MartinGallagher-code/phone_a_friend/actions/workflows/ci.yml/badge.svg)](https://github.com/MartinGallagher-code/phone_a_friend/actions/workflows/ci.yml)
[![Documentation Status](https://readthedocs.org/projects/phoneafriend/badge/?version=latest)](https://phoneafriend.readthedocs.io/en/latest/)
[![Coverage](https://img.shields.io/badge/coverage-100%25-brightgreen)](https://github.com/MartinGallagher-code/phone_a_friend/actions/workflows/ci.yml)
[![REUSE status](https://api.reuse.software/badge/github.com/MartinGallagher-code/phone_a_friend)](https://api.reuse.software/info/github.com/MartinGallagher-code/phone_a_friend)
[![License: GPL-3.0-or-later](https://img.shields.io/badge/license-GPL--3.0--or--later-blue.svg)](https://www.gnu.org/licenses/gpl-3.0.html)

**Website:** <https://martingallagher-code.github.io/phone_a_friend/>
**Documentation:** <https://phoneafriend.readthedocs.io/>

Serverless, end-to-end-encrypted chat for people who share access to the same
Linux directory (an NFS mount, a group-writable `/srv/chat`, a shared home
server, ...). There is **no server process**: every client reads and writes
plain files in the shared directory and does all encryption, decryption,
sending and receiving itself. A curses TUI runs in any bash terminal.

```
┌─ INVITES ────────────┬─ chat with bob ────────────────────────────┐
│ ✉ carol (chat)      │ 09:12 bob:   lunch?                        │
│ CHATS                │ 09:13 alice: sure - where?                 │
│  bob                 │ 09:14 bob:   the usual                     │
│  dave ●2             │                                            │
│ GROUPS               │                                            │
│  #book-club ●1       ├────────────────────────────────────────────┤
│ USERS                │ > see you at noo▊                         │
│  + erin              │                                            │
├──────────────────────┴────────────────────────────────────────────┤
│ Enter open/send  right-click or /menu: menu           /help: keys │
└───────────────────────────────────────────────────────────────────┘
```

## Features

* **Register** with a username + passphrase; a P-256 identity keypair is
  generated for you.
* **Invite people to chat** — key exchange is push-based: the invite pushes
  your public key to them; accepting pushes their public key back to you.
  Without an accepted exchange, messages cannot be sent or decrypted. Every
  invite and reply is checked against the sender's registered key before it
  is acted on, and a contact's key is never swapped behind your back (see
  *Security model*, below).
* **Verify a contact** — `/verify USER` (or `paf verify USER`) shows the
  pair's *safety number*, 60 digits that are the same on both sides. Compare
  it in person or by phone; if it matches, mark the contact verified and
  the left pane shows `✓` beside them. If a contact's key ever changes, paf
  keeps the old one, shows `!`, and says "bob's key changed — /verify bob".
* **Create groups and invite people** — each group has a random symmetric
  key; inviting someone pushes the group key to them (sealed to their public
  key). Any member can invite others.
* **Rename and delete groups** — the group's owner (whoever created it) can
  rename it (`/rename NAME`) or delete it (`/delete`). A deleted group stays
  readable, read-only, for 3 days, marked "(deleted)", and then the next
  client to run removes it. Group names are encrypted, so a listing of the
  shared directory does not show them.
* **Unfriend and remove** — stop chatting with a contact (`/unfriend`); a
  future invite rebuilds the friendship and the previous chat becomes
  readable again. Any group member can remove another member (`/gremove`),
  mirroring invites; removing yourself leaves the group. Removal is
  cooperative: the removed client discards its keys when it sees the notice
  (the group key is not rotated).
* **Presence, typing and read receipts** — the left pane marks contacts who
  are active (`•`) or idle (`◦`), the conversation shows "bob is typing..."
  and "✓ read by bob" under your newest message. Each client keeps one small,
  encrypted beacon file in the shared directory. On by default;
  `/presence off` (or `paf presence off`) stops writing it.
* **Search** — `/search WORDS` (or Ctrl-F) finds every message containing all
  the words, ignoring case, in every chat and group you can read. Hits are
  listed by conversation, newest first; Enter opens the conversation at the
  message. Esc cancels a search in progress. What is decrypted stays in
  memory: no index is written anywhere.
* **See who's around** — registered users you have not connected with yet
  are listed under USERS in the left pane; double-click one (or select it
  and press Enter) to send a chat invite.
* **Send/receive messages** to users or groups. The left-hand pane lists
  invites, chats and groups; click one, or use ↑/↓ + Enter, to open it and
  read its messages. New messages in the open conversation appear
  immediately; elsewhere an unread badge (`●3`) lights up next to the chat or
  group. Type in any language: accents, CJK and emoji go in exactly as typed,
  and the input line can be edited (←/→, Home/End, Delete).
* **Mouse, as in Meridian Commander** — click, double-click, right-click for
  a menu of what you can do with a chat, group, invite or user, and the
  wheel to scroll.
* **The keys on screen** — with no conversation open, the right pane is the
  whole key reference, and `/help` shows it over an open one. Every terminal
  passes `/help` on, which is not true of F1.
* **Encrypted per-user config** — each client maintains its user's config
  file (private key, contact keys, group keys, read state) in the shared
  directory, encrypted with a key derived from the passphrase (PBKDF2-SHA256
  + AES-256-GCM).
* **FIPS-approved cryptography only** — P-256 ECDH, AES-256-GCM, HKDF-SHA256
  and PBKDF2-HMAC-SHA256, on whatever OpenSSL `cryptography` is bound to.
  Bound to a FIPS-validated OpenSSL in FIPS mode (RHEL 9 or Rocky 9 with
  their own `python3-cryptography`), that is FIPS-validated crypto; anywhere
  else paf warns at start, and runs.

## Security model

* Access control is layered: the filesystem permissions of the shared
  directory decide *who can see the files at all*; encryption decides *who
  can read the messages*.
* Direct messages are encrypted with a key derived from the static-static
  P-256 Diffie-Hellman secret of the two participants — readable only by
  someone holding one of the two private keys **and** the peer's public key.
  That holds as long as the key you hold for a contact really is theirs,
  which is what the next points are for.
* **Invites and replies are checked before anything is done with them**
  (since 2.1): the sender they name must be a registered user, a key they
  carry must be the one that user's `identity.json` publishes, an
  acceptance must answer an invite you sent, and on a multi-user system the
  file must belong to the sender's own Unix account. Declines, group joins
  and removals carry no key, so they are signed (HMAC-SHA256 under the
  sender's and recipient's ECDH secret; a group removal also needs the group
  key, so only a member can send one). Anything else is refused, with a
  notice naming who it claimed to be from. No one on the share can put their
  key in place of a contact's, invite you in someone else's name, end your
  chats or throw you out of a group.
* **A contact's key is never replaced.** If their `identity.json` starts
  publishing another key (they made their account again, or someone
  tampered with it), paf keeps the old one, marks them `!`, and tells you to
  `/verify` them. The safety number then shown is for the new key; accept
  it only if it matches theirs.
* Group messages are encrypted with the group's symmetric key, held only by
  members who accepted an invite.
* Invites and invite replies are "sealed" (ephemeral P-256 ECDH → HKDF →
  AES-256-GCM) to the recipient's public identity key.
* All ciphertexts are authenticated (AEAD); tampered files are ignored.
* Every encrypted file names its format (`"v": 2`). Accounts and messages
  that 0.x wrote with X25519, ChaCha20-Poly1305 and scrypt are migrated, not
  dropped: see
  [Migrating from 0.x](https://phoneafriend.readthedocs.io/en/latest/security.html#migrating-from-0-x).
* Out of scope: forward secrecy/key rotation; authenticating each
  *message* beyond conversation-key possession (anyone holding a group's key
  can write as any member of it); a user's *first* key, which is taken from
  their `identity.json` on trust, so compare safety numbers for anyone who
  matters; traffic analysis (filenames reveal timing; file ownership and
  counts are visible, below); and revoking group keys. See
  [What is authenticated](https://phoneafriend.readthedocs.io/en/latest/security.html#what-is-authenticated).

### What a directory listing still shows

DM directories are named `dm/<32 hex characters>`, derived from the pair's
shared key, so only the two people in a DM can tell whose it is. Anyone who
can list the shared directory still sees:

* **how many** DMs and groups there are, and how many messages each holds;
* **sizes and times**: each message's file name starts with when it was
  sent, and every file has a size and modification time;
* **who wrote each file**: on a multi-user system every message file is
  owned by its sender's Unix account, so `ls -l` in a DM directory shows
  the two accounts writing there. Encryption cannot hide that; only a
  shared service account or a setuid helper could;
* user names (`users/`, `invites/`, `replies/`, `beacons/`), group owners,
  and when people are active (see Presence in the security docs);
* while a pair moves from 1.x, the old `dm/<a>__<b>` directory, until both
  people have run 2.0 once (each client moves the files its user owns).

## Install

**One file, no root, no pip.** Download `paf.pyz` from the
[latest release](https://github.com/MartinGallagher-code/phone_a_friend/releases/latest)
and run it:

```bash
python3 paf.pyz --help       # or: chmod +x paf.pyz && ./paf.pyz
```

It needs Python ≥ 3.9 and the distribution's `python3-cryptography`, which
most hosts already have (RHEL 9, Rocky 9, Fedora, Debian 11+, Ubuntu
22.04+). Without it, `paf.pyz` says so in one line and exits. The file
carries no compiled code and no OpenSSL of its own, so on a FIPS host it uses
the system's FIPS-validated OpenSSL. RHEL 8's default `python3` is 3.6, too
old.

Or from PyPI:

```bash
pip install phoneafriend     # installs the `paf` command
```

From a checkout:

```bash
pip install .             # or `pip install -e .` for development
# or, without installing:
pip install -r requirements.txt
alias paf='python -m phone_a_friend'
python3 tools/build_pyz.py   # builds dist/paf.pyz
```

## Set up a shared directory

Any directory all participants can read and write works. Typical setup with
a shared POSIX group:

```bash
sudo mkdir -p /srv/paf
sudo chgrp chatters /srv/paf
sudo chmod 2770 /srv/paf     # rwx for the group, setgid, nothing for others
```

The client creates its own subdirectories (drop-boxes get the sticky bit so
users cannot delete each other's files). `users/`, `invites/` and `replies/`
are sticky too, so no one can rename another user's directory away and put
their own `identity.json` in its place: a new directory gets mode 1777, and
the client adds the sticky bit to one its user owns. For a directory made
before 2.1 by someone else, its owner (or root) can run
`chmod +t /srv/paf/users /srv/paf/invites /srv/paf/replies`.

## Use

```bash
paf --dir /srv/paf                 # launch the TUI (register on first run)
PAF_DIR=/srv/paf paf               # same, via environment variable
```

In the TUI:

| Key                | Action                                                 |
|--------------------|--------------------------------------------------------|
| ↑ / ↓              | select a chat, group, user or invite in the left pane  |
| Enter              | open selection — or send, if the input line has text   |
| Ctrl-N             | invite a user to chat (pushes your public key)         |
| Ctrl-G             | create a group                                         |
| Ctrl-O             | invite a user to the open group (pushes the group key) |
| PgUp / PgDn        | scroll message history, or the key reference           |
| ← / →              | move the cursor in the input line                      |
| Home / End         | start / end of the input line (also Ctrl-A / Ctrl-E)   |
| Backspace / Delete | delete the character before / under the cursor         |
| Esc                | clear input line / quit                                |

Pressing Enter on an incoming invite prompts you to accept (`y`) or decline
(`n`); on a name under USERS it prompts to send them a chat invite.

The left pane scrolls when it has more rows than the terminal; ▲ and ▼ on its
border show that rows are hidden above or below. The bottom row is the status
line, which always ends with `/help: keys`. While no conversation is open, the
right pane shows the key reference: the keys, the mouse and the slash
commands. It scrolls with PgUp/PgDn and the wheel, and `/help` shows the same
list over an open conversation.

F1, F2, F3, F4, F9 and F10 still do what they did in 0.2.0 (help, invite,
group, group invite, menu, quit), but the screen no longer names them. Many
terminals keep some function keys for themselves: GNOME Terminal, Konsole and
xfce4-terminal open their own manual on F1, often a web page, and some take
F10 for their menu bar. So paf points to `/help` and the Ctrl keys, which
reach it in any terminal. 0.2.0's key bar along the bottom is gone (PAF-002).

Typing takes any character — accents, CJK, emoji — exactly as typed, and
widths are counted in terminal columns, so a line of double-width characters
wider than the pane scrolls sideways with the cursor kept on screen. (Emoji
widths differ between terminals, so on some the cursor can sit a column off
after an emoji; the text sent is unaffected.)

### Mouse

| Mouse        | Action                                                          |
|--------------|-----------------------------------------------------------------|
| click        | open a chat or group; on an invite or a name under USERS, select it |
| double-click | open or act on any item: answer an invite, invite a user to chat |
| right-click  | a menu of the item's actions (below)                            |
| wheel        | scroll what is under the pointer: the conversation, or the left pane (3 lines a notch) |

A single click on an invite or a name under USERS only selects it, so a
stray click cannot pop up a question; double-click it, or press Enter.
Scrolling the conversation back to the bottom marks it read, as PgDn does.

The menu (right-click or `/menu`) offers, for

* a chat: Open, Unfriend, Verify (safety number), Invite to a group;
* a group: Open, Invite a user, Remove a member, Leave;
* an invite: Accept, Decline;
* a name under USERS: Send a chat invite.

Each item does exactly what its key or slash command does. Esc, or a click
outside the menu, closes it without doing anything.

Every action is also available as a **slash command** typed into the input
line — these work in any terminal, including ones whose host application
intercepts Ctrl or function keys (the VS Code integrated terminal binds
Ctrl-N/Ctrl-G/Ctrl-O itself):

```
/invite USER     invite a user to chat
/unfriend USER   stop chatting with a user (a new invite can restore it)
/verify USER     compare safety numbers with a contact, to check their key
/group NAME      create a group
/ginvite USER    invite a user to the open (or selected) group
/gremove USER    remove a user from the open (or selected) group
/leave           leave the open (or selected) group
/rename NAME     rename the open group (its owner only)
/delete          delete the open group for everyone (its owner only)
/presence on|off share when you are active, typing, and what you have read
/search WORDS    every message containing all the words, in every chat and group
/menu            the selected item's menu
/help            the key reference, over an open conversation
/quit            exit
```

### Scripting / headless use

Every operation is also available as a subcommand, which is handy for
testing and automation (`--passphrase` / `$PAF_PASSPHRASE` avoid the prompt):

```bash
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
paf -d /srv/paf -u alice verify bob            # safety number; mark verified
paf -d /srv/paf -u alice remove bob --group "book club"
paf -d /srv/paf -u alice remove alice --group "book club"   # leave
paf -d /srv/paf -u alice rename-group "book club" "reading group"
paf -d /srv/paf -u alice delete-group "reading group"
paf -d /srv/paf -u alice presence              # contacts: active / idle / away
paf -d /srv/paf -u alice presence off          # stop sharing yours
paf -d /srv/paf -u alice search pizza friday    # messages with both words
paf -d /srv/paf -u bob   status                # contacts, groups, unread
```

## Test

```bash
python -m unittest discover -s tests -v
```

Coverage is 100% and enforced in CI (`fail_under = 100` in `pyproject.toml`):

```bash
pip install coverage
coverage run -m unittest discover -s tests && coverage report
```

## Releasing to PyPI

The version is single-sourced from `__version__` in
`phone_a_friend/__init__.py`. Every change moves it: each pull request bumps
it there (and only there) and opens `CHANGELOG.md` with that version's own
dated section, and CI's `version` job (`tests/check_version.sh`) fails a pull
request that does not. Publishing is a separate step, taken when wanted.

Publishing is automated via `.github/workflows/publish.yml` using PyPI
[trusted publishing](https://docs.pypi.org/trusted-publishers/) — no API
token is stored in the repo. One-time setup on pypi.org: add a GitHub
publisher for `MartinGallagher-code/phone_a_friend`, workflow
`publish.yml`, environment `pypi`. Then creating a GitHub release (e.g.
tag `v0.1.0`) builds, checks, and uploads the sdist and wheel. Pushing a `v*`
tag, or running the workflow by hand (Actions → Publish to PyPI → Run
workflow), does the same, and a release also attaches `paf.pyz`. A version
already on PyPI is skipped rather than failing the run, so a release made
after its version was uploaded still goes green and still attaches
`paf.pyz`.

## Shared-directory layout

```
<shared>/
  users/<name>/identity.json     public identity (name + public key)
  users/<name>/config.enc        that user's client config, encrypted
  invites/<name>/<id>.json       sealed invites pushed TO <name>
  replies/<name>/<id>.json       sealed invite replies pushed TO <name>
  dm/<hidden>/<ts>-<rand>.json   direct messages, pair-key encrypted
  groups/<gid>/meta.json         public group metadata
  groups/<gid>/msgs/<...>.json   group messages, group-key encrypted
```

## License

Copyright (C) 2026 Martin J. Gallagher

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with
this program. If not, see <https://www.gnu.org/licenses/>.

Full text: the `LICENSE` file at the repository root, or
<https://www.gnu.org/licenses/gpl-3.0.html> — SPDX identifier
`GPL-3.0-or-later`. This repository follows the
[REUSE Specification](https://reuse.software/): source files carry SPDX
copyright and licence headers, and the licence text lives in `LICENSES/`.
