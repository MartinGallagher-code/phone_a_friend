# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher

"""Renaming and deleting groups (PAF-004)."""

import contextlib
import curses
import io
import json
import os
import sys
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from phone_a_friend import __main__ as cli  # noqa: E402
from phone_a_friend import store  # noqa: E402
from phone_a_friend.store import GROUP_GRACE, Session, StoreError  # noqa: E402
from test_tui import ENTER, TuiCase  # noqa: E402

DAY = 24 * 3600


class GroupCase(TuiCase):
    def group_of_two(self):
        """alice owns #team; bob is a member."""
        a, b = self.make_pair()
        gid = a.create_group("team")
        a.invite_group(gid, "bob")
        b.accept_invite(*b.list_invites()[0])
        a.process_replies()
        return a, b, gid

    def meta(self, gid):
        with open(os.path.join(self.shared.group_dir(gid), "meta.json")) as fh:
            return json.load(fh)


class RenameTest(GroupCase):
    def test_the_name_is_not_in_the_clear(self):
        a, _, gid = self.group_of_two()
        raw = json.dumps(self.meta(gid))
        self.assertNotIn("team", raw)
        self.assertEqual(self.meta(gid)["owner"], "alice")
        self.assertEqual(a.group_meta(gid)["name"], "team")

    def test_rename_reaches_members_on_their_next_poll(self):
        a, b, gid = self.group_of_two()
        a.rename_group(gid, "  crew ")
        self.assertEqual(a.config["groups"][gid]["name"], "crew")
        self.assertNotIn("crew", json.dumps(self.meta(gid)))
        self.assertEqual(b.config["groups"][gid]["name"], "team")
        self.assertEqual(b.sync_groups(), ["#team is now #crew"])
        self.assertEqual(b.config["groups"][gid]["name"], "crew")
        self.assertEqual(Session.login(self.shared, "bob", "pw").config["groups"][gid]["name"], "crew")
        self.assertEqual(b.sync_groups(), [])

    def test_only_the_owner(self):
        a, b, gid = self.group_of_two()
        with self.assertRaisesRegex(StoreError, "only the group's owner, alice"):
            b.rename_group(gid, "mine")
        with self.assertRaisesRegex(StoreError, "only the group's owner, alice"):
            b.delete_group(gid)
        with self.assertRaisesRegex(StoreError, "empty"):
            a.rename_group(gid, "  ")
        with self.assertRaisesRegex(StoreError, "not a member"):
            a.rename_group("nope", "x")
        self.assertEqual(a.group_meta(gid)["name"], "team")

    def test_unreadable_meta(self):
        a, b, gid = self.group_of_two()
        path = os.path.join(self.shared.group_dir(gid), "meta.json")
        good = self.meta(gid)
        for bad in ([1], {"owner": "alice"}, dict(good, name_blob="AAAA"),
                    dict(good, name_blob=7)):
            with open(path, "w") as fh:
                json.dump(bad, fh)
            self.assertIsNone(a.group_meta(gid), bad)
            with self.assertRaisesRegex(StoreError, "cannot be read"):
                a.rename_group(gid, "x")
            self.assertEqual(b.sync_groups(), [])  # left as it was
        os.unlink(path)
        self.assertEqual(b.sync_groups(), [])  # directory still there

    def test_plaintext_meta_from_before_is_encrypted_by_its_owner(self):
        a, b, gid = self.group_of_two()
        path = os.path.join(self.shared.group_dir(gid), "meta.json")
        with open(path, "w") as fh:
            json.dump({"id": gid, "name": "team", "owner": "alice",
                       "created": 1.0}, fh)
        self.assertEqual(b.sync_groups(), [])
        self.assertIn("team", json.dumps(self.meta(gid)))  # bob may not
        self.assertEqual(a.sync_groups(), [])
        self.assertNotIn("team", json.dumps(self.meta(gid)))
        self.assertEqual(b.group_meta(gid)["name"], "team")
        # and a rename of a plaintext meta encrypts it too
        with open(path, "w") as fh:
            json.dump({"id": gid, "name": "team", "owner": "alice"}, fh)
        a.rename_group(gid, "crew")
        self.assertNotIn("name", self.meta(gid))


