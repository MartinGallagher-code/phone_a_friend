# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher

"""Invites and replies are from who they say, a contact's key is never
swapped, and safety numbers let people check (PAF-009).

The first class is the proof of concept the ticket was filed with, as
tests: each of its attacks worked on 2.0.0.
"""

import builtins
import contextlib
import curses
import hashlib
import io
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from phone_a_friend import __main__ as cli  # noqa: E402
from phone_a_friend import crypto, store, tui  # noqa: E402
from phone_a_friend.store import SharedDir, Session, StoreError  # noqa: E402
from test_tui import (  # noqa: E402
    B3_CLICK, ENTER, ESC, MOUSE, FakeScreen, TuiCase, ev, fake_curses,
)
from v0_store import V0User  # noqa: E402

B64 = crypto.b64e
UNSIGNED = "nothing shows it is {0}'s (phone_a_friend before 2.1 signs none)"
NO_0X = unittest.skipIf(
    crypto.fips_enabled(),
    "OpenSSL is in FIPS mode, so 0.x fixtures cannot be written",
)


def drop(sender, box, recipient, payload):
    """``sender`` (a Session) seals ``payload`` to ``recipient``'s key and
    drops it in ``box``, as anyone who can write to the share can."""
    sender._drop_sealed(box, B64(recipient.pub), payload)


def texts(s, other):
    files = s.list_message_files("dm", other)
    return [m["text"] for m in s.load_messages("dm", other, files).values()]


@contextlib.contextmanager
def owned_by_another(*dirs):
    """Files in ``dirs`` look as if another Unix account owned them."""
    real = os.lstat

    def lstat(path, *args, **kwargs):
        st = real(path, *args, **kwargs)
        if any(str(path).startswith(d + os.sep) for d in dirs):
            fields = list(st)
            fields[4] = st.st_uid + 1  # st_uid
            return os.stat_result(fields)
        return st

    with mock.patch.object(store.os, "lstat", lstat):
        yield


class AuthCase(TuiCase):
    def register(self, name):
        return Session.register(self.shared, name, "pw")

    def reregister(self, name):
        """``name`` loses their account and makes it again: a new key."""
        shutil.rmtree(self.shared.user_dir(name))
        return Session.register(self.shared, name, "pw2")

    def unseal_all(self, s, box):
        out = []
        for f in sorted(os.listdir(box)):
            out.append(s._read_sealed(os.path.join(box, f)))
        return out


class ProofOfConceptTest(AuthCase):
    """PAF-009-poc.py's three attacks, each of which worked on 2.0.0."""

    def test_an_unsolicited_acceptance_cannot_swap_a_contacts_key(self):
        a, b = self.make_pair()
        m = self.register("mallory")
        drop(m, self.shared.replies_dir("alice"), a,
             {"type": "contact_accept", "from": "bob", "from_pub": B64(m.pub)})
        self.assertEqual(a.process_replies(), [
            "refused a chat acceptance claiming to be from bob: "
            "the key in it is not bob's"])
        self.assertEqual(a.config["contacts"]["bob"], B64(b.pub))
        self.assertEqual(os.listdir(self.shared.replies_dir("alice")), [])
        a.send_message("dm", "bob", "secret for bob")
        m.config["contacts"]["alice"] = B64(a.pub)
        self.assertEqual(texts(m, "alice"), [])
        self.assertEqual(texts(b, "alice"), ["secret for bob"])

    def test_a_forged_invite_is_not_listed(self):
        self.register("alice")
        d = self.register("dave")
        m = self.register("mallory")
        drop(m, self.shared.invites_dir("dave"), d,
             {"type": "contact", "from": "alice", "from_pub": B64(m.pub), "ts": 0})
        self.assertEqual(d.list_invites(), [])
        self.assertEqual(d.take_notices(), [
            "refused a chat invite claiming to be from alice: "
            "the key in it is not alice's"])
        self.assertEqual(os.listdir(self.shared.invites_dir("dave")), [])
        self.assertEqual(d.config["contacts"], {})

    def test_forged_removals_are_not_honoured(self):
        a, b = self.make_pair()
        m = self.register("mallory")
        gid = a.create_group("g")
        a.invite_group(gid, "bob")
        b.accept_invite(*b.list_invites()[0])
        a.process_replies()
        replies = self.shared.replies_dir("bob")
        drop(m, replies, b, {"type": "contact_removed", "from": "alice"})
        drop(m, replies, b, {"type": "group_removed", "from": "alice",
                             "group_id": gid, "group_name": "g"})
        self.assertEqual(b.process_replies(), [
            "ignored an unsigned chat removal notice from alice: "
            + UNSIGNED.format("alice"),
            "ignored an unsigned group removal notice from alice: "
            + UNSIGNED.format("alice"),
        ])
        self.assertIn("alice", b.config["contacts"])
        self.assertIn(gid, b.config["groups"])


