# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher

"""FIPS-approved primitives, the record format, and migration from 0.x."""

import ast
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from cryptography.exceptions import UnsupportedAlgorithm  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec, x25519  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402

import phone_a_friend  # noqa: E402
from phone_a_friend import __main__ as cli  # noqa: E402
from phone_a_friend import crypto, legacy, store  # noqa: E402
from phone_a_friend.store import SharedDir, Session, StoreError  # noqa: E402
from v0_store import V0User  # noqa: E402

PKG = os.path.dirname(os.path.abspath(phone_a_friend.__file__))
H = bytes.fromhex


class KnownAnswerTest(unittest.TestCase):
    """Published test vectors, run through this module's own wrappers."""

    def test_aes_256_gcm(self):
        # The GCM specification's test cases 13, 14 and 16 (AES-256), the
        # vectors NIST's CAVP GCM tests were built from.
        zero = bytes(32)
        self.assertEqual(
            crypto.AESGCM(zero).encrypt(bytes(12), b"", b""),
            H("530f8afbc74536b9a963b4f1c4cb738b"),
        )
        self.assertEqual(
            crypto.AESGCM(zero).encrypt(bytes(12), bytes(16), b""),
            H("cea7403d4d606b6e074ec5d3baf39d18d0d1c8a799996bf0265b98b5d48ab919"),
        )
        key = H("feffe9928665731c6d6a8f9467308308feffe9928665731c6d6a8f9467308308")
        iv = H("cafebabefacedbaddecaf888")
        pt = H(
            "d9313225f88406e5a55909c5aff5269a86a7a9531534f7da2e4c303d8a318a72"
            "1c3c0c95956809532fcf0e2449a6b525b16aedf5aa0de657ba637b39"
        )
        aad = H("feedfacedeadbeeffeedfacedeadbeefabaddad2")
        ct = H(
            "522dc1f099567d07f47f37a32a84427d643a8cdcbfe5c0c97598a2bd2555d1aa"
            "8cb08e48590dbb3da7b08b1056828838c5f61e6393ba7a0abcc9f662"
            "76fc6ece0f4e1768cddf8853bb2d551b"
        )
        with mock.patch.object(crypto, "random_bytes", return_value=iv):
            self.assertEqual(crypto.sym_encrypt(key, pt, aad), iv + ct)
        self.assertEqual(crypto.sym_decrypt(key, iv + ct, aad), pt)

    def test_pbkdf2_hmac_sha256(self):
        # RFC 7914 section 11; a 32-byte key is the first block of each
        self.assertEqual(
            crypto.derive_passphrase_key("passwd", b"salt", 1),
            H("55ac046e56e3089fec1691c22544b605f94185216dde0465e68b9d57c20dacbc"),
        )
        self.assertEqual(
            crypto.derive_passphrase_key("Password", b"NaCl", 80000),
            H("4ddcd8f60b98be21830cee5ef22701f9641a4418d04c0414aeff08876b34ab56"),
        )

    def test_hkdf_sha256(self):
        # RFC 5869 test case 3 (no salt, no info), first 32 bytes of the OKM
        self.assertEqual(
            crypto.hkdf(b"\x0b" * 22, b""),
            H("8da4e775a563c18f715f802a063c5a31b8a11f5c5ee1879ec3454e5f3c738d2d"),
        )


