# Notes for Claude

## What this is

phone_a_friend is serverless, end-to-end-encrypted chat for people who share
a Linux directory, with a curses TUI. There is no server process: every
client reads and writes plain files in the shared directory and does its own
encryption. It is on PyPI as `phoneafriend` (no separators), the command is
`paf`, and the import package is `phone_a_friend`.

- `phone_a_friend/crypto.py`: FIPS-approved primitives only: P-256 ECDH
  identity keys, AES-256-GCM, HKDF-SHA256, sealed invites, and the
  PBKDF2-HMAC-SHA256 key that protects each user's config (PAF-003).
- `phone_a_friend/legacy.py`: the 0.x primitives (X25519, ChaCha20-Poly1305,
  scrypt), read-only, imported only to migrate 0.x data. Nothing else may
  import them; `tests/test_fips.py` checks.
- `phone_a_friend/store.py`: the shared directory's files (README,
  *Shared-directory layout*) and each user's encrypted `config.enc`.
- `phone_a_friend/tui.py`: the curses TUI. `phone_a_friend/__main__.py`: the
  `paf` command and its subcommands.

`tools/build_pyz.py` builds `paf.pyz`, the single-file zipapp (PAF-007);
`tools/pyz_main.py` is its root `__main__.py` and must stay parseable by
Python 3.6, so it can tell an old host what is missing.

The one dependency is `cryptography`, and Python 3.9 is the floor
(`requires-python = ">=3.9"`, and CI tests 3.9). README.md is the manual and
also the PyPI long description, so its links are absolute. `docs/*.rst` is
the Sphinx site on Read the Docs, and `docs/index.html` is the GitHub Pages
site.

Clients of different versions meet in the same shared directory, so its
layout and file formats are part of the interface: a version that can no
longer read a directory, or a `config.enc`, that an earlier version wrote is
a major version.

## Every change moves the version

However small the change (code, tests, docs or CI), a pull request into
`main` raises the version, and `CHANGELOG.md` opens with that version's own
section, `## X.Y.Z — YYYY-MM-DD`, never an "Unreleased" heading left for
later. Patch for fixes, docs, tests and CI; minor for anything new a user can
use; major only when something that worked stops working the way it did.

The version is written in one place: `__version__` in
`phone_a_friend/__init__.py`. Everything else reads it from there:
`pyproject.toml` (`[tool.setuptools.dynamic]`), `docs/conf.py` and
`paf --version`.

`tests/check_version.sh` (shared_tools' template; only its `places()` and
`CHANGELOG` are this repository's) fails when `__version__` and the top of
`CHANGELOG.md` disagree, or when the changelog has an "Unreleased" heading.
CI's **version** job also fails a pull request whose version is not greater
than its base branch's, and a push to `main` whose version is not greater
than what `main` had before it. If `main` moves while a pull request is
open, rebase and bump again.

Publishing is a separate step, and a person's call: push a `v*` tag, publish
a GitHub release, or run `publish.yml` by hand, and PyPI trusted publishing
uploads the sdist and wheel. A number that is never published is fine; a
change with no number is not. A version already on PyPI is skipped
(`skip-existing`, from 2.1.1), so a release made after its version was
uploaded only attaches `paf.pyz`. A cloud session cannot publish: its git
proxy refuses tag pushes and its GitHub app cannot start workflows
(PAF-010), so ask the owner to run it.

## Checks

```bash
pip install -e . 'coverage[toml]'                 # coverage[toml] reads pyproject.toml on 3.9 and 3.10
bash tests/check_version.sh origin/main           # bumped, agreed, and past main's
coverage run -m unittest discover -s tests -v && coverage report   # 100% is enforced
reuse lint
python -m build && twine check dist/*
pip install -r docs/requirements.txt && sphinx-build -W -b html docs docs/_build/html
```

CI's **fips** job runs the suite again on Rocky Linux 9's own
`python3-cryptography` with OpenSSL in FIPS mode; tests that need to write
0.x fixtures skip there.

CI (`.github/workflows/ci.yml`) runs the suite with the 100% coverage gate on
Python 3.9, 3.11 and 3.13, then a smoke test of `paf` itself: two users
register, invite, accept and send, and the message must not appear in the
clear in any `.json` file. It also runs the version job, `reuse lint`, and
the build with `twine check`. Read the Docs builds with warnings as errors
(`.readthedocs.yaml`), so run the Sphinx build too when `docs/` or a
docstring changes. Coverage stays at 100%: a change to the code comes with
the tests that cover it, in `tests/test_*.py`, which `unittest discover`
finds by itself.

## Every task is a ticket

Everything done here goes through a ticket in `MartinGallagher-code/shared_tools`,
which keeps every project's tickets as files and moves each one Backlog → In Progress
→ Review → Done (Adversarial Test only when the owner asks). Before any change:

1. Attach `MartinGallagher-code/shared_tools` with `add_repo` and push access, and
   clone it. If a clone is already here, pull it instead
   (`git -C <shared_tools clone> pull --ff-only`): a stale clone has a stale
   `CLAUDE.md` and a stale `ticket.py`.
2. Read the "Every task is a ticket" section of its `CLAUDE.md`. It sets out what each
   column requires, and none of it is optional.
3. Open the ticket with the shared_tools clone's script, not a path in this
   repository:

   ~~~bash
   <shared_tools clone>/scripts/ticket.py new phone_a_friend "Imperative title" --request "the request, in the user's own words"
   ~~~

**Every change moves the version**, however small: a pull request into `main` raises
it in every place it is written and opens `CHANGELOG.md` with that version's own
dated section, never an "Unreleased" heading left for later. CI fails a pull request
whose version is not greater than its base branch's. This file's own section on the
version says where it is written.

The board: https://claude.ai/artifact/HrB3RpVpqGGQS7UZgZsMJm

## Bundling this repo

When packing this repository into a text bundle for handover, use the
canonical scripts in
[`shared_tools`](https://github.com/MartinGallagher-code/shared_tools)
(`scripts/handover.sh`, or `merge.sh`, `split.sh`, `b64bundle.sh`) and read
that repository's `CLAUDE.md` first. It has no `repos/phone_a_friend.conf`,
and `handover.sh` works without one. The tree is small and has gone over as
one plain `merge.sh` bundle before, but whether `crypto.py` passes the upload
scanner in the clear has not been tested since. If a plain part is refused,
send that file base64, as shared_tools' `CLAUDE.md` describes.

**Never include git internals in the bundle.** Export with
`git archive main | tar -x -C /tmp/tree`, never a working tree or a plain
`cp -r` of a live checkout; `.git/config` can carry credentials.
