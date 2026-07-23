# SPDX-FileCopyrightText: 2026 Martin Gallagher
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Edge-case and error-path tests for the store and crypto layers."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from phone_a_friend import crypto
from phone_a_friend import store as store_mod
from phone_a_friend.store import SharedDir, Session, StoreError, _atomic_write, _make_dir, _ts_of


class HelperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="paf-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ts_of_bad_filename(self):
        self.assertEqual(_ts_of("garbage.json"), 0.0)
        self.assertEqual(_ts_of("x-y.json"), 0.0)

    def test_atomic_write_cleans_up_on_failure(self):
        target = os.path.join(self.tmp, "out.bin")
        with mock.patch.object(store_mod.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                _atomic_write(target, b"data", 0o644)
        self.assertEqual(os.listdir(self.tmp), [])  # tmp file removed

    def test_atomic_write_cleanup_failure_is_swallowed(self):
        target = os.path.join(self.tmp, "out.bin")
        with mock.patch.object(store_mod.os, "replace", side_effect=OSError("boom")), \
                mock.patch.object(store_mod.os, "unlink", side_effect=OSError("nope")):
            with self.assertRaises(OSError):
                _atomic_write(target, b"data", 0o644)

    def test_make_dir_survives_chmod_failure(self):
        path = os.path.join(self.tmp, "sub")
        with mock.patch.object(store_mod.os, "chmod", side_effect=OSError("ro")):
            _make_dir(path, 0o755)
        self.assertTrue(os.path.isdir(path))


class SharedDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="paf-test-")
        self.shared = SharedDir(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ensure_root_missing(self):
        with self.assertRaises(StoreError):
            SharedDir(os.path.join(self.tmp, "nope")).ensure_root()

    def test_list_users_empty(self):
        self.assertEqual(self.shared.list_users(), [])

    def test_read_identity_missing_and_corrupt(self):
        with self.assertRaises(StoreError):
            self.shared.read_identity("ghost")
        os.makedirs(self.shared.user_dir("mallory"))
        with open(self.shared.identity_path("mallory"), "w") as fh:
            fh.write("not json")
        with self.assertRaises(StoreError):
            self.shared.read_identity("mallory")
        with open(self.shared.identity_path("mallory"), "w") as fh:
            json.dump({"name": "other", "pub": "x"}, fh)
        with self.assertRaises(StoreError):
            self.shared.read_identity("mallory")

    def test_read_group_meta(self):
        with self.assertRaises(StoreError):
            self.shared.read_group_meta("nope")
        s = Session.register(self.shared, "alice", "pw")
        gid = s.create_group("g")
        meta = self.shared.read_group_meta(gid)
        self.assertEqual(meta["owner"], "alice")
        self.assertEqual(meta["name"], "g")


class SessionEdgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="paf-test-")
        self.shared = SharedDir(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_register_empty_passphrase(self):
        with self.assertRaises(StoreError):
            Session.register(self.shared, "alice", "")

    def test_login_corrupt_config(self):
        Session.register(self.shared, "alice", "pw")
        with open(self.shared.config_path("alice"), "w") as fh:
            fh.write("not json")
        with self.assertRaises(StoreError):
            Session.login(self.shared, "alice", "pw")

    def test_login_unknown_user(self):
        with self.assertRaises(StoreError):
            Session.login(self.shared, "ghost", "pw")

    def test_invite_contact_errors(self):
        a = Session.register(self.shared, "alice", "pw")
        with self.assertRaises(StoreError):
            a.invite_contact("alice")  # self
        with self.assertRaises(StoreError):
            a.invite_contact("ghost")  # unregistered
        Session.register(self.shared, "bob", "pw")
        a.invite_contact("bob")
        with self.assertRaises(StoreError):
            a.invite_contact("bob")  # already pending
        a.config["contacts"]["bob"] = "key"
        a.config["pending"] = []
        with self.assertRaises(StoreError):
            a.invite_contact("bob")  # already a contact

    def test_invite_group_errors(self):
        a = Session.register(self.shared, "alice", "pw")
        with self.assertRaises(StoreError):
            a.invite_group("nogroup", "bob")  # not a member
        gid = a.create_group("g")
        with self.assertRaises(StoreError):
            a.invite_group(gid, "alice")  # self

    def test_create_group_empty_name(self):
        a = Session.register(self.shared, "alice", "pw")
        with self.assertRaises(StoreError):
            a.create_group("   ")

    def test_group_by_name(self):
        a = Session.register(self.shared, "alice", "pw")
        gid = a.create_group("g")
        self.assertEqual(a.group_by_name("g"), gid)
        self.assertEqual(a.group_by_name(gid), gid)
        self.assertIsNone(a.group_by_name("other"))

    def test_conv_corrupt_keys(self):
        a = Session.register(self.shared, "alice", "pw")
        a.config["contacts"]["bob"] = "not!base64!"
        with self.assertRaises(StoreError):
            a.send_message("dm", "bob", "hi")
        a.config["groups"]["g1"] = {"name": "g", "key": "also!not!b64"}
        with self.assertRaises(StoreError):
            a.send_message("grp", "g1", "hi")
        a.config["groups"]["g1"]["key"] = crypto.b64e(b"too-short")
        with self.assertRaises(StoreError):
            a.send_message("grp", "g1", "hi")

    def test_conv_bad_kind(self):
        a = Session.register(self.shared, "alice", "pw")
        with self.assertRaises(StoreError):
            a.send_message("smoke-signal", "bob", "hi")

    def test_list_invites_skips_junk(self):
        a = Session.register(self.shared, "alice", "pw")
        b = Session.register(self.shared, "bob", "pw")
        inv_dir = self.shared.invites_dir("bob")
        with open(os.path.join(inv_dir, "notes.txt"), "w") as fh:
            fh.write("not an invite")
        with open(os.path.join(inv_dir, "999-garbage.json"), "w") as fh:
            fh.write("{ not json")
        with open(os.path.join(inv_dir, "998-notsealed.json"), "w") as fh:
            json.dump({"other": "shape"}, fh)
        # an invite sealed to the WRONG key is silently undecryptable
        wrong = crypto.public_key_bytes(crypto.generate_private_key())
        blob = crypto.seal(wrong, json.dumps({"type": "contact"}).encode())
        with open(os.path.join(inv_dir, "997-wrongkey.json"), "w") as fh:
            json.dump({"sealed": crypto.b64e(blob)}, fh)
        self.assertEqual(b.list_invites(), [])
        a.invite_contact("bob")
        self.assertEqual(len(b.list_invites()), 1)

    def test_list_invites_missing_dir(self):
        a = Session.register(self.shared, "alice", "pw")
        shutil.rmtree(self.shared.invites_dir("alice"))
        self.assertEqual(a.list_invites(), [])

    def test_stale_invites_discarded(self):
        a = Session.register(self.shared, "alice", "pw")
        b = Session.register(self.shared, "bob", "pw")
        a.invite_contact("bob")
        fname, payload = b.list_invites()[0]
        b.accept_invite(fname, payload)
        # a second contact invite arrives after bob already has alice
        a.config["pending"] = []
        a.invite_contact("bob") if "bob" not in a.config["contacts"] else None
        a.process_replies()
        a.config["pending"] = []
        # rebuild a stale invite manually: alice invites again
        ident = self.shared.read_identity("bob")
        a._drop_sealed(
            self.shared.invites_dir("bob"),
            ident["pub"],
            {"type": "contact", "from": "alice", "from_pub": crypto.b64e(a.pub)},
        )
        self.assertEqual(b.list_invites(), [])  # discarded as stale
        # same for a stale group invite
        gid = a.create_group("g")
        a.invite_group(gid, "bob")
        fname, payload = b.list_invites()[0]
        b.accept_invite(fname, payload)
        a._drop_sealed(
            self.shared.invites_dir("bob"),
            ident["pub"],
            {
                "type": "group", "from": "alice",
                "from_pub": crypto.b64e(a.pub),
                "group_id": gid, "group_name": "g", "group_key": a.config["groups"][gid]["key"],
            },
        )
        self.assertEqual(b.list_invites(), [])

    def test_accept_invite_unknown_type(self):
        a = Session.register(self.shared, "alice", "pw")
        with self.assertRaises(StoreError):
            a.accept_invite("x.json", {"type": "carrier-pigeon", "from": "bob"})

    def test_discard_missing_file(self):
        Session._discard(os.path.join(self.tmp, "does-not-exist"))

    def test_process_replies_edge_cases(self):
        a = Session.register(self.shared, "alice", "pw")
        # missing dir
        shutil.rmtree(self.shared.replies_dir("alice"))
        self.assertEqual(a.process_replies(), [])
        os.makedirs(self.shared.replies_dir("alice"))
        # junk files are skipped
        with open(os.path.join(self.shared.replies_dir("alice"), "x.txt"), "w") as fh:
            fh.write("junk")
        with open(os.path.join(self.shared.replies_dir("alice"), "1-bad.json"), "w") as fh:
            fh.write("{")
        self.assertEqual(a.process_replies(), [])
        # unknown reply type is consumed without a notice
        a._drop_sealed(
            self.shared.replies_dir("alice"),
            crypto.b64e(a.pub),
            {"type": "smoke-signal", "from": "bob"},
        )
        self.assertEqual(a.process_replies(), [])
        self.assertEqual(
            [f for f in os.listdir(self.shared.replies_dir("alice")) if f.endswith(".json") and "bad" not in f],
            [],
        )

    def test_group_decline_reply(self):
        a = Session.register(self.shared, "alice", "pw")
        b = Session.register(self.shared, "bob", "pw")
        gid = a.create_group("g")
        a.invite_group(gid, "bob")
        fname, payload = b.list_invites()[0]
        b.decline_invite(fname, payload)
        notices = a.process_replies()
        self.assertTrue(any("declined a group invite" in n for n in notices))
        self.assertEqual(a.config["pending"], [])

    def test_clear_pending_gid_mismatch(self):
        a = Session.register(self.shared, "alice", "pw")
        a.config["pending"] = [{"type": "group", "to": "bob", "group_id": "g1"}]
        a._clear_pending("group", "bob", "other")
        self.assertEqual(len(a.config["pending"]), 1)

    def test_load_messages_skips_bad_files(self):
        a = Session.register(self.shared, "alice", "pw")
        b = Session.register(self.shared, "bob", "pw")
        a.invite_contact("bob")
        fname, payload = b.list_invites()[0]
        b.accept_invite(fname, payload)
        a.process_replies()
        a.send_message("dm", "bob", "real")
        dm_dir = self.shared.dm_dir("alice", "bob")
        with open(os.path.join(dm_dir, "111-bad.json"), "w") as fh:
            fh.write("{ nope")
        with open(os.path.join(dm_dir, "112-shape.json"), "w") as fh:
            json.dump({"other": 1}, fh)
        # a validly-encrypted payload that is not a message dict
        _, key, aad = a._conv("dm", "bob")
        blob = crypto.sym_encrypt(key, json.dumps(["not", "a", "dict"]).encode(), aad)
        with open(os.path.join(dm_dir, "113-shape2.json"), "w") as fh:
            json.dump({"blob": crypto.b64e(blob)}, fh)
        files = b.list_message_files("dm", "alice")
        msgs = b.load_messages("dm", "alice", files + ["999-missing.json"])
        self.assertEqual([m["text"] for m in msgs.values()], ["real"])

    def test_unread_and_mark_read_without_key(self):
        a = Session.register(self.shared, "alice", "pw")
        self.assertEqual(a.unread_count("dm", "stranger"), 0)
        self.assertFalse(a.mark_read("dm", "stranger"))

    def test_mark_read_no_change(self):
        a = Session.register(self.shared, "alice", "pw")
        b = Session.register(self.shared, "bob", "pw")
        a.invite_contact("bob")
        fname, payload = b.list_invites()[0]
        b.accept_invite(fname, payload)
        a.process_replies()
        self.assertFalse(a.mark_read("dm", "bob"))  # no messages yet
        a.send_message("dm", "bob", "x")
        self.assertTrue(a.mark_read("dm", "bob"))
        self.assertFalse(a.mark_read("dm", "bob"))  # already up to date


class CryptoEdgeTest(unittest.TestCase):
    def test_sym_decrypt_short_blob(self):
        with self.assertRaises(crypto.DecryptError):
            crypto.sym_decrypt(crypto.new_symmetric_key(), b"short")

    def test_unseal_short_blob(self):
        priv = crypto.generate_private_key()
        with self.assertRaises(crypto.DecryptError):
            crypto.unseal(priv, b"short")

    def test_unseal_bad_ephemeral_key(self):
        priv = crypto.generate_private_key()
        # an all-zero ephemeral public key is rejected by X25519
        blob = b"\x00" * 32 + b"\x00" * 40
        with self.assertRaises(crypto.DecryptError):
            crypto.unseal(priv, blob)


if __name__ == "__main__":
    unittest.main()
