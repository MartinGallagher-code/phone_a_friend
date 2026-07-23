# phone_a_friend

[![PyPI](https://img.shields.io/pypi/v/phone-a-friend?label=PyPI)](https://pypi.org/project/phone-a-friend/)
[![Python versions](https://img.shields.io/pypi/pyversions/phone-a-friend)](https://pypi.org/project/phone-a-friend/)
[![CI](https://github.com/MartinGallagher-code/phone_a_friend/actions/workflows/ci.yml/badge.svg)](https://github.com/MartinGallagher-code/phone_a_friend/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/badge/coverage-100%25-brightgreen)](https://github.com/MartinGallagher-code/phone_a_friend/actions/workflows/ci.yml)
[![REUSE status](https://api.reuse.software/badge/github.com/MartinGallagher-code/phone_a_friend)](https://api.reuse.software/info/github.com/MartinGallagher-code/phone_a_friend)
[![License: GPL-3.0-or-later](https://img.shields.io/badge/license-GPL--3.0--or--later-blue)](LICENSE)

Serverless, end-to-end-encrypted chat for people who share access to the same
Linux directory (an NFS mount, a group-writable `/srv/chat`, a shared home
server, ...). There is **no server process**: every client reads and writes
plain files in the shared directory and does all encryption, decryption,
sending and receiving itself. A curses TUI runs in any bash terminal.

```
┌─ INVITES ────────────┬─ chat with bob ────────────────────────────┐
│ ✉ carol (chat)       │ 09:12 bob:   lunch?                        │
│ CHATS                │ 09:13 alice: sure - where?                 │
│  bob                 │ 09:14 bob:   the usual                     │
│  dave ●2             │                                            │
│ GROUPS               ├────────────────────────────────────────────┤
│  #book-club ●1       │ > see you at noo▊                          │
└──────────────────────┴────────────────────────────────────────────┘
```

## Features

* **Register** with a username + passphrase; an X25519 identity keypair is
  generated for you.
* **Invite people to chat** — key exchange is push-based: the invite pushes
  your public key to them; accepting pushes their public key back to you.
  Without an accepted exchange, messages cannot be sent or decrypted.
* **Create groups and invite people** — each group has a random symmetric
  key; inviting someone pushes the group key to them (sealed to their public
  key). Any member can invite others.
* **Send/receive messages** to users or groups. The left-hand pane lists
  invites, chats and groups; click (mouse supported) or use ↑/↓ + Enter to
  open one and read its messages. New messages in the open conversation
  appear immediately; elsewhere an unread badge (`●3`) lights up next to the
  chat or group.
* **Encrypted per-user config** — each client maintains its user's config
  file (private key, contact keys, group keys, read state) in the shared
  directory, encrypted with a key derived from the passphrase (scrypt +
  ChaCha20-Poly1305).

## Security model

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
* Out of scope for v1: forward secrecy/key rotation, sender authentication
  beyond conversation-key possession, traffic analysis (filenames reveal
  timing; directory names reveal who talks to whom), and revoking group keys.

## Install

Requires Python ≥ 3.9 (Linux). From PyPI:

```bash
pip install phone-a-friend     # installs the `paf` command
```

From a checkout:

```bash
pip install .             # or `pip install -e .` for development
# or, without installing:
pip install -r requirements.txt
alias paf='python -m phone_a_friend'
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
users cannot delete each other's files).

## Use

```bash
paf --dir /srv/paf                 # launch the TUI (register on first run)
PAF_DIR=/srv/paf paf               # same, via environment variable
```

In the TUI:

| Key           | Action                                                  |
|---------------|---------------------------------------------------------|
| ↑ / ↓ / click | select a chat, group or invite in the left pane         |
| Enter         | open selection — or send, if the input line has text    |
| Ctrl-N        | invite a user to chat (pushes your public key)          |
| Ctrl-G        | create a group                                          |
| Ctrl-O        | invite a user to the open group (pushes the group key)  |
| PgUp / PgDn   | scroll message history                                  |
| Esc           | clear input line / quit                                 |

Selecting an incoming invite prompts you to accept (`y`) or decline (`n`).

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

Publishing is automated via `.github/workflows/publish.yml` using PyPI
[trusted publishing](https://docs.pypi.org/trusted-publishers/) — no API
token is stored in the repo. One-time setup on pypi.org: add a GitHub
publisher for `MartinGallagher-code/phone_a_friend`, workflow
`publish.yml`, environment `pypi`. Then creating a GitHub release (e.g.
tag `v0.1.0`) builds, checks, and uploads the sdist and wheel.

## Licensing

Licensed under GPL-3.0-or-later. The repository is compliant with the
[REUSE Specification](https://reuse.software/): every file carries SPDX
copyright and license information, and license texts live in `LICENSES/`.
Compliance is checked in CI with `reuse lint`.

## Shared-directory layout

```
<shared>/
  users/<name>/identity.json     public identity (name + public key)
  users/<name>/config.enc        that user's client config, encrypted
  invites/<name>/<id>.json       sealed invites pushed TO <name>
  replies/<name>/<id>.json       sealed invite replies pushed TO <name>
  dm/<a>__<b>/<ts>-<rand>.json   direct messages, pair-key encrypted
  groups/<gid>/meta.json         public group metadata
  groups/<gid>/msgs/<...>.json   group messages, group-key encrypted
```