class AcceptanceTest(AuthCase):
    def test_an_acceptance_must_answer_an_invite_we_sent(self):
        a = self.register("alice")
        b = self.register("bob")
        drop(b, self.shared.replies_dir("alice"), a,
             {"type": "contact_accept", "from": "bob", "from_pub": B64(b.pub)})
        self.assertEqual(a.process_replies(), [
            "ignored a chat acceptance from bob: you did not invite bob"])
        self.assertEqual(a.config["contacts"], {})

    def test_an_acceptance_must_carry_the_senders_key(self):
        a = self.register("alice")
        b = self.register("bob")
        m = self.register("mallory")
        a.invite_contact("bob")
        drop(m, self.shared.replies_dir("alice"), a,
             {"type": "contact_accept", "from": "bob", "from_pub": B64(m.pub)})
        self.assertIn("the key in it is not bob's", a.process_replies()[0])
        self.assertNotIn("bob", a.config["contacts"])
        # bob's own answer still counts
        b.accept_invite(*b.list_invites()[0])
        self.assertEqual(a.process_replies(), ["bob accepted your chat invite"])
        self.assertEqual(a.config["contacts"]["bob"], B64(b.pub))

    def test_an_acceptance_written_by_another_device_of_ours(self):
        a1 = self.register("alice")
        a2 = Session.login(self.shared, "alice", "pw")  # read before the invite
        b = self.register("bob")
        a1.invite_contact("bob")
        b.accept_invite(*b.list_invites()[0])
        self.assertEqual(a2.config["pending"], [])
        self.assertEqual(a2.process_replies(), ["bob accepted your chat invite"])
        # a config on disk that cannot be read pends nothing
        with open(self.shared.config_path("alice"), "w") as fh:
            fh.write("{ nope")
        self.assertEqual(a2._pending_on_disk(), [])

    def test_mutual_invites_are_answered_once(self):
        a = self.register("alice")
        b = self.register("bob")
        a.invite_contact("bob")
        b.invite_contact("alice")
        a.accept_invite(*a.list_invites()[0])
        b.accept_invite(*b.list_invites()[0])
        self.assertEqual(a.process_replies(), ["bob accepted your chat invite"])
        self.assertEqual(b.process_replies(), ["alice accepted your chat invite"])
        self.assertEqual(a.config["contacts"]["bob"], B64(b.pub))
        self.assertEqual(b.config["contacts"]["alice"], B64(a.pub))


class InviteTest(AuthCase):
    def test_a_group_invite_with_another_key_is_refused(self):
        self.register("alice")
        d = self.register("dave")
        m = self.register("mallory")
        gid = m.create_group("trap")
        drop(m, self.shared.invites_dir("dave"), d, {
            "type": "group", "from": "alice", "from_pub": B64(m.pub),
            "group_id": gid, "group_name": "trap",
            "group_key": m.config["groups"][gid]["key"], "ts": 0})
        self.assertEqual(d.list_invites(), [])
        # what list_invites refused comes out of process_replies, last
        self.assertEqual(d.process_replies(), [
            "refused a group invite claiming to be from alice: "
            "the key in it is not alice's"])
        self.assertEqual(d.process_replies(), [])

    def test_a_payload_handed_in_directly_is_checked_too(self):
        self.register("alice")
        d = self.register("dave")
        m = self.register("mallory")
        forged = {"type": "contact", "from": "alice", "from_pub": B64(m.pub)}
        with self.assertRaisesRegex(StoreError, "the key in it is not alice's"):
            d.accept_invite("x.json", forged)
        self.assertEqual(d.config["contacts"], {})
        # and declining it answers no one
        d.decline_invite("x.json", forged)
        self.assertEqual(os.listdir(self.shared.replies_dir("alice")), [])
        self.assertEqual(os.listdir(self.shared.replies_dir("mallory")), [])

    def test_the_sender_must_be_a_user(self):
        a = self.register("alice")
        m = self.register("mallory")
        box = self.shared.invites_dir("alice")
        for sender, notice in (
            ("zed", "refused a chat invite claiming to be from zed: no such user"),
            ("../users/mallory", "refused a chat invite with no valid sender"),
            ("mallory\n", "refused a chat invite with no valid sender"),
            (["mallory"], None),  # not even a name: dropped unread
            ("alice", "refused a chat invite claiming to be from you"),
        ):
            with self.subTest(sender=sender):
                drop(m, box, a, {"type": "contact", "from": sender,
                                 "from_pub": B64(m.pub)})
                self.assertEqual(a.list_invites(), [])
                self.assertEqual(a.take_notices(), [notice] if notice else [])

    def test_malformed_and_unknown_invites_are_dropped(self):
        a = self.register("alice")
        m = self.register("mallory")
        box = self.shared.invites_dir("alice")
        drop(m, box, a, {"type": "group", "from": "mallory",
                         "from_pub": B64(m.pub), "group_id": ["x"],
                         "group_name": "g", "group_key": "k"})
        drop(m, box, a, {"type": "carrier-pigeon", "from": "mallory"})
        self.assertEqual(a.list_invites(), [])
        self.assertEqual(a.take_notices(), [])
        self.assertEqual(os.listdir(box), [])

    def test_identity_key_forms(self):
        a = self.register("alice")
        self.register("bob")
        bob_pub = self.shared.read_identity("bob")["pub"]
        self.assertTrue(a._is_identity_key("bob", bob_pub))
        for bad in (None, "not!b64", B64(bytes(32)), B64(bytes(65))):
            with self.subTest(key=bad):
                self.assertFalse(a._is_identity_key("bob", bad))
        # an identity in a format this client does not know names no key
        path = self.shared.identity_path("bob")
        with open(path) as fh:
            ident = json.load(fh)
        with open(path, "w") as fh:
            json.dump(dict(ident, v=99), fh)
        self.assertFalse(a._is_identity_key("bob", bob_pub))
        self.assertFalse(a._is_identity_key("nobody", bob_pub))


