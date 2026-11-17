# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher

"""paf.pyz, the single-file build (tools/build_pyz.py)."""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "tools"))

import build_pyz  # noqa: E402
import pyz_main  # noqa: E402


class PyzTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="paf-pyz-test-")
        cls.pyz = os.path.join(cls.tmp, "out", "paf.pyz")
        with mock.patch("sys.stdout", io.StringIO()):
            build_pyz.main([cls.pyz])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def paf(self, *argv, env=None, isolated=True):
        cmd = [sys.executable] + (["-I"] if isolated else []) + [self.pyz, *argv]
        full_env = dict(os.environ, PAF_FIPS_WARNING="0")
        for var in ("PAF_DIR", "PAF_USER", "PAF_PASSPHRASE"):
            full_env.pop(var, None)
        full_env.update(env or {})
        return subprocess.run(
            cmd, cwd=self.tmp, env=full_env, capture_output=True, text=True
        )

    def test_contents(self):
        with zipfile.ZipFile(self.pyz) as zf:
            names = zf.namelist()
        self.assertIn("__main__.py", names)
        self.assertIn("phone_a_friend/crypto.py", names)
        self.assertFalse([n for n in names if n.endswith((".so", ".pyd", ".pyc"))])
        self.assertFalse([n for n in names if n.startswith("cryptography")])
        with open(self.pyz, "rb") as fh:
            self.assertEqual(fh.readline(), b"#!/usr/bin/env python3\n")

    def test_version_and_help(self):
        out = self.paf("--version")
        self.assertEqual(out.returncode, 0, out.stderr)
        import phone_a_friend

        self.assertIn(f"(paf) {phone_a_friend.__version__}", out.stdout)
        self.assertEqual(self.paf("--help").returncode, 0)

    def test_two_users_round_trip(self):
        d = os.path.join(self.tmp, "shared")
        os.makedirs(d, exist_ok=True)
        steps = [
            ("alice", "pw-a", ["register"]),
            ("bob", "pw-b", ["register"]),
            ("alice", "pw-a", ["invite", "bob"]),
            ("bob", "pw-b", ["accept", "--from", "alice"]),
            ("alice", "pw-a", ["send", "--to", "bob", "hello from a zip"]),
        ]
        for user, pw, argv in steps:
            out = self.paf("-d", d, "-u", user, "--passphrase", pw, *argv)
            self.assertEqual(out.returncode, 0, out.stderr)
        out = self.paf("-d", d, "-u", "bob", "--passphrase", "pw-b", "read",
                       "--to", "alice")
        self.assertIn("[alice] hello from a zip", out.stdout)

    def test_without_cryptography(self):
        fake = os.path.join(self.tmp, "fake")
        os.makedirs(os.path.join(fake, "cryptography"), exist_ok=True)
        with open(os.path.join(fake, "cryptography", "__init__.py"), "w") as fh:
            fh.write("raise ImportError('not installed')\n")
        out = self.paf("--version", env={"PYTHONPATH": fake}, isolated=False)
        self.assertEqual(out.returncode, 1)
        self.assertEqual(out.stdout, "")
        self.assertEqual(len(out.stderr.strip().splitlines()), 1)
        self.assertIn("python3-cryptography", out.stderr)

    def test_old_python(self):
        with mock.patch.object(pyz_main.sys, "version_info", (3, 6, 8, "final", 0)):
            self.assertEqual(
                pyz_main.check(), "paf: needs Python 3.9 or newer; this python3 is 3.6"
            )
            err = io.StringIO()
            with mock.patch.object(pyz_main.sys, "stderr", err), \
                    self.assertRaises(SystemExit) as cm:
                pyz_main.main()
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("Python 3.9", err.getvalue())

    def test_entry_point_parses_as_python_3_6(self):
        import ast

        with open(pyz_main.__file__) as fh:
            ast.parse(fh.read(), feature_version=(3, 6))

    def test_default_output(self):
        with mock.patch.object(build_pyz, "ROOT", self.tmp), \
                mock.patch.object(build_pyz, "build", return_value=self.pyz) as b, \
                mock.patch("sys.stdout", io.StringIO()):
            build_pyz.main([])
        b.assert_called_once_with(os.path.join(self.tmp, "dist", "paf.pyz"))


if __name__ == "__main__":
    unittest.main()
