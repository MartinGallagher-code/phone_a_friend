# Changelog

Every change to phone_a_friend (`phoneafriend` on PyPI, the `paf` command) is
recorded here, newest first. Every change moves the version: a pull request
into `main` raises `__version__` in `phone_a_friend/__init__.py` and opens
this file with that version's own numbered, dated section, never an
"Unreleased" heading left for later. Patch for fixes, docs, tests and CI;
minor for anything new a user can use; major only when something that worked
stops working the way it did. `CLAUDE.md` has the rest.

Sections 0.1.0 to 0.1.3 were written afterwards, from the git history, and
are dated by their uploads to PyPI. Numbers in brackets are pull requests.

## 2.0.0 — 2026-11-17

DM directories no longer show who talks to whom (PAF-008). This is 2.0.0
because a 1.x client looks for DMs under `dm/<a>__<b>` and does not find
what 2.0 writes: upgrade everyone who shares a directory.

### Changed

- **DM directories are named from the pair key**: `dm/<32 hex characters>`,
  the first 16 bytes of HKDF-SHA256 over the two users' P-256 shared secret.
  Only the two of them can compute it; the AEAD's associated data still
  names the pair, so messages decrypt unchanged wherever they sit.
- **Old directories are migrated without losing messages.** 2.0 reads both
  `dm/<a>__<b>` and the new directory, and each client moves the message
  files it owns into the new one (the sticky bit lets only a file's owner
  move it), removing the old directory once it is empty. A contact still on
  0.x keeps the old name until they upgrade.

### Added

- **Docs: what a directory listing still shows**: how many DMs and messages
  there are, sizes and times, and, on a multi-user system, which Unix
  account owns each message file, which encryption cannot hide (README,
  `docs/security.rst`).

## 1.4.0 — 2026-11-17

Search every conversation (PAF-006).

### Added

- **`/search WORDS`**, and **Ctrl-F**, find every message containing all
  the words, ignoring case, in every chat and group you can read. The
  status line counts conversations and hits as it goes, and Esc cancels.
  Hits are listed by conversation (the one with the newest hit first),
  newest first, with the date, the sender and the text around the first
  word. Enter opens the conversation scrolled to the message, highlighted.
- **`paf search WORDS`** prints the same, grouped by conversation.
- **Nothing decrypted is written.** Search decrypts in memory, through the
  TUI's own message cache, so a second search decrypts only what arrived
  since. 5,000 messages are searched in well under a second on a local
  disk (the test allows 1 s for the first results).

## 1.3.0 — 2026-11-17

Presence, typing indicators and read receipts (PAF-005), on by default.

### Added

- **Presence.** Contacts who are active are marked `•` in the left pane,
  idle ones `◦`, and an open chat's title says "(active)", "(idle)" or
  "(away)".
- **Typing.** "bob is typing..." under the conversation while someone in it
  types; it lapses 8 seconds after they stop.
- **Read receipts.** "✓ read by bob, carol" under your newest message, in
  chats and groups.
- **One beacon file per user and device**, `beacons/<name>.<device>.json`:
  presence is its age, and inside it each conversation's read marker and
  typing time are encrypted with keys derived from that conversation's key,
  under tags only its members can recognise. Clients write it at most every
  15 seconds while active (sooner on a change, every 5 s while typing,
  never after 5 idle minutes) and read everyone's with one directory listing
  per poll, opening only files whose modification time moved.
- **`/presence on|off`** and **`paf presence [on|off]`**. Off stops writing
  the beacon and deletes yours; `paf presence` lists your contacts' presence.
- Docs: what an outsider can still see (that a user is active, and when), in
  `docs/security.rst`; the beacon format, write throttling and the NFS
  attribute-caching caveat in `docs/storage.rst`.

## 1.2.0 — 2026-11-17

Groups can be renamed and deleted, and their names are no longer in the
clear (PAF-004).

### Added

- **`/rename NAME` and `/delete`** for the open group, also on the group's
  right-click menu, and `paf rename-group` / `paf delete-group`. Only the
  group's owner (the user who created it, as `meta.json` records) may; the
  others are told who can.
- **A deleted group stays readable for 3 days.** It is marked "(deleted)"
  in the left pane, its title says until when, and it is read-only. After 3
  days the next client to run removes it: it renames the directory aside and
  then deletes it, so two clients doing it at once do no harm.
- **Members see a rename** on their next poll, with a notice.

### Changed

- **Group names are encrypted** in `meta.json`, under a key derived from the
  group key, so a listing of the shared directory no longer shows them. The
  owner's client encrypts a name written by an earlier version the next time
  it runs; until then members read it as before.

## 1.1.0 — 2026-11-17