class OwnerTest(AuthCase):
    MSG = "its file belongs to another account than {0}'s"

    def test_an_invite_in_another_accounts_file_is_refused(self):
        a = self.register("alice")
        b = self.register("bob")
        a.invite_contact("bob")
        with owned_by_another(self.shared.invites_dir("bob")):
            self.assertEqual(b.list_invites(), [])
        self.assertEqual(b.take_notices(), [
            "refused a chat invite claiming to be from alice: "
            + self.MSG.format("alice")])

    def test_a_reply_in_another_accounts_file_is_refused(self):
        a = self.register("alice")
        b = self.register("bob")
        a.invite_contact("bob")
        b.accept_invite(*b.list_invites()[0])
        with owned_by_another(self.shared.replies_dir("alice")):
            notices = a.process_replies()
        self.assertEqual(notices, [
            "refused a chat acceptance claiming to be from bob: "
            + self.MSG.format("bob")])
        self.assertNotIn("bob", a.config["contacts"])

    def test_a_symlink_is_refused(self):
        a = self.register("alice")
        b = self.register("bob")
        a.invite_contact("bob")
        b.accept_invite(*b.list_invites()[0])
        box = self.shared.replies_dir("alice")
        (fname,) = os.listdir(box)
        aside = os.path.join(self.tmp, "aside.json")
        os.rename(os.path.join(box, fname), aside)
        os.symlink(aside, os.path.join(box, fname))
        self.assertIn(self.MSG.format("bob"), a.process_replies()[0])

    def test_owner_helpers_without_files(self):
        a = self.register("alice")
        self.assertFalse(a._owner_ok(os.path.join(self.tmp, "none"), "alice"))
        self.assertFalse(a._owner_proves("nobody"))

    def test_an_older_clients_unsigned_notice_counts_on_a_multi_user_system(self):
        a, b = self.make_pair()
        gid = a.create_group("g")
        a.invite_group(gid, "bob")
        b.accept_invite(*b.list_invites()[0])
        a.process_replies()
        replies = self.shared.replies_dir("bob")
        # what 2.0 sent: no signature
        drop(a, replies, b, {"type": "group_removed", "from": "alice",
                             "group_id": gid, "group_name": "g"})
        drop(a, replies, b, {"type": "contact_removed", "from": "alice"})
        # alice is another Unix account than bob, so her file shows it is
        # hers; a group removal must be signed all the same, since its
        # sender's account cannot show they are in the group
        with mock.patch.object(store.os, "getuid", return_value=os.getuid() + 1):
            notices = b.process_replies()
        self.assertEqual(notices, [
            "alice ended your chat - a new invite can restore it",
            "ignored an unsigned group removal notice from alice: "
            + UNSIGNED.format("alice"),
        ])
        self.assertIn(gid, b.config["groups"])

    @unittest.skipUnless(os.geteuid() == 0, "giving a file away needs root")
    def test_real_owners(self):
        a = self.register("alice")
        b = self.register("bob")
        c = self.register("carol")
        a.invite_contact("bob")
        (fname,) = os.listdir(self.shared.invites_dir("bob"))
        os.chown(os.path.join(self.shared.invites_dir("bob"), fname), 4242, -1)
        self.assertEqual(b.list_invites(), [])
        self.assertIn(self.MSG.format("alice"), b.take_notices()[0])
        # carol's identity and her invite are both 4242's: hers
        c.invite_contact("bob")
        os.chown(self.shared.identity_path("carol"), 4242, -1)
        for f in os.listdir(self.shared.invites_dir("bob")):
            os.chown(os.path.join(self.shared.invites_dir("bob"), f), 4242, -1)
        self.assertEqual([p["from"] for _, p in b.list_invites()], ["carol"])


