# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher

"""Build paf.pyz: phone_a_friend as one file, run with ``python3 paf.pyz``.

The zip holds the pure-Python package and tools/pyz_main.py as its
__main__.py, and nothing else. It does not hold cryptography: a compiled
extension cannot be imported from a zip, and the PyPI wheel bundles its own
OpenSSL, which is not FIPS-validated. The single file uses the host's
python3-cryptography instead, which is bound to the system OpenSSL.

    python3 tools/build_pyz.py [OUTPUT]      # default: dist/paf.pyz
"""

import os
import shutil
import sys
import tempfile
import zipapp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = "phone_a_friend"


def build(output: str) -> str:
    with tempfile.TemporaryDirectory(prefix="paf-pyz-") as staging:
        dest = os.path.join(staging, PACKAGE)
        os.mkdir(dest)
        for name in sorted(os.listdir(os.path.join(ROOT, PACKAGE))):
            if name.endswith(".py"):
                shutil.copy2(os.path.join(ROOT, PACKAGE, name), dest)
        shutil.copy2(
            os.path.join(ROOT, "tools", "pyz_main.py"),
            os.path.join(staging, "__main__.py"),
        )
        os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
        zipapp.create_archive(
            staging,
            output,
            interpreter="/usr/bin/env python3",
            compressed=True,
        )
    return output


def main(argv=None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    out = build(argv[0] if argv else os.path.join(ROOT, "dist", "paf.pyz"))
    print(f"built {out} ({os.path.getsize(out):,} bytes)")


if __name__ == "__main__":
    main()
