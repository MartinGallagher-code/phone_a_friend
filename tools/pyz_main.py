# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher

"""The entry point of paf.pyz, the single-file phone_a_friend.

build_pyz.py puts this file at the root of the zip as __main__.py. It runs
before anything else in the zip, on whatever python3 the host has, so it is
kept to syntax that even Python 3.6 (RHEL 8's python3) can parse: it has to
be able to say plainly what is missing.
"""

import sys

MIN_PYTHON = (3, 9)


def check():
    """Return an error line if this host cannot run paf, else None."""
    if sys.version_info[:2] < MIN_PYTHON:
        return (
            "paf: needs Python %d.%d or newer; this python3 is %d.%d"
            % (MIN_PYTHON + tuple(sys.version_info[:2]))
        )
    try:
        import cryptography.hazmat.primitives.ciphers.aead  # noqa: F401
    except ImportError:
        return (
            "paf: needs the system's Python cryptography package: ask your "
            "administrator for python3-cryptography (dnf or apt install "
            "python3-cryptography)"
        )
    return None


def main():
    problem = check()
    if problem is not None:
        sys.stderr.write(problem + "\n")
        sys.exit(1)
    from phone_a_friend.__main__ import main as paf_main

    paf_main()


if __name__ == "__main__":
    main()