class SignedReplyTest(AuthCase):
    def test_every_reply_is_signed(self):
        a, b = self.make_pair()
        self.assertEqual(os.listdir(self.shared.replies_dir("alice")), [])
        gid = a.create_group("g")
        a.invite_group(gid, "bob")
        b.decline_invite(*b.list_invites()[0])
        a.invite_group(gid, "bob")
        b.accept_invite(*b.list_invites()[0])
        a.remove_group_member(gid, "bob")
        a.remove_contact("bob")
        sent = self.unseal_all(a, self.shared.replies_dir("alice"))
        sent += self.unseal_all(b, self.shared.replies_dir("bob"))
        self.assertEqual(sorted(p["type"] for p in sent),
                         ["contact_removed", "group_accept", "group_decline",
                          "group_removed"])
        for p in sent:
            self.assertRegex(p["auth"], r"^[A-Za-z0-9+/]{43}=$")

    def test_a_bad_signature_is_refused(self):
        a, b = self.make_pair()
        m = self.register("mallory")
        box = self.shared.replies_dir("bob")
        # signed by mallory, claiming alice
        drop(m, box, b, m._signed({"type": "contact_removed", "from": "alice"}, b.pub))
        # alice's signature, over other fields
        signed = a._signed({"type": "contact_removed", "from": "alice"}, b.pub)
        drop(m, box, b, dict(signed, extra="x"))
        drop(m, box, b, dict(signed, auth="not!b64"))
        refused = ("refused a chat removal notice claiming to be from alice: "
                   "it is not signed with alice's key")
        self.assertEqual(b.process_replies(), [refused])  # said once
        self.assertIn("alice", b.config["contacts"])
        self.assertFalse(b._auth_ok({"from": "ghost", "auth": "AAAA"},
                                    "contact_removed"))

    def test_only_a_member_can_sign_a_group_removal(self):
        a, b = self.make_pair()
        m = self.register("mallory")
        gid = a.create_group("g")
        a.invite_group(gid, "bob")
        b.accept_invite(*b.list_invites()[0])
        # mallory, not in the group, guesses at its key
        m.config["groups"][gid] = {"name": "g",
                                   "key": B64(crypto.new_symmetric_key())}
        forged = m._signed({"type": "group_removed", "from": "mallory",
                            "group_id": gid, "group_name": "g"}, b.pub, gid)
        drop(m, self.shared.replies_dir("bob"), b, forged)
        self.assertEqual(b.process_replies(), [
            "refused a group removal notice claiming to be from mallory: "
            "it is not signed with mallory's key"])
        self.assertIn(gid, b.config["groups"])
        # a member's removal works
        a.remove_group_member(gid, "bob")
        self.assertEqual(b.process_replies(), ["alice removed you from group 'g'"])

    def test_replies_about_nothing_of_ours_are_dropped_unread(self):
        a = self.register("alice")
        m = self.register("mallory")
        box = self.shared.replies_dir("alice")
        for payload in (
            {"type": "contact_decline", "from": "mallory"},
            {"type": "group_accept", "from": "mallory", "group_id": "g"},
            {"type": "group_decline", "from": "mallory", "group_id": "g"},
            {"type": "contact_removed", "from": "mallory"},
            {"type": "group_removed", "from": "mallory", "group_id": "g"},
            {"type": "group_removed", "from": "mallory", "group_id": ["g"]},
            {"type": "contact_accept", "from": "mallory"},
            {"type": "contact", "from": "mallory", "from_pub": B64(m.pub)},
        ):
            drop(m, box, a, payload)
        self.assertEqual(a.process_replies(), [])
        self.assertEqual(os.listdir(box), [])

    def test_a_decline_from_someone_else_is_refused(self):
        a = self.register("alice")
        self.register("bob")
        m = self.register("mallory")
        a.invite_contact("bob")
        drop(m, self.shared.replies_dir("alice"), a,
             m._signed({"type": "contact_decline", "from": "bob",
                        "group_id": None}, a.pub))
        self.assertIn("it is not signed with bob's key", a.process_replies()[0])
        self.assertEqual(a.config["pending"], [{"type": "contact", "to": "bob"}])


