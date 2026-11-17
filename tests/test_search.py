# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher

"""Searching every conversation (PAF-006)."""

import contextlib
import curses
import io
import os
import sys
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from phone_a_friend import __main__ as cli  # noqa: E402
from phone_a_friend import store, tui  # noqa: E402
from phone_a_friend.store import Session  # noqa: E402
from test_tui import ENTER, ESC, FakeScreen, TuiCase, fake_curses  # noqa: E402


def snapshot(root):
    """Every path under root, and every file's size and mtime."""
    out = {}
    for dirpath, dirs, files in os.walk(root):
        for name in dirs + files:
            path = os.path.join(dirpath, name)
            st = os.stat(path)
            out[path] = (st.st_size, st.st_mtime_ns)
    return out


class SearchCase(TuiCase):
    def fixtures(self):
        a, b = self.make_pair()
        gid = a.create_group("club")
        a.invite_group(gid, "bob")
        b.accept_invite(*b.list_invites()[0])
        a.process_replies()
        a.send_message("dm", "bob", "Lunch at the Pizza place?")
        b.send_message("dm", "alice", "pizza sounds good, at noon")
        a.send_message("grp", gid, "Club pizza night on Friday")
        b.send_message("grp", gid, "nothing to see here")
        a.send_message("dm", "bob", "an unrelated message")
        return a, b, gid


class StoreSearchTest(SearchCase):
    def test_exactly_the_right_hits(self):
        a, b, gid = self.fixtures()
        found = b.search(["pizza"])
        self.assertEqual([(k, t) for k, t, _ in found], [("grp", gid), ("dm", "alice")])
        texts = {t: [m["text"] for _, m in hits] for _, t, hits in found}
        self.assertEqual(texts[gid], ["Club pizza night on Friday"])
        self.assertEqual(
            texts["alice"],
            ["pizza sounds good, at noon", "Lunch at the Pizza place?"],  # newest first
        )

    def test_case_insensitive_and_all_terms(self):
        a, b, gid = self.fixtures()
        hits = b.search(["PIZZA", "at"])
        self.assertEqual(
            sorted(m["text"] for _, _, h in hits for _, m in h),
            ["Lunch at the Pizza place?", "pizza sounds good, at noon"],
        )
        self.assertEqual(b.search(["pizza", "friday"])[0][1], gid)
        self.assertEqual(b.search(["pizza", "nowhere"]), [])
        self.assertTrue(Session.matches("Straße", ["STRASSE"]))

    def test_nothing_is_written(self):
        a, b, gid = self.fixtures()
        before = snapshot(self.tmp)
        b.search(["pizza"])
        b.search(["club"])
        self.assertEqual(snapshot(self.tmp), before)

    def test_the_cache_saves_decrypting_again(self):
        a, b, gid = self.fixtures()
        cache = {}
        b.search(["pizza"], cache)
        with mock.patch.object(b, "load_messages", wraps=b.load_messages) as load:
            b.search(["noon"], cache)
            load.assert_not_called()
            a.send_message("dm", "bob", "more pizza")
            self.assertEqual(len(b.search(["more"], cache)), 1)
            self.assertEqual(load.call_count, 1)  # only the new file

    def test_a_conversation_that_cannot_be_read(self):
        a, b, gid = self.fixtures()
        with mock.patch.object(b, "list_message_files",
                               side_effect=store.StoreError("x")):
            self.assertEqual(b.search(["pizza"]), [])

    def test_five_thousand_messages(self):
        a, b, gid = self.fixtures()
        d = self.shared.group_msgs_dir(gid)
        _, key, aad = a._conv("grp", gid)
        blob = store.crypto.sym_encrypt(
            key, b'{"from": "alice", "ts": 1.0, "text": "filler text"}', aad
        )
        body = store._wrap("blob", blob)
        for i in range(5000):
            with open(os.path.join(d, f"{i:020d}-fill.json"), "wb") as fh:
                fh.write(body)
        start = time.monotonic()
        first = b.search_conversation("dm", "alice", ["pizza"], {})
        first_at = time.monotonic() - start
        found = b.search(["pizza"], {})
        total = time.monotonic() - start
        self.assertEqual(len(first), 2)
        self.assertLess(first_at, 1.0)  # stated: first results within 1 s
        self.assertLess(total, 30.0)
        self.assertEqual(len(found), 2)


class TuiSearchTest(SearchCase):
    def test_search_and_jump(self):
        a, b, gid = self.fixtures()
        keys = list("/search pizza") + [ENTER, -1, -1, curses.KEY_DOWN, ENTER, -1]
        app, scr = self.run_app(b, keys)
        self.assertEqual(app.active, ("dm", "alice"))
        newest_hit = b.search_conversation("dm", "alice", ["pizza"])[0]
        self.assertEqual(app.highlight, newest_hit[0])
        self.assertEqual(newest_hit[1]["text"], "pizza sounds good, at noon")
        shown = scr.rendered()
        self.assertIn("search: pizza - Enter opens", shown)
        self.assertIn("#club", shown)
        self.assertIn("alice: Club pizza night", shown)
        self.assertIn("searching 2/2", shown)

    def test_ctrl_f_and_no_hits_and_cancel(self):
        a, b, gid = self.fixtures()
        app, _ = self.run_app(b, [6] + list("zebra") + [ENTER, -1, -1, -1])
        self.assertEqual(app.status, "no messages contain: zebra")
        app, _ = self.run_app(b, [6, ESC, -1])  # prompt cancelled
        self.assertIn("logged in", app.status)
        app, _ = self.run_app(b, list("/search pizza") + [ENTER, ESC, -1])
        self.assertEqual(app.status, "search cancelled (0 found so far)")
        app, _ = self.run_app(b, list("/search pizza") + [ENTER, -1, -1, ESC, -1])
        self.assertIsNone(app.active)  # menu closed: nothing opened
        self.assertEqual(app.status, "3 found in 2 conversation(s)")

    def test_jump_scrolls_to_an_old_message(self):
        a, b, gid = self.fixtures()
        for i in range(60):
            a.send_message("dm", "bob", f"later {i}")
        app = tui.App(b)
        app.msg_cache = {}
        scr = FakeScreen([-1, -1, ENTER])
        with fake_curses():
            app._search(scr, "lunch")
            app._draw(scr)
        self.assertGreater(app.scroll, 0)
        self.assertIn("Lunch at the Pizza place?", scr.rendered().split("> ")[-2])

    def test_hit_line(self):
        long = "x" * 40 + " needle " + "y" * 100
        line = tui.App._hit_line({"from": "bob", "ts": 0, "text": long}, ["needle"])
        self.assertIn("...", line)
        self.assertIn("needle", line)
        app = tui.App(Session.register(self.shared, "zed", "pw"))
        self.assertEqual(app._conv_label("grp", "missing"), "#?")


class SearchCliTest(SearchCase):
    def test_cli(self):
        self.fixtures()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["-d", self.tmp, "-u", "bob", "--passphrase", "pw",
                      "search", "pizza"])
        text = out.getvalue()
        self.assertIn("#club:", text)
        self.assertIn("alice:", text)
        self.assertIn("[alice] Lunch at the Pizza place?", text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["-d", self.tmp, "-u", "bob", "--passphrase", "pw",
                      "search", "zebra", "crossing"])
        self.assertIn("no messages contain: zebra crossing", out.getvalue())


if __name__ == "__main__":
    unittest.main()
