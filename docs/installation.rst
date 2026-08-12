.. SPDX-FileCopyrightText: 2026 Martin Gallagher
..
.. SPDX-License-Identifier: GPL-3.0-or-later

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