class KeyChangeTest(AuthCase):
    CHANGED = "bob's key changed — /verify bob"

    def test_a_new_key_is_never_taken_silently(self):
        a, b = self.make_pair()
        b2 = self.reregister("bob")
        self.assertEqual(a.process_replies(), [self.CHANGED])
        self.assertEqual(a.process_replies(), [])  # said once
        self.assertEqual(a.config["contacts"]["bob"], B64(b.pub))
        self.assertEqual(a.config["key_changes"], {"bob": B64(b2.pub)})
        self.assertFalse(a.is_verified("bob"))
        # the new bob's invite is refused the same way
        b2.invite_contact("alice")
        self.assertEqual(a.list_invites(), [])
        self.assertEqual(a.take_notices(), [self.CHANGED])
        # the safety number is for the new key; accepting it moves alice to
        # it, and pushes her key to the new bob
        number, key, changed = a.safety_number("bob")
        self.assertTrue(changed)
        self.assertEqual(key, B64(b2.pub))
        self.assertEqual(number, crypto.safety_number(b2.pub, a.pub))
        self.assertTrue(a.confirm_key("bob", key))
        self.assertEqual(a.config["contacts"]["bob"], key)
        self.assertEqual(a.config["key_changes"], {})
        self.assertTrue(a.is_verified("bob"))
        b2.accept_invite(*b2.list_invites()[0])
        self.assertEqual(a.process_replies(), ["bob accepted your chat invite"])
        a.send_message("dm", "bob", "after")
        self.assertEqual(texts(b2, "alice"), ["after"])
        self.assertEqual(a.safety_number("bob")[0], b2.safety_number("alice")[0])

    def test_a_key_that_changes_again_is_not_taken(self):
        a, b = self.make_pair()
        self.reregister("bob")
        a.process_replies()
        _, key, _ = a.safety_number("bob")
        self.reregister("bob")
        with self.assertRaisesRegex(StoreError, "bob's key changed again"):
            a.confirm_key("bob", key)
        self.assertEqual(a.config["contacts"]["bob"], B64(b.pub))

    def test_an_identity_back_to_the_stored_key_is_no_change(self):
        a, b = self.make_pair()
        path = self.shared.identity_path("bob")
        with open(path, "rb") as fh:
            original = fh.read()
        self.reregister("bob")
        self.assertEqual(a.process_replies(), [self.CHANGED])
        with open(path, "wb") as fh:
            fh.write(original)
        self.assertEqual(a.process_replies(), [])
        self.assertEqual(a.config["key_changes"], {})
        # a key change noted, then the identity comes back before /verify
        a.config["key_changes"]["bob"] = B64(b.pub)
        number, key, changed = a.safety_number("bob")
        self.assertFalse(changed)
        self.assertFalse(a.confirm_key("bob", key))
        self.assertTrue(a.is_verified("bob"))

    def test_replies_and_invites_from_a_contact_with_another_key(self):
        a, b = self.make_pair()
        gid = a.create_group("g")
        b2 = self.reregister("bob")
        # an acceptance with the new key, of an invite alice did send
        a.config["pending"].append({"type": "contact", "to": "bob"})
        drop(b2, self.shared.replies_dir("alice"), a,
             {"type": "contact_accept", "from": "bob", "from_pub": B64(b2.pub)})
        self.assertEqual(a.process_replies(), [self.CHANGED])
        self.assertEqual(a.config["contacts"]["bob"], B64(b.pub))
        self.assertEqual(a.config["pending"], [])
        # a group invite with it
        gid2 = b2.create_group("h")
        b2.invite_group(gid2, "alice")
        self.assertEqual(a.list_invites(), [])
        self.assertEqual(a.take_notices(), [self.CHANGED])
        payload = {"type": "group", "from": "bob", "from_pub": B64(b2.pub),
                   "group_id": gid2, "group_name": "h",
                   "group_key": b2.config["groups"][gid2]["key"]}
        with self.assertRaisesRegex(StoreError, re.escape(self.CHANGED)):
            a.accept_invite("x.json", payload)
        with self.assertRaisesRegex(StoreError, re.escape(self.CHANGED)):
            a._add_contact("bob", B64(b2.pub))
        self.assertNotIn(gid2, a.config["groups"])
        self.assertIn(gid, a.config["groups"])
        # the same key again changes nothing
        a._add_contact("bob", B64(b.pub))
        self.assertEqual(a.config["contacts"]["bob"], B64(b.pub))

    def test_contacts_with_nothing_to_compare(self):
        a, b = self.make_pair()
        a.process_replies()
        shutil.rmtree(self.shared.user_dir("bob"))  # gone
        self.assertEqual(a.process_replies(), [])
        a.config["contacts"]["carol"] = "not!b64"  # corrupt
        Session.register(self.shared, "carol", "pw")
        self.assertEqual(a.process_replies(),
                         ["carol's key changed — /verify carol"])
        self.assertFalse(a._same_key("carol", "not!b64", "x!"))

    @NO_0X
    def test_a_key_change_noted_for_a_contact_still_on_0x(self):
        a = self.register("alice")
        V0User(self.tmp, "carol", "pw-c")
        notice = a._key_changed("carol", B64(bytes(32)))
        self.assertEqual(notice, "carol's key changed — /verify carol")
        self.assertEqual(a.config["key_changes"], {"carol": B64(bytes(32))})

    def test_unfriending_forgets_verification(self):
        a, b = self.make_pair()
        a.confirm_key("bob", a.safety_number("bob")[1])
        b.confirm_key("alice", b.safety_number("alice")[1])
        self.assertTrue(Session.login(self.shared, "alice", "pw").is_verified("bob"))
        a.remove_contact("bob")
        self.assertEqual(a.config["verified"], {})
        self.assertEqual(b.process_replies(),
                         ["alice ended your chat - a new invite can restore it"])
        self.assertEqual(b.config["verified"], {})


