# SPDX-FileCopyrightText: 2026 Martin Gallagher
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""End-to-end tests for the shared-directory protocol (no TUI needed).

Run with:  python -m unittest discover -s tests -v
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from phone_a_friend import crypto
from phone_a_friend.store import SharedDir, Session, StoreError


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="paf-test-")
        self.shared = SharedDir(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ accounts
    def test_register_login(self):
        Session.register(self.shared, "alice", "pw-a")
        s = Session.login(self.shared, "alice", "pw-a")
        self.assertEqual(s.name, "alice")
        with self.assertRaises(StoreError):
            Session.login(self.shared, "alice", "wrong")
        with self.assertRaises(StoreError):
            Session.register(self.shared, "alice", "again")
        with self.assertRaises(StoreError):
            Session.register(self.shared, "../evil", "pw")
        self.assertEqual(self.shared.list_users(), ["alice"])

    # ---------------------------------------------------------- direct chat
    def test_contact_invite_and_dm(self):
        a = Session.register(self.shared, "alice", "pw-a")
        b = Session.register(self.shared, "bob", "pw-b")

        # no keys exchanged yet -> cannot message
        with self.assertRaises(StoreError):
            a.send_message("dm", "bob", "hi")

        a.invite_contact("bob")
        self.assertEqual(a.config["pending"], [{"type": "contact", "to": "bob"}])

        invites = b.list_invites()
        self.assertEqual(len(invites), 1)
        fname, payload = invites[0]
        self.assertEqual(payload["type"], "contact")
        self.assertEqual(payload["from"], "alice")
        b.accept_invite(fname, payload)
        self.assertIn("alice", b.config["contacts"])
        self.assertEqual(b.list_invites(), [])  # consumed

        # alice picks up the acceptance (bob's key is pushed back)
        notices = a.process_replies()
        self.assertTrue(any("accepted" in n for n in notices))
        self.assertIn("bob", a.config["contacts"])
        self.assertEqual(a.config["pending"], [])

        a.send_message("dm", "bob", "hello bob")
        b.send_message("dm", "alice", "hey alice")

        files = b.list_message_files("dm", "alice")
        msgs = list(b.load_messages("dm", "alice", files).values())
        self.assertEqual(
            [(m["from"], m["text"]) for m in sorted(msgs, key=lambda m: m["ts"])],
            [("alice", "hello bob"), ("bob", "hey alice")],
        )

        # unread bookkeeping
        self.assertEqual(b.unread_count("dm", "alice"), 2)
        b.mark_read("dm", "alice")
        self.assertEqual(b.unread_count("dm", "alice"), 0)
        a.send_message("dm", "bob", "one more")
        self.assertEqual(b.unread_count("dm", "alice"), 1)

    def test_eavesdropper_cannot_read(self):
        a = Session.register(self.shared, "alice", "pw-a")
        b = Session.register(self.shared, "bob", "pw-b")
        eve = Session.register(self.shared, "eve", "pw-e")

        a.invite_contact("bob")
        fname, payload = b.list_invites()[0]
        b.accept_invite(fname, payload)
        a.process_replies()
        a.send_message("dm", "bob", "secret stuff")

        # eve has no exchanged key with either party
        with self.assertRaises(StoreError):
            eve.send_message("dm", "alice", "hi")

        # even by forging a contact entry with her own key, decryption fails
        eve.config["contacts"]["alice"] = crypto.b64e(
            crypto.public_key_bytes(crypto.generate_private_key())
        )
        dm_dir = self.shared.dm_dir("alice", "bob")
        files = sorted(f for f in os.listdir(dm_dir) if f.endswith(".json"))
        self.assertEqual(len(files), 1)
        # decrypt attempt against the real ciphertext files, wrong key: skipped
        eve.config["contacts"]["bob"] = eve.config["contacts"]["alice"]
        decrypted = eve.load_messages("dm", "bob", files)
        self.assertEqual(decrypted, {})

        # raw file on disk does not contain the plaintext
        with open(os.path.join(dm_dir, files[0]), "rb") as fh:
            raw = fh.read()
        self.assertNotIn(b"secret stuff", raw)

    def test_decline_invite(self):
        a = Session.register(self.shared, "alice", "pw-a")
        b = Session.register(self.shared, "bob", "pw-b")
        a.invite_contact("bob")
        fname, payload = b.list_invites()[0]
        b.decline_invite(fname, payload)
        notices = a.process_replies()
        self.assertTrue(any("declined" in n for n in notices))
        self.assertEqual(a.config["pending"], [])
        self.assertNotIn("alice", b.config["contacts"])

    # --------------------------------------------------------------- groups
    def test_group_flow(self):
        a = Session.register(self.shared, "alice", "pw-a")
        b = Session.register(self.shared, "bob", "pw-b")
        eve = Session.register(self.shared, "eve", "pw-e")

        gid = a.create_group("book club")
        a.send_message("grp", gid, "welcome!")
        a.invite_group(gid, "bob")

        fname, payload = b.list_invites()[0]
        self.assertEqual(payload["type"], "group")
        self.assertEqual(payload["group_name"], "book club")
        b.accept_invite(fname, payload)
        self.assertTrue(any("joined" in n for n in a.process_replies()))

        # bob can read history and post
        files = b.list_message_files("grp", gid)
        msgs = b.load_messages("grp", gid, files)
        self.assertEqual([m["text"] for m in msgs.values()], ["welcome!"])
        b.send_message("grp", gid, "thanks!")
        files = a.list_message_files("grp", gid)
        texts = [
            m["text"]
            for m in sorted(
                a.load_messages("grp", gid, files).values(), key=lambda m: m["ts"]
            )
        ]
        self.assertEqual(texts, ["welcome!", "thanks!"])

        # eve without the group key cannot read anything
        with self.assertRaises(StoreError):
            eve.list_message_files("grp", gid)
        eve.config["groups"][gid] = {
            "name": "book club",
            "key": crypto.b64e(crypto.new_symmetric_key()),
        }
        self.assertEqual(
            eve.load_messages("grp", gid, eve.list_message_files("grp", gid)), {}
        )

        # bob can invite others once he holds the key (push basis)
        del eve.config["groups"][gid]  # drop the forged key from above
        b.invite_group(gid, "eve")
        fname, payload = eve.list_invites()[-1]
        self.assertEqual(payload["group_id"], gid)

    # --------------------------------------------------------------- config
    def test_config_encrypted_and_persistent(self):
        a = Session.register(self.shared, "alice", "pw-a")
        b = Session.register(self.shared, "bob", "pw-b")
        a.invite_contact("bob")
        fname, payload = b.list_invites()[0]
        b.accept_invite(fname, payload)
        a.process_replies()
        gid = a.create_group("g1")

        # a fresh login sees the same state
        a2 = Session.login(self.shared, "alice", "pw-a")
        self.assertIn("bob", a2.config["contacts"])
        self.assertIn(gid, a2.config["groups"])

        # the config file on disk leaks neither keys nor contacts
        with open(self.shared.config_path("alice"), "rb") as fh:
            raw = fh.read()
        self.assertNotIn(b"bob", raw)
        self.assertNotIn(a2.config["groups"][gid]["key"].encode(), raw)

    # --------------------------------------------------------------- crypto
    def test_crypto_primitives(self):
        priv = crypto.generate_private_key()
        pub = crypto.public_key_bytes(priv)
        blob = crypto.seal(pub, b"hello")
        self.assertEqual(crypto.unseal(priv, blob), b"hello")
        other = crypto.generate_private_key()
        with self.assertRaises(crypto.DecryptError):
            crypto.unseal(other, blob)

        p1, p2 = crypto.generate_private_key(), crypto.generate_private_key()
        k12 = crypto.pair_key(p1, crypto.public_key_bytes(p2))
        k21 = crypto.pair_key(p2, crypto.public_key_bytes(p1))
        self.assertEqual(k12, k21)

        key = crypto.new_symmetric_key()
        ct = crypto.sym_encrypt(key, b"msg", b"aad")
        self.assertEqual(crypto.sym_decrypt(key, ct, b"aad"), b"msg")
        with self.assertRaises(crypto.DecryptError):
            crypto.sym_decrypt(key, ct, b"other-aad")


if __name__ == "__main__":
    unittest.main()
