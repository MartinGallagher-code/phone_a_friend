.. SPDX-License-Identifier: GPL-3.0-or-later
.. SPDX-FileCopyrightText: 2026 Martin J. Gallagher

Installation
============

Requires Python ≥ 3.9 (Linux). From PyPI:

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