phone_a_friend as one file, `paf.pyz`, that runs with `python3 paf.pyz`, with
no root, no pip and nothing to install (PAF-007).

### Added

- **`paf.pyz`**, a zipapp of the pure-Python package, attached to every
  GitHub release by `publish.yml`. `tools/build_pyz.py` builds it. It holds
  no compiled code and no OpenSSL: it uses the host's
  `python3-cryptography`, so on a FIPS host it runs on the system's
  validated OpenSSL.
- **A plain message when the host cannot run it.** Without
  `python3-cryptography`, `paf.pyz` prints one line naming the package and
  exits with status 1, and on a Python older than 3.9 it says which version
  it needs.
- **CI**: builds `paf.pyz`, checks it holds no `.so`, then on a clean Rocky
  Linux 9 container with only dnf's `python3-cryptography` (no pip), runs
  it as an unprivileged user: two users register, invite, accept, send and
  read, with OpenSSL in and out of FIPS mode; then removes the package and
  checks the one-line message and exit 1. `tests/test_pyz.py` does the same
  locally.
- **Docs**: the installation page and README lead with the single file and
  say which distributions' Python qualifies.

## 1.0.0 — 2026-11-17

phone_a_friend now uses only FIPS-approved cryptography (PAF-003). This is
1.0.0 because the shared directory's formats changed: a 0.x client cannot
read what 1.0 writes. 1.0 reads everything 0.x wrote and migrates it, so
upgrade everyone who shares a directory.

### Changed

- **FIPS-approved primitives only.** Identity keys, DM key agreement and
  sealed invites are ECDH on NIST P-256 (was X25519); messages, invites and
  the config are AES-256-GCM with a 96-bit random nonce (was
  ChaCha20-Poly1305); the passphrase key is PBKDF2-HMAC-SHA256 with 600,000
  iterations (was scrypt N=2^14); HKDF-SHA256 stays. Group messages use a
  key derived from the group key rather than the group key itself.
- **Every encrypted record names its format**: `"v": 2` in `config.enc`,
  invites, replies, messages and `identity.json`, and `config.enc` records
  its KDF, iteration count and AEAD. A record of an unknown format is
  skipped, and such a `config.enc` refused, never misread.
- **0.x data is migrated, not dropped.** The first 1.0 login moves an
  account to a new P-256 identity, keeping its X25519 key only to read old
  messages; `identity.json` publishes the new key beside the old one, and a
  contact's client takes it up only when the old key matches the one it
  holds. Each client re-encrypts its own 0.x messages once the conversation
  has a new key. Until a contact upgrades, their messages read but nothing
  can be sent to them. The 0.x algorithms are in `phone_a_friend/legacy.py`,
  read-only, imported only when 0.x data is met.

### Added

- **FIPS mode is reported.** `paf --version` ends with the OpenSSL in use
  and whether it is in FIPS mode. When it is not, each command warns once on
  stderr and runs, and the TUI says so on its status line;
  `PAF_FIPS_WARNING=0` silences it.
- **`paf migrate`** re-encrypts every 0.x message of yours that can be, and
  lists what is still in the 0.x format.
- **CI's fips job** runs the suite on Rocky Linux 9's own
  `python3-cryptography` with OpenSSL in FIPS mode, plus the two-user smoke
  test. Known-answer tests for AES-256-GCM, PBKDF2-HMAC-SHA256 and
  HKDF-SHA256, a check that nothing but `legacy.py` imports X25519,
  ChaCha20-Poly1305 or scrypt, and migration tests against a directory
  written the way 0.3.0 wrote it.
- **Docs**: `docs/security.rst` lists the primitives and their standards,
  the FIPS boundary and how migration works; `docs/installation.rst` says how
  to run on a FIPS host.

## 0.3.0 — 2026-11-17

The key bar along the bottom is gone, and the key reference is on screen
instead (PAF-002). Nothing in the shared directory changed: 0.1.x, 0.2.0 and
this client chat with each other in the same directory.

### Changed