@NO_0X
class MigrationAuthTest(unittest.TestCase):
    """0.x contacts keep upgrading through legacy_pub, unflagged."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="paf-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.shared = SharedDir(self.tmp)
        self.alice = V0User(self.tmp, "alice", "pw-a")
        self.bob = V0User(self.tmp, "bob", "pw-b")
        self.alice.befriend(self.bob)

    def test_an_upgrade_is_not_a_key_change(self):
        a = Session.login(self.shared, "alice", "pw-a")
        self.assertEqual(a.process_replies(), [])  # bob still on 0.x
        b = Session.login(self.shared, "bob", "pw-b")
        self.assertEqual(a.process_replies(), [])
        self.assertEqual(b.process_replies(), [])
        self.assertEqual(a.safety_number("bob")[0], b.safety_number("alice")[0])
        self.assertEqual(a.config["contacts"]["bob"], B64(b.pub))

    def test_a_0x_key_not_named_by_the_identity_is_a_key_change(self):
        a = Session.login(self.shared, "alice", "pw-a")
        os.unlink(self.shared.identity_path("bob"))
        Session.register(self.shared, "bob2", "pw")  # a fresh, current key
        os.rename(self.shared.identity_path("bob2"),
                  self.shared.identity_path("bob"))
        with open(self.shared.identity_path("bob")) as fh:
            ident = json.load(fh)
        with open(self.shared.identity_path("bob"), "w") as fh:
            json.dump(dict(ident, name="bob"), fh)
        self.assertEqual(a.process_replies(), ["bob's key changed — /verify bob"])
        with self.assertRaisesRegex(StoreError, "has not upgraded"):
            a.send_message("dm", "bob", "x")

    def test_a_0x_invite_from_a_0x_user_is_listed(self):
        carol = V0User(self.tmp, "carol", "pw-c")
        carol.invite(self.alice)
        a = Session.login(self.shared, "alice", "pw-a")
        self.assertEqual([p["from"] for _, p in a.list_invites()], ["carol"])


class SafetyNumberTest(AuthCase):
    def test_the_same_on_both_sides(self):
        a, b = self.make_pair()
        m = self.register("mallory")
        number, key, changed = a.safety_number("bob")
        self.assertRegex(number, r"^(\d{5} ){11}\d{5}$")
        self.assertEqual(key, B64(b.pub))
        self.assertFalse(changed)
        self.assertEqual(number, b.safety_number("alice")[0])
        self.assertNotEqual(number, crypto.safety_number(a.pub, m.pub))
        with self.assertRaisesRegex(StoreError, "carol is not a contact"):
            a.safety_number("carol")

    def test_matches_an_independent_computation(self):
        a = crypto.public_key_bytes(crypto.generate_private_key())
        b = crypto.public_key_bytes(crypto.generate_private_key())
        lo, hi = sorted([a, b])
        digest = b"".join(
            hashlib.sha256(b"paf-safety-number-v1|" + bytes([i]) + lo + hi).digest()
            for i in (0, 1)
        )
        want = " ".join(
            "%05d" % (int.from_bytes(digest[i:i + 5], "big") % 100000)
            for i in range(0, 60, 5)
        )
        self.assertEqual(crypto.safety_number(a, b), want)
        self.assertEqual(crypto.safety_number(b, a), want)

    def test_hmac_sha256_known_answer(self):
        # RFC 4231, test case 1 (a 20-byte key: FIPS providers may refuse
        # HMAC keys under 112 bits)
        key = b"\x0b" * 20
        tag = crypto.mac(key, b"Hi There")
        self.assertEqual(tag.hex(), "b0344c61d8db38535ca8afceaf0bf12b"
                                    "881dc200c9833da726e9376c2e32cff7")
        self.assertTrue(crypto.mac_ok(key, b"Hi There", tag))
        self.assertFalse(crypto.mac_ok(key, b"Hi there", tag))

    def test_verified_survives_a_login_and_a_key_change_voids_it(self):
        a, b = self.make_pair()
        self.assertFalse(a.confirm_key("bob", a.safety_number("bob")[1]))
        a2 = Session.login(self.shared, "alice", "pw")
        self.assertTrue(a2.is_verified("bob"))
        self.reregister("bob")
        a2.process_replies()
        self.assertFalse(a2.is_verified("bob"))
        self.assertFalse(a2.is_verified("nobody"))


class HardeningTest(AuthCase):
    def mode(self, name):
        return stat.S_IMODE(os.stat(os.path.join(self.tmp, name)).st_mode)

    def test_new_top_directories_are_sticky_drop_boxes(self):
        self.register("alice")
        for top in ("users", "invites", "replies"):
            with self.subTest(top=top):
                self.assertEqual(self.mode(top), 0o1777)

    def test_login_adds_the_sticky_bit_to_ours(self):
        self.register("alice")
        os.chmod(os.path.join(self.tmp, "users"), 0o775)
        with mock.patch.object(store.os, "getuid", return_value=os.getuid() + 1):
            Session.login(self.shared, "alice", "pw")  # not ours: left
        self.assertEqual(self.mode("users"), 0o775)
        Session.login(self.shared, "alice", "pw")
        self.assertEqual(self.mode("users"), 0o1775)
        os.chmod(os.path.join(self.tmp, "users"), 0o775)
        with mock.patch.object(store.os, "chmod", side_effect=PermissionError):
            Session.login(self.shared, "alice", "pw")
        self.assertEqual(self.mode("users"), 0o775)

    def test_a_name_with_a_trailing_newline_is_refused(self):
        with self.assertRaisesRegex(StoreError, "username must be"):
            Session.register(self.shared, "bob\n", "pw")


class VerifyTuiTest(AuthCase):
    def last_frame(self, scr):
        return scr.rendered()

    def test_verify_a_contact(self):
        a, b = self.make_pair()
        number = a.safety_number("bob")[0]
        app, scr = self.run_app(
            a, list("/verify bob") + [ENTER, ESC, "y", ENTER, -1])
        shown = scr.rendered()
        self.assertIn("safety number with bob", shown)
        self.assertIn(" ".join(number.split()[:4]), shown)
        self.assertIn("not verified yet", shown)
        self.assertEqual(app.status, "bob verified ✓")
        self.assertTrue(a.is_verified("bob"))
        self.assertEqual(app._sidebar_label("dm", "bob")[0], "  bob ✓")
        # opened: the title says so; /verify alone is the open chat's, and
        # asks nothing more
        app, scr = self.run_app(a, [ENTER] + list("/verify") + [ENTER, ESC, -1])
        self.assertIn("chat with bob - verified ✓", scr.rendered())
        self.assertIn("verified ✓", scr.rendered())
        self.assertEqual(app.status, tui.HELP)

    def test_saying_no_or_naming_no_one(self):
        a, b = self.make_pair()
        app, _ = self.run_app(a, list("/verify bob") + [ENTER, ESC, "n", ENTER, -1])
        self.assertFalse(a.is_verified("bob"))
        app, _ = self.run_app(a, list("/verify") + [ENTER, -1])
        self.assertEqual(app.status, "open a chat first, or /verify USER")
        app, _ = self.run_app(a, list("/verify carol") + [ENTER, -1])
        self.assertIn("error: carol is not a contact", app.status)

    def test_from_the_chat_menu(self):
        a, b = self.make_pair()
        with mock.patch.object(a, "confirm_key", wraps=a.confirm_key) as confirm:
            self.run_app(a, [MOUSE, curses.KEY_DOWN, curses.KEY_DOWN, ENTER,
                             ESC, "y", ENTER, -1],
                         mouse_events=[ev(3, 1, B3_CLICK)])
            confirm.assert_called_once_with("bob", B64(b.pub))

    def test_a_changed_key(self):
        a, b = self.make_pair()
        b2 = self.reregister("bob")
        app, scr = self.run_app(a, [-1, ENTER, -1])
        self.assertEqual(app._sidebar_label("dm", "bob")[0], "  bob !")
        self.assertIn("chat with bob - key changed: /verify bob", scr.rendered())
        app, scr = self.run_app(a, list("/verify bob") + [ENTER, ESC, "y", ENTER, -1])
        self.assertIn("bob's key changed", scr.rendered())
        self.assertIn("accept bob's new key", scr.rendered())
        self.assertEqual(
            app.status, "bob's new key accepted - verified ✓, your key pushed to bob")
        self.assertEqual(a.config["contacts"]["bob"], B64(b2.pub))

    def test_a_key_that_changes_while_the_number_is_shown(self):
        a, b = self.make_pair()
        with mock.patch.object(a, "confirm_key",
                               side_effect=StoreError("bob's key changed again")):
            app, _ = self.run_app(a, list("/verify bob") + [ENTER, ESC, "y", ENTER, -1])
        self.assertEqual(app.status, "error: bob's key changed again")

    def test_a_refusal_reaches_the_status_line(self):
        a, b = self.make_pair()
        m = self.register("mallory")
        drop(m, self.shared.replies_dir("alice"), a,
             {"type": "contact_accept", "from": "bob", "from_pub": B64(m.pub)})
        app, _ = self.run_app(a, [-1])
        self.assertIn("refused a chat acceptance claiming to be from bob", app.status)


class VerifyCliTest(AuthCase):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"PAF_FIPS_WARNING": "0"})
        env.start()
        self.addCleanup(env.stop)

    def cli(self, user, *argv, answer=None, expect_exit=None):
        out, err = io.StringIO(), io.StringIO()
        code = None
        side = answer if isinstance(answer, BaseException) else None
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(builtins, "input", side_effect=side,
                                  return_value=answer):
            try:
                cli.main(["-d", self.tmp, "-u", user, "--passphrase", "pw", *argv])
            except SystemExit as exc:
                code = exc.code
        self.assertEqual(code, expect_exit)
        return out.getvalue() + err.getvalue()

    def test_paf_verify(self):
        a, b = self.make_pair()
        number = a.safety_number("bob")[0].split()
        out = self.cli("alice", "verify", "bob", answer="n")
        self.assertIn("safety number with bob:", out)
        self.assertIn("  " + " ".join(number[:4]), out)
        self.assertIn("  " + " ".join(number[8:]), out)
        self.assertIn("bob is not verified", out)
        out = self.cli("alice", "verify", "bob", answer=EOFError())
        self.assertIn("bob is not verified", out)
        out = self.cli("alice", "verify", "bob", answer="y")
        self.assertIn("bob verified", out)
        out = self.cli("alice", "verify", "bob")
        self.assertIn("bob is verified", out)
        self.assertIn("bob  (unread: 0)  verified", self.cli("alice", "status"))
        self.assertIn("carol is not a contact",
                      self.cli("alice", "verify", "carol", expect_exit=1))

    def test_paf_verify_a_changed_key(self):
        a, b = self.make_pair()
        self.reregister("bob")
        self.assertIn("bob  (unread: 0)  key changed: paf verify bob",
                      self.cli("alice", "status"))
        out = self.cli("alice", "verify", "bob", answer="y")
        self.assertIn("safety number with bob's NEW key:", out)
        self.assertIn("bob's new key accepted - verified; your key was pushed "
                      "to bob as a chat invite", out)

    def test_refused_invites_are_printed(self):
        self.register("alice")
        d = self.register("dave")
        m = self.register("mallory")
        for _ in range(2):
            drop(m, self.shared.invites_dir("dave"), d,
                 {"type": "contact", "from": "alice", "from_pub": B64(m.pub)})
        refused = "* refused a chat invite claiming to be from alice"
        self.assertIn(refused, self.cli("dave", "invites"))
        drop(m, self.shared.invites_dir("dave"), d,
             {"type": "contact", "from": "alice", "from_pub": B64(m.pub)})
        out = self.cli("dave", "accept")
        self.assertIn(refused, out)
        self.assertIn("nothing to accept", out)


if __name__ == "__main__":
    unittest.main()