class NoLegacyCryptoTest(unittest.TestCase):
    BANNED = {"X25519PrivateKey", "X25519PublicKey", "ChaCha20Poly1305", "Scrypt"}
    BANNED_MODULES = {"x25519", "scrypt"}

    def test_only_legacy_imports_the_0x_primitives(self):
        for fname in sorted(os.listdir(PKG)):
            if not fname.endswith(".py") or fname == "legacy.py":
                continue
            with open(os.path.join(PKG, fname)) as fh:
                tree = ast.parse(fh.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    names = {a.name for a in node.names}
                    mod = (node.module or "").rsplit(".", 1)[-1]
                    with self.subTest(file=fname, module=node.module):
                        self.assertFalse(names & self.BANNED)
                        self.assertNotIn(mod, self.BANNED_MODULES)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        with self.subTest(file=fname, module=alias.name):
                            self.assertFalse(
                                alias.name.rsplit(".", 1)[-1] in self.BANNED_MODULES
                            )

    def test_a_new_store_never_loads_legacy(self):
        tmp = tempfile.mkdtemp(prefix="paf-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        code = (
            "import sys\n"
            "from phone_a_friend.store import SharedDir, Session\n"
            f"d = SharedDir({tmp!r})\n"
            "a = Session.register(d, 'alice', 'pw-a')\n"
            "b = Session.register(d, 'bob', 'pw-b')\n"
            "a.invite_contact('bob')\n"
            "f, p = b.list_invites()[0]\n"
            "b.accept_invite(f, p)\n"
            "a.process_replies()\n"
            "a.send_message('dm', 'bob', 'hi')\n"
            "gid = a.create_group('g')\n"
            "a.send_message('grp', gid, 'all')\n"
            "a = Session.login(d, 'alice', 'pw-a')\n"
            "fs = b.list_message_files('dm', 'alice')\n"
            "assert list(b.load_messages('dm', 'alice', fs).values())[0]['text'] == 'hi'\n"
            "assert a.migrate() == []\n"
            "assert 'phone_a_friend.legacy' not in sys.modules\n"
            "import cryptography.hazmat.primitives.asymmetric.x25519 as x\n"
            "print('ok')\n"
        )
        out = subprocess.run(
            [sys.executable, "-c", code],
            cwd=os.path.join(HERE, ".."),
            capture_output=True,
            text=True,
        )
        self.assertEqual(out.stdout.strip(), "ok", out.stderr)


class CryptoTest(unittest.TestCase):
    def test_keys_are_p256(self):
        priv = crypto.generate_private_key()
        self.assertIsInstance(priv.curve, ec.SECP256R1)
        raw = crypto.private_key_bytes(priv)
        self.assertEqual(
            crypto.public_key_bytes(crypto.private_key_from_bytes(raw)),
            crypto.public_key_bytes(priv),
        )
        self.assertEqual(len(crypto.public_key_bytes(priv)), crypto.PUB_LEN)

    def test_private_key_must_be_p256(self):
        p384 = ec.generate_private_key(ec.SECP384R1()).private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        with self.assertRaises(ValueError):
            crypto.private_key_from_bytes(p384)

    def test_is_public_key(self):
        self.assertTrue(
            crypto.is_public_key(crypto.public_key_bytes(crypto.generate_private_key()))
        )
        self.assertFalse(crypto.is_public_key(b"\x04" + bytes(64)))  # not on curve
        self.assertFalse(crypto.is_public_key(bytes(32)))

    def test_unseal_rejects_off_curve_ephemeral_key(self):
        priv = crypto.generate_private_key()
        with self.assertRaises(crypto.DecryptError):
            crypto.unseal(priv, b"\x04" + bytes(64) + bytes(40))

    def test_group_message_key_is_not_the_group_key(self):
        gk = crypto.new_symmetric_key()
        k1 = crypto.group_message_key(gk, "g1")
        self.assertNotEqual(k1, gk)
        self.assertNotEqual(k1, crypto.group_message_key(gk, "g2"))

    def test_fips_status(self):
        with mock.patch.object(crypto._backend(), "_fips_enabled", True, create=True):
            self.assertTrue(crypto.fips_enabled())
            self.assertIn("FIPS mode on", crypto.fips_status())
        with mock.patch.object(crypto._backend(), "_fips_enabled", False, create=True):
            self.assertIn("FIPS mode off", crypto.fips_status())


class FormatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="paf-test-")
        self.shared = SharedDir(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _json_files(self):
        for dirpath, _, files in os.walk(self.tmp):
            for f in files:
                if f.endswith((".json", ".enc")):
                    with open(os.path.join(dirpath, f)) as fh:
                        yield os.path.relpath(os.path.join(dirpath, f), self.tmp), json.load(fh)

    def test_every_record_names_its_format(self):
        a = Session.register(self.shared, "alice", "pw-a")
        b = Session.register(self.shared, "bob", "pw-b")
        a.invite_contact("bob")
        gid = a.create_group("g")
        a.invite_group(gid, "bob")
        seen = set()
        for path, obj in self._json_files():
            if path.endswith("meta.json"):
                continue
            seen.add(path.split(os.sep)[0])
            self.assertEqual(obj.get("v"), crypto.FORMAT, path)
        fname, payload = b.list_invites()[0]
        b.accept_invite(fname, payload)
        for path, obj in self._json_files():
            seen.add(path.split(os.sep)[0])
        a.process_replies()
        a.send_message("dm", "bob", "hi")
        a.send_message("grp", gid, "all")
        for path, obj in self._json_files():
            if not path.endswith("meta.json"):
                seen.add(path.split(os.sep)[0])
                self.assertEqual(obj.get("v"), crypto.FORMAT, path)
        self.assertEqual(seen, {"users", "invites", "replies", "dm", "groups"})
        with open(self.shared.config_path("alice")) as fh:
            wrapper = json.load(fh)
        self.assertEqual(
            wrapper["kdf"],
            {"alg": "pbkdf2-hmac-sha256", "iterations": crypto.PBKDF2_ITERATIONS},
        )
        self.assertEqual(wrapper["aead"], "aes-256-gcm")

    def test_stored_iterations_are_honoured(self):
        with mock.patch.object(crypto, "PBKDF2_ITERATIONS", 1000):
            Session.register(self.shared, "alice", "pw")
        s = Session.login(self.shared, "alice", "pw")
        self.assertEqual(s._iterations, 1000)
        s.save_config()
        with open(self.shared.config_path("alice")) as fh:
            self.assertEqual(json.load(fh)["kdf"]["iterations"], 1000)

    def test_unknown_config_format_is_refused(self):
        Session.register(self.shared, "alice", "pw")
        path = self.shared.config_path("alice")
        with open(path) as fh:
            wrapper = json.load(fh)
        wrapper["v"] = 99
        with open(path, "w") as fh:
            json.dump(wrapper, fh)
        with self.assertRaisesRegex(StoreError, "format 99"):
            Session.login(self.shared, "alice", "pw")
        with open(path, "w") as fh:
            json.dump(["not", "a", "wrapper"], fh)
        with self.assertRaisesRegex(StoreError, "could not read"):
            Session.login(self.shared, "alice", "pw")
        with open(path, "w") as fh:
            json.dump({"v": crypto.FORMAT, "salt": "AAAA"}, fh)
        with self.assertRaisesRegex(StoreError, "could not read"):
            Session.login(self.shared, "alice", "pw")

    def test_unknown_record_formats_are_skipped(self):
        a = Session.register(self.shared, "alice", "pw-a")
        b = Session.register(self.shared, "bob", "pw-b")
        a.invite_contact("bob")
        b.accept_invite(*b.list_invites()[0])
        a.process_replies()
        a.send_message("dm", "bob", "real")
        d = a.dm_directory("bob")
        f = os.listdir(d)[0]
        with open(os.path.join(d, f)) as fh:
            w = json.load(fh)
        w["v"] = 3
        with open(os.path.join(d, "1-future.json"), "w") as fh:
            json.dump(w, fh)
        with open(os.path.join(d, "2-list.json"), "w") as fh:
            json.dump([1], fh)
        msgs = b.load_messages("dm", "alice", b.list_message_files("dm", "alice"))
        self.assertEqual([m["text"] for m in msgs.values()], ["real"])
        # a sealed record of an unknown format, and one that is not a dict
        inv = self.shared.invites_dir("bob")
        a._drop_sealed(inv, crypto.b64e(b.pub), {"type": "contact"})
        newest = sorted(os.listdir(inv))[-1]
        with open(os.path.join(inv, newest)) as fh:
            w = json.load(fh)
        w["v"] = 3
        with open(os.path.join(inv, newest), "w") as fh:
            json.dump(w, fh)
        with open(os.path.join(inv, "3-list.json"), "w") as fh:
            json.dump({"v": crypto.FORMAT, "sealed": crypto.b64e(
                crypto.seal(b.pub, b"[1, 2]"))}, fh)
        self.assertEqual(b.list_invites(), [])
        # a fresh account has no 0.x key, so a 0.x-shaped record is skipped
        with open(os.path.join(inv, "4-v0.json"), "w") as fh:
            json.dump({"sealed": crypto.b64e(bytes(100))}, fh)
        self.assertEqual(b.list_invites(), [])

    def test_identity_must_be_current(self):
        Session.register(self.shared, "alice", "pw")
        b = Session.register(self.shared, "bob", "pw")
        path = self.shared.identity_path("alice")
        with open(path) as fh:
            ident = json.load(fh)
        for bad in ("not!b64", crypto.b64e(bytes(65))):
            ident["pub"] = bad
            with open(path, "w") as fh:
                json.dump(ident, fh)
            with self.assertRaisesRegex(StoreError, "corrupt identity"):
                b.invite_contact("alice")

    def test_identity_is_repaired_at_login(self):
        Session.register(self.shared, "alice", "pw")
        os.unlink(self.shared.identity_path("alice"))
        with open(self.shared.identity_path("alice"), "w") as fh:
            json.dump({"name": "alice", "pub": "AAAA"}, fh)
        s = Session.login(self.shared, "alice", "pw")
        self.assertEqual(
            self.shared.read_identity("alice")["pub"], crypto.b64e(s.pub)
        )

    def test_corrupt_contact_keys(self):
        a = Session.register(self.shared, "alice", "pw")
        with self.assertRaisesRegex(StoreError, "corrupt key"):
            a._add_contact("bob", "not!b64")
        a.config["contacts"]["bob"] = crypto.b64e(bytes(7))
        with self.assertRaisesRegex(StoreError, "corrupt key"):
            a.send_message("dm", "bob", "hi")


# An account as 0.3.0 wrote it (passphrase "pw-a"), kept as data so that a
# host whose OpenSSL is in FIPS mode, which cannot write one, can still try
# to open one.
V0_IDENTITY = {"name": "alice", "pub": "4AI64IQqNc2IXHkhzLjhPEBr99mOKsfkX1xs/CCjQBM="}
V0_CONFIG = {
    "salt": "UkbUu1yH7R1xIP/3cfa4dw==",
    "kdf": {"n": 16384, "r": 8, "p": 1},
    "blob": (
        "zGwZgpXr2Txp18U/JhezcYFF1qIXbvB6vxnzuPR3eQ1XC9n7FmAp+x9oqz9lXpsqNvOZ"
        "9G8Y8cic+VTPx5t8NpwgVIMIs+PiSZT5ajIsx72JtoHLJ9XOLgexl25Vb9VCGEtW3Wzw"
        "YMXf3XfiiXN/GxImA7ySnqT8stRKwuV1JVIaaB2rX1EA59mVZ87ZV1t2JudPy2zXxSSK"
        "6ZwZj7yOvH6U7A=="
    ),
}


class StoredV0AccountTest(unittest.TestCase):
    """The same 0.x account opened in and out of FIPS mode."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="paf-test-")
        self.shared = SharedDir(self.tmp)
        os.makedirs(self.shared.user_dir("alice"))
        with open(self.shared.identity_path("alice"), "w") as fh:
            json.dump(V0_IDENTITY, fh)
        with open(self.shared.config_path("alice"), "w") as fh:
            json.dump(V0_CONFIG, fh)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @unittest.skipIf(crypto.fips_enabled(), "OpenSSL is in FIPS mode")
    def test_migrates_outside_fips_mode(self):
        a = Session.login(self.shared, "alice", "pw-a")
        self.assertEqual(a.config["legacy_pub"], V0_IDENTITY["pub"])
        self.assertEqual(self.shared.read_identity("alice")["v"], crypto.FORMAT)

    @unittest.skipUnless(crypto.fips_enabled(), "OpenSSL is not in FIPS mode")
    def test_fips_mode_refuses_plainly_and_changes_nothing(self):
        with self.assertRaisesRegex(StoreError, "not in FIPS mode"):
            Session.login(self.shared, "alice", "pw-a")
        with open(self.shared.config_path("alice")) as fh:
            self.assertEqual(json.load(fh), V0_CONFIG)
        with self.assertRaises(crypto.LegacyUnavailable):
            legacy.private_key_from_bytes(bytes(32))


@unittest.skipIf(
    crypto.fips_enabled(),
    "OpenSSL is in FIPS mode, so 0.x fixtures cannot be written "
    "(StoredV0AccountTest covers this host)",
)
class MigrationTest(unittest.TestCase):
    """A directory 0.3.0 wrote, opened by this version."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="paf-test-")
        self.shared = SharedDir(self.tmp)
        self.alice = V0User(self.tmp, "alice", "pw-a")
        self.bob = V0User(self.tmp, "bob", "pw-b")
        self.carol = V0User(self.tmp, "carol", "pw-c")
        self.alice.befriend(self.bob)
        self.dm = [
            self.alice.send_dm(self.bob, "hi bob"),
            self.bob.send_dm(self.alice, "hi alice"),
        ]
        self.gid = self.alice.create_group("team", [self.bob])
        self.alice.send_group(self.gid, "from alice")
        self.bob.send_group(self.gid, "from bob")
        self.carol.invite(self.alice)  # pending, sealed to alice's X25519 key

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _versions(self, directory):
        out = {}
        for f in sorted(os.listdir(directory)):
            with open(os.path.join(directory, f)) as fh:
                out[f] = json.load(fh).get("v")
        return out

    def texts(self, s, kind, target):
        files = s.list_message_files(kind, target)
        return [m["text"] for m in s.load_messages(kind, target, files).values()]

    def test_wrong_passphrase_on_a_0x_config(self):
        with self.assertRaisesRegex(StoreError, "wrong passphrase"):
            Session.login(self.shared, "alice", "nope")
        with open(self.shared.config_path("alice")) as fh:
            self.assertNotIn("v", json.load(fh))  # left untouched

    def test_full_migration(self):
        a = Session.login(self.shared, "alice", "pw-a")
        # config and identity moved to the new format; the old key is kept
        with open(self.shared.config_path("alice")) as fh:
            self.assertEqual(json.load(fh)["v"], crypto.FORMAT)
        ident = self.shared.read_identity("alice")
        self.assertEqual(ident["v"], crypto.FORMAT)
        self.assertEqual(ident["pub"], crypto.b64e(a.pub))
        self.assertEqual(ident["legacy_pub"], crypto.b64e(self.alice.pub))
        self.assertEqual(a.config["legacy_priv"], self.alice.config["priv"])
        self.assertEqual(Session.login(self.shared, "alice", "pw-a").pub, a.pub)

        # everything 0.x wrote still reads
        self.assertEqual(self.texts(a, "dm", "bob"), ["hi bob", "hi alice"])
        self.assertEqual(
            self.texts(a, "grp", self.gid), ["from alice", "from bob"]
        )
        # her own group message was re-encrypted at login; bob's was not
        gdir = self.shared.group_msgs_dir(self.gid)
        self.assertEqual(sorted(self._versions(gdir).values(), key=str),
                         [2, None])
        # bob has not upgraded: no new DM to him, nothing of hers re-encrypted
        with self.assertRaisesRegex(StoreError, "has not upgraded"):
            a.send_message("dm", "bob", "x")
        with self.assertRaisesRegex(StoreError, "has not upgraded"):
            a.invite_contact("carol")
        self.assertEqual(
            a.migrate(), [("bob", 2), ("#team", 1)]
        )
        # carol's 0.x invite is listed, but cannot be answered until she
        # upgrades; declining it just drops it
        (fname, payload), = a.list_invites()
        self.assertEqual(payload["from"], "carol")
        with self.assertRaisesRegex(StoreError, "carol has not upgraded"):
            a.accept_invite(fname, payload)
        self.assertNotIn("carol", a.config["contacts"])

        # bob upgrades: his own messages move, and the pair can talk again
        b = Session.login(self.shared, "bob", "pw-b")
        self.assertEqual(self.texts(b, "dm", "alice"), ["hi bob", "hi alice"])
        self.assertEqual(a.migrate(), [])
        self.assertEqual(
            set(self._versions(a.dm_directory("bob")).values()), {2}
        )
        self.assertEqual(set(self._versions(gdir).values()), {2})
        a.send_message("dm", "bob", "upgraded")
        self.assertEqual(
            self.texts(b, "dm", "alice"), ["hi bob", "hi alice", "upgraded"]
        )
        self.assertEqual(b.config["contacts"]["alice"], crypto.b64e(a.pub))
        self.assertEqual(
            self.texts(b, "grp", self.gid), ["from alice", "from bob"]
        )
        self.assertEqual(b.config["legacy_contacts"]["alice"],
                         crypto.b64e(self.alice.pub))

    def test_invites_across_the_upgrade(self):
        a = Session.login(self.shared, "alice", "pw-a")
        (fname, payload), = a.list_invites()
        # carol upgrades; now her 0.x invite can be accepted, and the reply
        # goes to her new key
        c = Session.login(self.shared, "carol", "pw-c")
        a.accept_invite(fname, payload)
        self.assertEqual(a.config["contacts"]["carol"], crypto.b64e(self.carol.pub))
        notices = c.process_replies()
        self.assertEqual(notices, ["alice accepted your chat invite"])
        a.send_message("dm", "carol", "hello carol")
        self.assertEqual(a.config["contacts"]["carol"], crypto.b64e(c.pub))
        self.assertEqual(self.texts(c, "dm", "alice"), ["hello carol"])

    def test_decline_and_remove_with_a_0x_peer(self):
        self.bob.invite(self.carol)
        c = Session.login(self.shared, "carol", "pw-c")
        (fname, payload), = c.list_invites()
        c.decline_invite(fname, payload)  # bob cannot read a reply: none sent
        self.assertEqual(os.listdir(self.shared.replies_dir("bob")), [])
        self.assertEqual(c.list_invites(), [])
        a = Session.login(self.shared, "alice", "pw-a")
        a.remove_contact("bob")  # no notice: bob is still on 0.x
        self.assertEqual(os.listdir(self.shared.replies_dir("bob")), [])
        # once a peer has upgraded, the notice goes to the new key
        a2 = Session.login(self.shared, "alice", "pw-a")
        a2.config["contacts"]["carol"] = crypto.b64e(self.carol.pub)
        a2.remove_contact("carol")
        self.assertEqual(c.process_replies(), [])  # carol had no contact entry
        self.assertEqual(os.listdir(self.shared.replies_dir("carol")), [])

    def test_contact_whose_identity_is_gone(self):
        a = Session.login(self.shared, "alice", "pw-a")
        shutil.rmtree(self.shared.user_dir("bob"))
        with self.assertRaisesRegex(StoreError, "bob has not upgraded"):
            a.send_message("dm", "bob", "x")
        self.assertEqual(self.texts(a, "dm", "bob"), ["hi bob", "hi alice"])

    def test_a_forged_identity_does_not_capture_a_contact(self):
        a = Session.login(self.shared, "alice", "pw-a")
        mallory = crypto.generate_private_key()
        with open(self.shared.identity_path("bob"), "w") as fh:
            json.dump({"name": "bob", "v": 2,
                       "pub": crypto.b64e(crypto.public_key_bytes(mallory)),
                       "legacy_pub": crypto.b64e(bytes(32))}, fh)
        with self.assertRaisesRegex(StoreError, "has not upgraded"):
            a.send_message("dm", "bob", "x")

    def test_fips_openssl_refuses_the_migration(self):
        refuse = mock.patch.object(
            legacy.Scrypt, "__init__", side_effect=UnsupportedAlgorithm("fips")
        )
        with refuse, self.assertRaisesRegex(StoreError, "not in FIPS mode"):
            Session.login(self.shared, "alice", "pw-a")
        # migrated elsewhere; afterwards this OpenSSL refuses X25519
        a = Session.login(self.shared, "alice", "pw-a")
        with mock.patch.object(
            legacy.X25519PrivateKey, "from_private_bytes",
            side_effect=UnsupportedAlgorithm("fips"),
        ):
            a2 = Session.login(self.shared, "alice", "pw-a")
            self.assertEqual(self.texts(a2, "dm", "bob"), [])  # unreadable here
            self.assertEqual(a2.list_invites(), [])
        self.assertEqual(len(a.list_invites()), 1)

    def test_legacy_reader_edges(self):
        priv = legacy.private_key_from_bytes(
            crypto.b64d(self.alice.config["priv"])
        )
        for bad in (b"short", bytes(32) + bytes(40)):
            with self.assertRaises(crypto.DecryptError):
                legacy.unseal(priv, bad)
        with self.assertRaises(crypto.DecryptError):
            legacy.sym_decrypt(bytes(32), b"short")

    def test_reencrypt_of_a_file_not_ours_is_left(self):
        a = Session.login(self.shared, "alice", "pw-a")
        Session.login(self.shared, "bob", "pw-b")
        a._conv("dm", "bob")  # takes bob's new key (and saves the config)
        with mock.patch.object(store, "_atomic_write", side_effect=PermissionError):
            self.assertEqual(a.migrate(), [("bob", 1)])
        self.assertEqual(a.migrate(), [])

    def test_migrate_skips_unreadable_entries(self):
        a = Session.login(self.shared, "alice", "pw-a")
        os.mkdir(os.path.join(self.shared.dm_dir("alice", "bob"), "0-dir.json"))
        self.assertEqual(a.migrate(), [("bob", 2), ("#team", 1)])

    def test_corrupt_identity_is_rewritten_at_login(self):
        with open(self.shared.identity_path("alice"), "w") as fh:
            fh.write("{ nope")
        a = Session.login(self.shared, "alice", "pw-a")
        self.assertEqual(
            self.shared.read_identity("alice")["pub"], crypto.b64e(a.pub)
        )

    def test_corrupt_legacy_contact_key_is_skipped(self):
        a = Session.login(self.shared, "alice", "pw-a")
        a.config["legacy_contacts"]["bob"] = "not!b64"
        self.assertEqual(self.texts(a, "dm", "bob"), [])

    def test_cli_migrate(self):
        out = io.StringIO()
        argv = ["-d", self.tmp, "-u", "alice", "--passphrase", "pw-a", "migrate"]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            cli.main(argv)
        self.assertIn("bob: 2 message(s) still in the 0.x format", out.getvalue())
        Session.login(self.shared, "bob", "pw-b")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            cli.main(argv)
        self.assertIn("everything you can read", out.getvalue())


class FipsWarningTest(unittest.TestCase):
    def run_main(self, argv, fips, env=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(crypto, "fips_enabled", return_value=fips), \
                mock.patch.dict(os.environ, env or {}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main(argv)
            except SystemExit:
                pass
        return out.getvalue(), err.getvalue()

    def test_warns_but_runs_when_not_in_fips_mode(self):
        tmp = tempfile.mkdtemp(prefix="paf-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        argv = ["-d", tmp, "-u", "alice", "--passphrase", "pw", "register"]
        os.environ.pop("PAF_FIPS_WARNING", None)
        out, err = self.run_main(argv, fips=False)
        self.assertIn("registered alice", out)
        self.assertIn("not running as a FIPS-validated module", err)

    def test_silent_in_fips_mode_or_when_asked(self):
        os.environ.pop("PAF_DIR", None)
        _, err = self.run_main(["status"], fips=True)
        self.assertNotIn("warning", err)
        _, err = self.run_main(["status"], fips=False,
                               env={"PAF_FIPS_WARNING": "0"})
        self.assertNotIn("warning", err)
        self.assertIn("no shared directory", err)

    def test_version_reports_the_crypto(self):
        out, _ = self.run_main(["--version"], fips=False)
        self.assertIn("Crypto: OpenSSL", out)


class TuiFipsTest(unittest.TestCase):
    def test_status_line(self):
        from phone_a_friend import tui

        tmp = tempfile.mkdtemp(prefix="paf-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        s = Session.register(SharedDir(tmp), "alice", "pw")
        with mock.patch.object(crypto, "fips_enabled", return_value=False):
            self.assertIn("not in FIPS mode", tui.App(s).status)
        with mock.patch.object(crypto, "fips_enabled", return_value=True):
            self.assertNotIn("FIPS", tui.App(s).status)


if __name__ == "__main__":
    unittest.main()
