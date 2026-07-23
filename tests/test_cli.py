# SPDX-FileCopyrightText: 2026 Martin Gallagher
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the command-line interface (phone_a_friend.__main__)."""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from phone_a_friend import __main__ as cli
from phone_a_friend.store import SharedDir, Session


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="paf-test-")
        self.env = mock.patch.dict(
            os.environ, {}, clear=False
        )
        self.env.start()
        for var in ("PAF_DIR", "PAF_USER", "PAF_PASSPHRASE"):
            os.environ.pop(var, None)

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *argv, expect_exit=None):
        out, err = io.StringIO(), io.StringIO()
        code = None
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main(list(argv))
            except SystemExit as exc:
                code = exc.code
        if expect_exit is not None:
            self.assertEqual(code, expect_exit)
        else:
            self.assertIsNone(code)
        return out.getvalue(), err.getvalue()

    def register(self, name, pw="pw"):
        return self.run_cli(
            "-d", self.tmp, "-u", name, "--passphrase", pw, "register"
        )

    # -------------------------------------------------------------- plumbing
    def test_no_shared_dir(self):
        _, err = self.run_cli("-u", "alice", "status", expect_exit=2)
        self.assertIn("no shared directory", err)

    def test_shared_dir_from_env(self):
        os.environ["PAF_DIR"] = self.tmp
        out, _ = self.run_cli(
            "-u", "alice", "--passphrase", "pw", "register"
        )
        self.assertIn("registered alice", out)

    def test_register_requires_user(self):
        _, err = self.run_cli(
            "-d", self.tmp, "--passphrase", "pw", "register", expect_exit=1
        )
        self.assertIn("specify --user", err)

    def test_passphrase_from_env(self):
        os.environ["PAF_PASSPHRASE"] = "pw"
        out, _ = self.run_cli("-d", self.tmp, "-u", "alice", "register")
        self.assertIn("registered alice", out)

    def test_passphrase_prompted_with_confirm(self):
        with mock.patch.object(cli.getpass, "getpass", side_effect=["pw", "pw"]):
            out, _ = self.run_cli("-d", self.tmp, "-u", "alice", "register")
        self.assertIn("registered alice", out)

    def test_passphrase_confirm_mismatch(self):
        with mock.patch.object(cli.getpass, "getpass", side_effect=["pw", "other"]):
            _, err = self.run_cli(
                "-d", self.tmp, "-u", "alice", "register", expect_exit=1
            )
        self.assertIn("do not match", err)

    def test_open_session_login_and_username_prompt(self):
        self.register("alice")
        with mock.patch("builtins.input", return_value="alice"):
            out, _ = self.run_cli("-d", self.tmp, "--passphrase", "pw", "status")
        self.assertIn("user: alice", out)

    def test_open_session_registers_on_yes(self):
        with mock.patch("builtins.input", return_value="y"), \
                mock.patch.object(cli.getpass, "getpass", side_effect=["pw", "pw"]):
            out, _ = self.run_cli("-d", self.tmp, "-u", "newbie", "status")
        self.assertIn("user: newbie", out)

    def test_open_session_declines_registration(self):
        with mock.patch("builtins.input", return_value="n"):
            self.run_cli(
                "-d", self.tmp, "-u", "nobody", "--passphrase", "pw",
                "status", expect_exit=1,
            )

    def test_keyboard_interrupt_exit_code(self):
        with mock.patch.object(
            cli, "cmd_status", side_effect=KeyboardInterrupt
        ):
            self.run_cli(
                "-d", self.tmp, "-u", "alice", "--passphrase", "pw",
                "status", expect_exit=130,
            )

    def test_tui_command_launches(self):
        self.register("alice")
        import phone_a_friend.tui as tui_mod

        with mock.patch.object(tui_mod, "run") as run:
            self.run_cli("-d", self.tmp, "-u", "alice", "--passphrase", "pw")
            self.run_cli("-d", self.tmp, "-u", "alice", "--passphrase", "pw", "tui")
        self.assertEqual(run.call_count, 2)

    # ------------------------------------------------------------- commands
    def test_full_dm_flow(self):
        self.register("alice", "pw-a")
        self.register("bob", "pw-b")
        out, _ = self.run_cli(
            "-d", self.tmp, "-u", "alice", "--passphrase", "pw-a", "invite", "bob"
        )
        self.assertIn("pushed to bob", out)
        out, _ = self.run_cli(
            "-d", self.tmp, "-u", "bob", "--passphrase", "pw-b", "invites"
        )
        self.assertIn("chat invite from alice", out)
        out, _ = self.run_cli(
            "-d", self.tmp, "-u", "bob", "--passphrase", "pw-b",
            "accept", "--from", "alice",
        )
        self.assertIn("accepted: chat with alice", out)
        self.run_cli(
            "-d", self.tmp, "-u", "alice", "--passphrase", "pw-a",
            "send", "--to", "bob", "hello",
        )
        out, _ = self.run_cli(
            "-d", self.tmp, "-u", "bob", "--passphrase", "pw-b",
            "read", "--to", "alice",
        )
        self.assertIn("[alice] hello", out)
        out, _ = self.run_cli(
            "-d", self.tmp, "-u", "bob", "--passphrase", "pw-b", "status"
        )
        self.assertIn("alice  (unread: 0)", out)

    def test_group_flow_and_status(self):
        self.register("alice", "pw-a")
        self.register("bob", "pw-b")
        out, _ = self.run_cli(
            "-d", self.tmp, "-u", "alice", "--passphrase", "pw-a",
            "create-group", "book club",
        )
        self.assertIn("created group 'book club'", out)
        out, _ = self.run_cli(
            "-d", self.tmp, "-u", "alice", "--passphrase", "pw-a",
            "invite", "bob", "--group", "book club",
        )
        self.assertIn("group key for 'book club'", out)
        out, _ = self.run_cli(
            "-d", self.tmp, "-u", "bob", "--passphrase", "pw-b", "invites"
        )
        self.assertIn("group invite from alice", out)
        # status shows the pending group invite from alice's side
        out, _ = self.run_cli(
            "-d", self.tmp, "-u", "alice", "--passphrase", "pw-a", "status"
        )
        self.assertIn("pending invites sent:", out)
        self.assertIn("group -> bob", out)
        out, _ = self.run_cli(
            "-d", self.tmp, "-u", "bob", "--passphrase", "pw-b", "accept"
        )
        self.assertIn("accepted: group 'book club'", out)
        self.run_cli(
            "-d", self.tmp, "-u", "alice", "--passphrase", "pw-a",
            "send", "--group", "book club", "welcome",
        )
        out, _ = self.run_cli(
            "-d", self.tmp, "-u", "bob", "--passphrase", "pw-b",
            "read", "--group", "book club",
        )
        self.assertIn("[alice] welcome", out)
        # acceptance notice arrives via status
        out, _ = self.run_cli(
            "-d", self.tmp, "-u", "alice", "--passphrase", "pw-a", "status"
        )
        self.assertIn("#book club", out)

    def test_invites_empty_and_accept_nothing(self):
        self.register("alice")
        out, _ = self.run_cli(
            "-d", self.tmp, "-u", "alice", "--passphrase", "pw", "invites"
        )
        self.assertIn("no pending invites", out)
        out, _ = self.run_cli(
            "-d", self.tmp, "-u", "alice", "--passphrase", "pw", "accept"
        )
        self.assertIn("nothing to accept", out)

    def test_send_and_read_argument_validation(self):
        self.register("alice")
        _, err = self.run_cli(
            "-d", self.tmp, "-u", "alice", "--passphrase", "pw",
            "send", "hi", expect_exit=1,
        )
        self.assertIn("specify --to USER or --group GROUP", err)
        _, err = self.run_cli(
            "-d", self.tmp, "-u", "alice", "--passphrase", "pw",
            "read", expect_exit=1,
        )
        self.assertIn("specify --to USER or --group GROUP", err)
        _, err = self.run_cli(
            "-d", self.tmp, "-u", "alice", "--passphrase", "pw",
            "send", "--group", "nope", "hi", expect_exit=1,
        )
        self.assertIn("unknown group", err)
        _, err = self.run_cli(
            "-d", self.tmp, "-u", "alice", "--passphrase", "pw",
            "read", "--group", "nope", expect_exit=1,
        )
        self.assertIn("unknown group", err)
        _, err = self.run_cli(
            "-d", self.tmp, "-u", "alice", "--passphrase", "pw",
            "invite", "bob", "--group", "nope", expect_exit=1,
        )
        self.assertIn("unknown group", err)

    def test_read_skips_undecryptable_files(self):
        self.register("alice", "pw-a")
        self.register("bob", "pw-b")
        self.run_cli(
            "-d", self.tmp, "-u", "alice", "--passphrase", "pw-a", "invite", "bob"
        )
        self.run_cli(
            "-d", self.tmp, "-u", "bob", "--passphrase", "pw-b", "accept"
        )
        self.run_cli(
            "-d", self.tmp, "-u", "alice", "--passphrase", "pw-a",
            "send", "--to", "bob", "real",
        )
        shared = SharedDir(self.tmp)
        with open(os.path.join(shared.dm_dir("alice", "bob"), "5-bad.json"), "w") as fh:
            json.dump({"blob": "AAAA"}, fh)
        out, _ = self.run_cli(
            "-d", self.tmp, "-u", "bob", "--passphrase", "pw-b",
            "read", "--to", "alice",
        )
        self.assertEqual(out.strip(), "[alice] real")


if __name__ == "__main__":
    unittest.main()