class DeleteTest(GroupCase):
    def test_readable_until_the_grace_period_ends_then_gone(self):
        a, b, gid = self.group_of_two()
        a.send_message("grp", gid, "before")
        t0 = 1_800_000_000.0
        with mock.patch.object(store.time, "time", return_value=t0):
            until = a.delete_group(gid)
        self.assertEqual(until, t0 + GROUP_GRACE)
        self.assertEqual(store.GROUP_GRACE_DAYS, 3)
        with self.assertRaisesRegex(StoreError, "deleted"):
            a.delete_group(gid)
        with self.assertRaisesRegex(StoreError, "read-only"):
            a.send_message("grp", gid, "after")

        with mock.patch.object(store.time, "time", return_value=t0 + DAY):
            notices = b.sync_groups()
        self.assertEqual(len(notices), 1)
        self.assertIn("#team was deleted - readable until", notices[0])
        self.assertEqual(b.group_deleted_until(gid), t0 + GROUP_GRACE)
        files = b.list_message_files("grp", gid)
        self.assertEqual(
            [m["text"] for m in b.load_messages("grp", gid, files).values()],
            ["before"],
        )
        with self.assertRaisesRegex(StoreError, "read-only"):
            b.send_message("grp", gid, "after")

        with mock.patch.object(store.time, "time",
                               return_value=t0 + GROUP_GRACE + 1):
            self.assertEqual(b.sync_groups(), ["deleted group #team removed"])
        self.assertNotIn(gid, b.config["groups"])
        self.assertFalse(os.path.exists(self.shared.group_dir(gid)))
        self.assertEqual(os.listdir(os.path.join(self.tmp, "groups")), [])
        # alice's client finds it gone
        self.assertEqual(a.sync_groups(), ["group #team is gone"])
        self.assertNotIn(gid, a.config["groups"])

    def test_two_cleanups_racing(self):
        a, b, gid = self.group_of_two()
        for i in range(20):
            a.send_message("grp", gid, f"m{i}")
        start = threading.Barrier(2)
        results = []

        def clean(s):
            start.wait()
            results.append(s.cleanup_group(gid))

        threads = [threading.Thread(target=clean, args=(s,)) for s in (a, b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(os.listdir(os.path.join(self.tmp, "groups")), [])
        self.assertFalse(a.cleanup_group(gid))  # and again: nothing to do

    def test_cli(self):
        a, b, gid = self.group_of_two()

        def run(*argv, user="alice"):
            out, err = io.StringIO(), io.StringIO()
            code = None
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                    mock.patch.dict(os.environ, {"PAF_FIPS_WARNING": "0"}):
                try:
                    cli.main(["-d", self.tmp, "-u", user, "--passphrase", "pw", *argv])
                except SystemExit as exc:
                    code = exc.code
            return code, out.getvalue(), err.getvalue()

        code, out, _ = run("rename-group", "team", "crew")
        self.assertIsNone(code)
        self.assertIn("renamed 'team' to 'crew'", out)
        code, out, _ = run("status", user="bob")
        self.assertIn("* #team is now #crew", out)
        code, _, err = run("delete-group", "crew", user="bob")
        self.assertEqual(code, 1)
        self.assertIn("only the group's owner", err)
        code, out, _ = run("delete-group", "crew")
        self.assertIn("deleted 'crew' - readable until", out)
        code, _, err = run("delete-group", "nope")
        self.assertIn("unknown group", err)


class TuiGroupTest(GroupCase):
    def test_rename_and_delete_commands(self):
        a, b, gid = self.group_of_two()
        app, _ = self.run_app(a, list("/rename x") + [ENTER, -1])
        self.assertIn("open a group first, then /rename", app.status)
        app, _ = self.run_app(a, list("/rename") + [ENTER, -1])
        self.assertIn("commands:", app.status)  # /rename needs a name
        # select the group (CHATS, bob, GROUPS, #team) and open it
        open_team = [curses.KEY_DOWN, ENTER]
        app, _ = self.run_app(a, open_team + list("/rename crew") + [ENTER, -1])
        self.assertEqual(app.status, "#team is now #crew")
        app, scr = self.run_app(b, [-1])
        self.assertIn("#crew", scr.rendered())
        self.assertIn("#team is now #crew", app.status)
        # bob may not
        app, _ = self.run_app(b, open_team + list("/rename mine") + [ENTER, -1])
        self.assertIn("only the group's owner", app.status)
        app, _ = self.run_app(b, open_team + list("/delete") + [ENTER, "y", ENTER, -1])
        self.assertIn("only the group's owner", app.status)
        # alice deletes, after a "no"
        app, _ = self.run_app(a, open_team + list("/delete") + [ENTER, "n", ENTER, -1])
        self.assertIsNone(a.group_deleted_until(gid))
        app, scr = self.run_app(a, open_team + list("/delete") + [ENTER, "y", ENTER, -1])
        self.assertIn("deleted #crew - readable until", app.status)
        self.assertIn("deleted, readable until", scr.rendered())
        app, scr = self.run_app(b, [-1])
        self.assertIn("#crew (deleted)", scr.rendered())

    def test_menu_entries(self):
        a, b, gid = self.group_of_two()
        app = __import__("phone_a_friend.tui", fromlist=["App"]).App(a)
        title, entries = app._menu_entries(None, "grp", gid)
        labels = [label for label, _ in entries]
        self.assertIn("Rename (owner)", labels)
        self.assertIn("Delete (owner)", labels)
        with mock.patch.object(app, "_prompt", side_effect=["crew", "", "y"]):
            dict(entries)["Rename (owner)"]()
            self.assertEqual(a.config["groups"][gid]["name"], "crew")
            dict(entries)["Rename (owner)"]()  # empty: nothing happens
            self.assertEqual(a.config["groups"][gid]["name"], "crew")
            dict(entries)["Delete (owner)"]()
        self.assertIsNotNone(a.group_deleted_until(gid))
        self.assertTrue(app._conversation_title().startswith("phone_a_friend"))
        app.active = ("grp", gid)
        self.assertIn("readable until", app._conversation_title())


if __name__ == "__main__":
    unittest.main()
