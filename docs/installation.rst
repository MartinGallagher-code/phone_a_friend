.. SPDX-License-Identifier: GPL-3.0-or-later
.. SPDX-FileCopyrightText: 2026 Martin J. Gallagher

Installation
============

The single file
---------------

phone_a_friend ships as one file, ``paf.pyz``, attached to every
`GitHub release <https://github.com/MartinGallagher-code/phone_a_friend/releases/latest>`_.
Download it anywhere you can write, and run it. It needs no root, no pip and
no installation:

.. code-block:: bash

   python3 paf.pyz --help       # or: chmod +x paf.pyz && ./paf.pyz
   alias paf='python3 ~/paf.pyz'

What the host needs:

* **Python 3.9 or newer.** RHEL 9 and Rocky 9 (3.9), Fedora, Debian 11+
  (3.9+) and Ubuntu 22.04+ (3.10+) qualify. RHEL 8's default ``python3`` is
  3.6, which is too old, and ``paf.pyz`` says so.
* **The distribution's** ``python3-cryptography``, which is often installed
  already. Without it, ``paf.pyz`` prints one line naming the package and
  exits with status 1. Ask your administrator for it:

  .. code-block:: bash

     sudo dnf install python3-cryptography     # RHEL, Rocky, Fedora
     sudo apt install python3-cryptography     # Debian, Ubuntu

The file holds only phone_a_friend's own Python code. ``cryptography`` cannot
go in it: its compiled extension does not import from a zip, and the PyPI
wheel would bring its own OpenSSL, which is not FIPS-validated. Using the
system's package binds paf to the system OpenSSL, which on RHEL 9 or Rocky 9
in FIPS mode is the validated module (see *On a FIPS host* below).

To build it from a checkout: ``python3 tools/build_pyz.py`` writes
``dist/paf.pyz``.

From PyPI
---------

Requires Python ≥ 3.9 (Linux):

.. code-block:: bash

   pip install phoneafriend     # installs the `paf` command

From a checkout:

.. code-block:: bash

   pip install .             # or `pip install -e .` for development
   # or, without installing:
   pip install -r requirements.txt
   alias paf='python -m phone_a_friend'

On a FIPS host
--------------

phone_a_friend uses only FIPS-approved algorithms (:doc:`security`), but
whether they run inside a FIPS-validated module depends on the OpenSSL that
``cryptography`` is bound to. The wheels on PyPI bundle their own OpenSSL,
which is not validated. On RHEL 9 or Rocky 9 in FIPS mode, use the
distribution's package instead, which is built against the system OpenSSL
and its FIPS provider:

.. code-block:: bash

   sudo dnf install python3-cryptography      # or ask your administrator
   python3 -m venv --system-site-packages ~/paf-venv
   ~/paf-venv/bin/pip install --no-deps phoneafriend
   ~/paf-venv/bin/paf --version               # last line: "FIPS mode on"

``paf --version`` ends with the OpenSSL in use and whether it is in FIPS
mode. When it is not, every ``paf`` command prints one warning on stderr and
carries on (the TUI says so on its status line); ``PAF_FIPS_WARNING=0``
silences it.

Set up a shared directory
-------------------------

Any directory all participants can read and write works. Typical setup with
a shared POSIX group:

.. code-block:: bash

   sudo mkdir -p /srv/paf
   sudo chgrp chatters /srv/paf
   sudo chmod 2770 /srv/paf     # rwx for the group, setgid, nothing for others

The client creates its own subdirectories (drop-boxes get the sticky bit so
users cannot delete each other's files).
