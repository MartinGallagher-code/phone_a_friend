# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher

"""DM directories named so that a listing does not show who talks to whom
(PAF-008)."""

import hashlib
import os
import re
import shutil
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from phone_a_friend import store  # noqa: E402
from phone_a_friend.store import Session, StoreError  # noqa: E402
from test_tui import TuiCase  # noqa: E402


class DmNameTest(TuiCase):
    def texts(self, s, other):
        files = s.list_message_files("dm", other)
        return [m["text"] for m in s.load_messages("dm", other, files).values()]

    def test_one_opaque_directory_both_can_use(self):
        a, b = self.make_pair()
        a.send_message("dm", "bob", "hi bob")
        b.send_message("dm", "alice", "hi alice")
        self.assertEqual(a.dm_directory("bob"), b.dm_directory("alice"))
        self.assertEqual(os.listdir(os.path.join(self.tmp, "dm")),
                         [os.path.basename(a.dm_directory("bob"))])
        name = os.path.basename(a.dm_directory("bob"))
        self.assertRegex(name, r"^[0-9a-f]{32}$")
        self.assertEqual(self.texts(a, "bob"), ["hi bob", "hi alice"])
        self.assertEqual(self.texts(b, "alice"), ["hi bob", "hi alice"])
        # and from a fresh login
        a2 = Session.login(self.shared, "alice", "pw")
        self.assertEqual(self.texts(a2, "bob"), ["hi bob", "hi alice"])

    def test_nobody_else_can_map_the_name(self):
        a, b = self.make_pair()
        eve = Session.register(self.shared, "eve", "pw")
        eve.invite_contact("alice")
        a.accept_invite(*a.list_invites()[0])
        eve.process_replies()
        a.send_message("dm", "bob", "x")
        eve.send_message("dm", "alice", "y")
        name = os.path.basename(a.dm_directory("bob"))
        # nothing anyone can compute from public data gives it
        idents = {u: self.shared.read_identity(u)["pub"] for u in ("alice", "bob")}
        for guess in ("alice__bob", "bob__alice", idents["alice"] + idents["bob"],
                      idents["bob"] + idents["alice"]):
            # FIPS-approved hashes only: OpenSSL in FIPS mode refuses MD5
            for h in (hashlib.sha256, hashlib.sha512, hashlib.sha3_256):
                self.assertNotIn(name, h(guess.encode()).hexdigest())
        # eve's own DM with alice is somewhere else; bob is not hers to name
        self.assertNotEqual(eve.dm_directory("alice"), a.dm_directory("bob"))
        with self.assertRaisesRegex(StoreError, "no key exchanged"):
            eve.dm_directory("bob")
        for d in os.listdir(os.path.join(self.tmp, "dm")):
            self.assertNotRegex(d, "alice|bob|eve")

    def make_old_store(self):
        """A pair whose messages sit in dm/alice__bob, as 1.4 left them."""
        a, b = self.make_pair()
        a.send_message("dm", "bob", "old from alice")
        b.send_message("dm", "alice", "old from bob")
        hidden = a.dm_directory("bob")
        old = self.shared.dm_dir("alice", "bob")
        os.rename(hidden, old)
        return old, hidden

    def test_an_old_directory_is_migrated(self):
        old, hidden = self.make_old_store()
        a = Session.login(self.shared, "alice", "pw")
        self.assertEqual(self.texts(a, "bob"), ["old from alice", "old from bob"])
        # every file here is ours (one uid in the tests), so all moved
        self.assertFalse(os.path.exists(old))
        self.assertEqual(len(os.listdir(hidden)), 2)
        a.send_message("dm", "bob", "new")
        b = Session.login(self.shared, "bob", "pw")
        self.assertEqual(self.texts(b, "alice"), ["old from alice", "old from bob", "new"])

    def test_files_not_ours_stay_until_their_sender_moves_them(self):
        old, hidden = self.make_old_store()
        a = Session.login(self.shared, "alice", "pw")
        with mock.patch.object(store.os, "getuid", return_value=os.getuid() + 1):
            self.assertEqual(self.texts(a, "bob"), ["old from alice", "old from bob"])
            a.send_message("dm", "bob", "new")
        self.assertEqual(len(os.listdir(old)), 2)  # not alice's to move
        self.assertEqual(len(os.listdir(hidden)), 1)
        self.assertEqual(a.migrate(), [])
        # a file that cannot be moved is left, and read where it is
        b = Session.login(self.shared, "bob", "pw")
        with mock.patch.object(store.os, "rename", side_effect=PermissionError):
            self.assertEqual(
                self.texts(b, "alice"), ["old from alice", "old from bob", "new"]
            )
        self.assertTrue(os.path.exists(old))
        b2 = Session.login(self.shared, "bob", "pw")
        self.assertEqual(len(self.texts(b2, "alice")), 3)
        self.assertFalse(os.path.exists(old))

    def test_a_contact_still_on_0x_keeps_the_old_name(self):
        a, b = self.make_pair()
        a.config["contacts"]["old"] = store.crypto.b64e(bytes(32))
        self.assertEqual(a.dm_directory("old"), self.shared.dm_dir("alice", "old"))
        shutil.rmtree(self.shared.dm_dir("alice", "bob"), ignore_errors=True)
        self.assertEqual(a.list_message_files("dm", "bob"), [])


if __name__ == "__main__":
    unittest.main()
