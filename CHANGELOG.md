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