- **The key bar is gone.** 0.2.0 drew `1Help 2Invite 3Group 4GInvite 9Menu
  10Quit` on the bottom row, and a click on a label pressed that key. It did
  not work as it looked. Several of its keys are ones terminals keep for
  themselves: GNOME Terminal, Konsole and xfce4-terminal open their own
  manual on F1 (often a web page), and some take F10 for their menu bar. Its
  labels were buttons only where the terminal reports clicks. The bottom row
  is now the status line, and the conversation and the left pane each get a
  row back. Prompts (accept an invite, a group's name, quit) appear there,
  as before.
- **Nothing on screen asks for a function key.** The status line always
  ends with `/help: keys`. Its standing hint, the help box, the welcome text
  and the messages that used to name F3 or F4 point to the Ctrl keys and the
  slash commands, which reach paf in any terminal.
- **The key reference is on screen.** With no conversation open, the right
  pane is the whole reference: the keys, the mouse and the slash commands.
  It reads from the top and scrolls with PgUp/PgDn and the wheel, with ▲ / ▼
  where lines are hidden. `/help` shows the same list over an open
  conversation.
- **F1, F2, F3, F4, F9 and F10 still work** where the terminal passes them
  on, doing what they did in 0.2.0. They are only no longer shown.
- **The smallest screen is a row shorter:** 6 rows by 40 columns, since the
  bar's row is free.
- paf never started a browser or any other program, and it still does not:
  a web page on F1 was the terminal's own help.

### Docs

- The README, `docs/usage.rst`, `docs/index.html` and the `tui` module's
  docstring describe the screen without the bar.
- The four screens on the project page are new captures of this version,
  taken from `paf` running in a terminal emulator against a shared directory
  set up as before.

### Tests

- The key bar's tests are replaced:
  - a click on any column of the bottom row (now the status line) runs no
    action, at 24 rows and at 12;
  - the status line is the last row, ends with `/help: keys`, and a prompt
    is drawn there.
- **No function key named:** nothing paf draws names one (at start-up, in a
  conversation, in the help box, in a menu, and in the messages that did),
  and nor do the hint, help and welcome texts.
- **The reference matches the code:** it lists exactly the commands `/help`'s
  usage line names, its Ctrl keys are bound to what it says, and it fits the
  welcome pane.
- **The welcome pane scrolls:** with PgUp/PgDn and the wheel, its last line
  can be reached on a 12-row screen, and an open conversation replaces it.
- **The smallest screen,** 6 × 40, draws the input line, and one row fewer is
  "too small".
- **No other program:** importing the TUI and the command imports neither
  `webbrowser` nor `subprocess`.
- **Rows that moved:** the mouse tests that name a row moved down with it.
  148 tests; coverage stays at 100%.

## 0.2.0 — 2026-11-16

The TUI's keyboard and mouse now work the way Meridian Commander's do
(PAF-001). Nothing in the shared directory changed: 0.1.x clients and this
one chat with each other in the same directory.

0.1.4 was not published to PyPI on its own: PyPI goes from 0.1.3 to 0.2.0,
so this release also carries 0.1.4's changes, below.

### Fixed

- **Typed text in any language arrives as typed.** Keys were read one byte
  at a time, so "é" became "Ã©", and that corrupted text was what got
  encrypted and sent. Keys are now read with `get_wch()`, whole, in the
  input line and in every prompt (F2, F3, F4 and the yes/no questions).
- **Keys that are not text no longer type characters.** Left, Home, Delete,
  F1, F5 and the like each used to leave a stray character (`Ą`, `Ć`, …) in
  the input line or a prompt.
- **The left pane scrolls.** With more chats, groups and users than the
  terminal has rows, the rest could not be seen or clicked, and the
  selection walked off the screen.
- **A slow click cannot answer a question.** The release of a click that
  opened a prompt was typed into it, so the answer was neither yes nor no.
  Mouse events now never type into a prompt.

### Added

- **An editable input line:** ← and → move the cursor, Home and End (and
  Ctrl-A and Ctrl-E) jump to its ends, Backspace and Delete remove the
  character before or under the cursor, and a line wider than the pane
  scrolls sideways. Widths are counted in terminal columns, so CJK and emoji
  take two.
- **The mouse wheel** scrolls whatever is under the pointer, three lines a
  notch: the conversation (back at the bottom, it is marked read) or the left
  pane.
- **Double-click** opens or acts on any item. A single click still opens a
  chat or group; on an invite or a name under USERS it only selects, so a
  stray click cannot pop up a question.
- **A right-click menu** of what you can do with an item: Open, Unfriend and
  Invite to a group on a chat; Open, Invite a user, Remove a member and Leave
  on a group; Accept and Decline on an invite; Send a chat invite on a name
  under USERS. **F9** and **`/menu`** open it on the selected item.
- **A key bar** along the bottom, `1Help 2Invite 3Group 4GInvite 9Menu
  10Quit`, whose keys can be clicked. Status messages moved to a line just
  above it, so the conversation is one row shorter.
- **F1 and `/help`** open a help screen with the keys, the mouse and the
  slash commands.
- **`/leave`** leaves the open (or selected) group, as the menu's Leave does.

### Changed

- Esc acts at once: curses waited a second after it for the rest of an
  escape sequence, and now waits 25 ms, as Meridian does. An `ESCDELAY` set
  in the environment is left alone.
- The welcome screen describes the mouse and points at F1.
- The README, the Read the Docs usage page and the GitHub Pages site describe
  the new keys and the mouse, and the site's four TUI captures were taken
  again from this version.

## 0.1.4 — 2026-11-16

The first numbered section since 0.1.3. It also covers four changes that
reached `main` after 0.1.3 was published, without moving the number, so
`main` said 0.1.3 but no longer matched what PyPI has as 0.1.3. Publishing
0.1.4 to PyPI is a separate step.

### Added

- **Every change moves the version.** `tests/check_version.sh`, copied from
  shared_tools' template, fails when `__version__` and the top of this file
  disagree, or when this file has an "Unreleased" heading. CI's new
  `version` job runs it, and also fails a pull request whose version is not
  greater than its base branch's, and a push to `main` whose version is not
  greater than what `main` had before it.
- **This changelog**, with sections for 0.1.0 to 0.1.3 rebuilt from the git
  history.
- **`CLAUDE.md`**, notes for Claude sessions working here: what the
  repository is, its checks, the version rule, the shared ticket board and
  bundling. phone_a_friend is now a project on that board, with tickets
  PAF-001 onwards (shared_tools ST-031).

### Changed

- The README's *Releasing to PyPI* section says every pull request moves the
  version and adds its section here, and that publishing is a separate step.
- `paf --version` gives the copyright holder as "Martin J. Gallagher", with
  the middle initial every other tool in the suite uses (#14).
- The licence notices match the rest of the suite: the two-line SPDX header
  in every file, the same README badge and *License* section, and a
  `REUSE.toml` that covers everything else, so `reuse lint` passes (#15).
- The README's licence section has no relative links, which were dead on
  PyPI and in Sphinx builds (#15).
- CI keeps its build artifacts for 7 days instead of 90 (#15).
- CI tests Python 3.9 on Ubuntu 24.04: `ubuntu-latest` is now Ubuntu 26.04,
  which has no Python 3.9 for `setup-python`. 3.11 and 3.13 stay on
  `ubuntu-latest`, and 3.9 is still the floor.

## 0.1.3 — 2026-08-12

### Added

- Documentation on Read the Docs, <https://phoneafriend.readthedocs.io/>: a
  Sphinx site with installation, usage, the security model, the
  shared-directory layout and an API reference, built with warnings as
  errors. The README and the PyPI page link to it (#12).

### Fixed

- The `tui` module's docstring is valid reStructuredText, so the API
  reference builds without warnings (#12).

## 0.1.2 — 2026-07-23

### Added

- **Unfriend and remove.** `paf remove USER [--group GROUP]`, and `/unfriend`
  and `/gremove` in the TUI: stop chatting with a contact, or remove a
  member from a group (removing yourself leaves it). The other side gets a
  sealed notice and forgets the keys. Message files stay, so a later invite
  makes the old conversation readable again. Removal is cooperative, and the
  group key is not rotated (#9).
- F10 quits the TUI, and the open conversation closes by itself when the
  peer unfriends you or you are removed from the group (#9).
- A project website on GitHub Pages, with real TUI screenshots, deployed by
  `pages.yml` (#10).

### Changed

- The PyPI page's Homepage is the GitHub Pages site (#11).

## 0.1.1 — 2026-07-23

### Added

- `paf --version`, GNU style: the name and version, the copyright, the
  licence and the warranty notice (#5, #6).

### Changed

- The version is written in one place, `__version__` in
  `phone_a_friend/__init__.py`, and `pyproject.toml` reads it from there
  (#6).
- The screen sketch at the top of the README lines up.

## 0.1.0 — 2026-07-23

The first release, on PyPI as `phoneafriend`.

- Serverless, end-to-end-encrypted chat for people who share a Linux
  directory. There is no server process: every client reads and writes plain
  files there and does its own encryption (#1).
- Register with a username and passphrase, which makes an X25519 identity
  key. Invites exchange keys for one-to-one chats. Groups have a symmetric
  key that any member can pass on by invite. Each user's config is kept in
  the shared directory, encrypted with a key derived from the passphrase
  (scrypt and ChaCha20-Poly1305) (#1).
- A curses TUI, with mouse support and unread badges, and `paf` commands for
  scripting: `register`, `invite`, `invites`, `accept`, `create-group`,
  `send`, `read` and `status` (#1).
- The TUI lists registered users you have not connected with under USERS, so
  two new users can find each other. F2, F3 and F4, and the slash commands
  `/invite`, `/group`, `/ginvite` and `/quit`, work where a host application
  takes Ctrl-N, Ctrl-G or Ctrl-O (#2).
- Packaging for PyPI, CI with a 100% coverage gate, and REUSE compliance
  (#2). Publishing by PyPI trusted publishing on a release, a `v*` tag or a
  manual run (#3), under the distribution name `phoneafriend` (#4).
