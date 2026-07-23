# SPDX-FileCopyrightText: 2026 Martin Gallagher
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""TUI tests driven through a scripted fake curses screen.

The App event loop is exercised end-to-end: polling the shared directory,
sidebar rendering with unread badges, invite accept/decline prompts,
sending/receiving messages, scrolling, mouse clicks and all keyboard
shortcuts - without needing a real terminal.
"""

import contextlib
import curses
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from phone_a_friend import tui
from phone_a_friend.store import SharedDir, Session

ENTER = 10
ESC = 27
CTRL_N, CTRL_G, CTRL_O = 14, 7, 15


class FakeScreen:
    """Scripted stand-in for a curses window.

    `keys` items may be ints, single-character strings, or callables
    (invoked at getch time, returning the key). When the script runs dry,
    getch raises KeyboardInterrupt, which cleanly ends the App loop.
    """

    def __init__(self, keys=(), rows=24, cols=80):
        self.keys = list(keys)
        self.rows, self.cols = rows, cols
        self.drawn = []
        self.fail_draws = False  # make draw calls raise curses.error

    # geometry / lifecycle -------------------------------------------------
    def getmaxyx(self):
        return (self.rows, self.cols)

    def erase(self):
        pass

    def refresh(self):
        pass

    def timeout(self, ms):
        pass

    # drawing ---------------------------------------------------------------
    def _draw(self, text=None):
        if self.fail_draws:
            raise curses.error("draw failed")
        if text is not None:
            self.drawn.append(str(text))

    def addstr(self, y, x, s, *attr):
        self.drawn.append(str(s))

    def addnstr(self, y, x, s, n, *attr):
        self._draw(s)

    def addch(self, y, x, ch, *attr):
        self._draw()

    def hline(self, y, x, ch, n):
        self._draw()

    def move(self, y, x):
        if self.fail_draws:
            raise curses.error("move failed")

    def rendered(self):
        return "\n".join(self.drawn)

    # input -----------------------------------------------------------------
    def getch(self):
        if not self.keys:
            raise KeyboardInterrupt
        k = self.keys.pop(0)
        if callable(k):
            k = k()
        if isinstance(k, str):
            return ord(k)
        return k


@contextlib.contextmanager
def fake_curses(mouse_events=(), mousemask_fails=False):
    events = list(mouse_events)

    def getmouse():
        if not events:
            raise curses.error("no mouse event")
        return events.pop(0)

    def mousemask(mask):
        if mousemask_fails:
            raise curses.error("no mouse support")
        return (mask, mask)

    with mock.patch.object(curses, "curs_set", lambda v: None), \
            mock.patch.object(curses, "mousemask", mousemask), \
            mock.patch.object(curses, "has_colors", lambda: True), \
            mock.patch.object(curses, "use_default_colors", lambda: None), \
            mock.patch.object(curses, "init_pair", lambda *a: None), \
            mock.patch.object(curses, "color_pair", lambda n: 0), \
            mock.patch.object(curses, "getmouse", getmouse), \
            mock.patch.object(curses, "ACS_VLINE", 0, create=True), \
            mock.patch.object(curses, "ACS_HLINE", 0, create=True):
        yield


class TuiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="paf-test-")
        self.shared = SharedDir(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_pair(self):
        """alice and bob, keys exchanged."""
        a = Session.register(self.shared, "alice", "pw")
        b = Session.register(self.shared, "bob", "pw")
        a.invite_contact("bob")
        fname, payload = b.list_invites()[0]
        b.accept_invite(fname, payload)
        a.process_replies()
        return a, b

    def run_app(self, session, keys, scr=None, **fc_kwargs):
        scr = scr or FakeScreen(keys)
        scr.keys = list(keys)
        app = tui.App(session)
        with fake_curses(**fc_kwargs):
            app._main(scr)
        return app, scr

    # ------------------------------------------------------------------ run
    def test_run_uses_curses_wrapper(self):
        a = Session.register(self.shared, "alice", "pw")
        scr = FakeScreen([])
        with fake_curses(), mock.patch.object(
            curses, "wrapper", lambda fn: fn(scr)
        ):
            tui.run(a)

    def test_poll_surfaces_notices_and_survives_errors(self):
        a = Session.register(self.shared, "alice", "pw")
        b = Session.register(self.shared, "bob", "pw")
        a.invite_contact("bob")
        fname, payload = b.list_invites()[0]
        b.accept_invite(fname, payload)
        # alice's TUI picks the acceptance up during its poll
        app, _ = self.run_app(a, [-1])
        self.assertIn("bob accepted", app.status)
        # a store error during polling must not crash the loop
        app2 = tui.App(a)
        with mock.patch.object(
            a, "list_invites", side_effect=tui.StoreError("boom")
        ), fake_curses():
            app2._poll()

    def test_quit_via_esc_confirm(self):
        a = Session.register(self.shared, "alice", "pw")
        # -1 poll tick, Esc -> confirm prompt: 'y' + Enter
        app, scr = self.run_app(a, [-1, ESC, "y", ENTER])
        self.assertIn("Welcome!", scr.rendered())

    def test_esc_quit_cancelled(self):
        a = Session.register(self.shared, "alice", "pw")
        # Esc -> confirm 'n': loop continues, then script dries up
        self.run_app(a, [ESC, "n", ENTER, -1])

    def test_f10_quit_and_cancel(self):
        a = Session.register(self.shared, "alice", "pw")
        # F10 -> confirm 'n': loop continues, then script dries up
        self.run_app(a, [curses.KEY_F10, "n", ENTER, -1])
        # F10 -> confirm 'y': quits before the script dries up
        self.run_app(a, [-1, curses.KEY_F10, "y", ENTER])

    def test_mousemask_unsupported(self):
        a = Session.register(self.shared, "alice", "pw")
        self.run_app(a, [-1], mousemask_fails=True)

    def test_terminal_too_small(self):
        a = Session.register(self.shared, "alice", "pw")
        scr = FakeScreen([], rows=5, cols=80)
        app, scr = self.run_app(a, [-1], scr=scr)
        self.assertIn("terminal too small", scr.rendered())

    # -------------------------------------------------------------- invites
    def test_invite_appears_and_accepts(self):
        a = Session.register(self.shared, "alice", "pw")
        b = Session.register(self.shared, "bob", "pw")
        a.invite_contact("bob")
        # Enter on invite item -> prompt -> 'y' + Enter accepts
        app, scr = self.run_app(b, [ENTER, "y", ENTER, -1])
        self.assertIn("alice", b.config["contacts"])
        self.assertIn("accepted", app.status)
        self.assertIn("✉ alice (chat)", scr.rendered())

    def test_invite_declined(self):
        a = Session.register(self.shared, "alice", "pw")
        b = Session.register(self.shared, "bob", "pw")
        a.invite_contact("bob")
        app, _ = self.run_app(b, [ENTER, "n", ENTER])
        self.assertNotIn("alice", b.config["contacts"])
        self.assertIn("declined", app.status)
        self.assertTrue(any("declined" in n for n in a.process_replies()))

    def test_invite_prompt_cancelled(self):
        a = Session.register(self.shared, "alice", "pw")
        b = Session.register(self.shared, "bob", "pw")
        a.invite_contact("bob")
        app, _ = self.run_app(b, [ENTER, ESC])
        self.assertEqual(len(b.list_invites()), 1)  # still pending

    def test_group_invite_sidebar_label_and_accept(self):
        a = Session.register(self.shared, "alice", "pw")
        b = Session.register(self.shared, "bob", "pw")
        gid = a.create_group("g")
        a.invite_group(gid, "bob")
        app, scr = self.run_app(b, [ENTER, "y", ENTER, -1])
        self.assertIn(gid, b.config["groups"])
        self.assertIn("✉ alice → g", scr.rendered())

    def test_handle_invite_error_path(self):
        b = Session.register(self.shared, "bob", "pw")
        app = tui.App(b)
        scr = FakeScreen(["y", ENTER])
        with fake_curses():
            app._handle_invite(scr, "x.json", {"type": "contact", "from": "z"})
        self.assertIn("error", app.status)

    # ------------------------------------------------------------ messaging
    def test_open_conversation_type_and_send(self):
        a, b = self.make_pair()
        a.send_message("dm", "bob", "ping from alice " + "x" * 200)  # wraps
        app, scr = self.run_app(
            b,
            [
                ENTER,                       # open chat with alice
                "h", "i", curses.KEY_BACKSPACE, "i",  # type "hi" (with a typo)
                ENTER,                       # send
                -1,
            ],
        )
        self.assertIn("ping from alice", scr.rendered())
        files = a.list_message_files("dm", "bob")
        texts = [m["text"] for m in a.load_messages("dm", "bob", files).values()]
        self.assertIn("hi", texts)
        self.assertEqual(app.unread.get("dm:alice"), 0)

    def test_unread_badge_shown(self):
        a, b = self.make_pair()
        a.send_message("dm", "bob", "unseen")
        app, scr = self.run_app(b, [-1])
        self.assertIn("●1", scr.rendered())
        self.assertEqual(app.unread["dm:alice"], 1)

    def test_scroll_and_fetch_while_scrolled(self):
        a, b = self.make_pair()
        for i in range(3):
            a.send_message("dm", "bob", f"m{i}")

        def send_late():
            a.send_message("dm", "bob", "late")
            return -1

        app, _ = self.run_app(
            b,
            [
                ENTER,              # open conversation (marks read)
                curses.KEY_PPAGE,   # scroll up
                send_late,          # new message arrives while scrolled
                -1,                 # poll fetches it, but does NOT mark read
                curses.KEY_NPAGE,   # back to bottom -> marks read
            ],
        )
        self.assertEqual(app.scroll, 0)
        self.assertEqual(b.unread_count("dm", "alice"), 0)

    def test_navigation_and_misc_keys(self):
        a, b = self.make_pair()
        gid = b.create_group("g")
        app, _ = self.run_app(
            b,
            [
                curses.KEY_RESIZE,
                curses.KEY_DOWN, curses.KEY_UP, curses.KEY_DOWN,
                "x", ESC,        # type then clear input with Esc
                1,               # unmapped control key -> ignored
                ENTER,           # open selected (group, after KEY_DOWN)
                -1,
            ],
        )
        self.assertIsNotNone(app.active)

    def test_enter_with_text_but_no_active_conversation(self):
        a, b = self.make_pair()
        # typing text without an open conversation, Enter opens selection
        app, _ = self.run_app(b, ["z", ENTER, -1])
        self.assertEqual(app.active, ("dm", "alice"))

    def test_send_error_keeps_input(self):
        b = Session.register(self.shared, "bob", "pw")
        app = tui.App(b)
        app.active = ("dm", "ghost")
        app.input = "hello"
        app._send()
        self.assertIn("error", app.status)
        self.assertEqual(app.input, "hello")

    def test_users_section_lists_strangers(self):
        a = Session.register(self.shared, "alice", "pw")
        Session.register(self.shared, "bob", "pw")
        # bob shows up under USERS on alice's side; Enter + confirm invites
        app, scr = self.run_app(a, [ENTER, "y", ENTER, -1])
        self.assertIn("USERS", scr.rendered())
        self.assertIn("+ bob", scr.rendered())
        self.assertIn("invite sent to bob", app.status)
        self.assertEqual(a.config["pending"], [{"type": "contact", "to": "bob"}])
        # once invited, bob moves from USERS to the pending list
        self.assertIn(("pend", "bob"), app.items)
        self.assertNotIn(("usr", "bob"), app.items)

    def test_users_section_confirm_declined(self):
        a = Session.register(self.shared, "alice", "pw")
        Session.register(self.shared, "bob", "pw")
        app, _ = self.run_app(a, [ENTER, "n", ENTER, -1])
        self.assertEqual(a.config["pending"], [])
        self.assertIn(("usr", "bob"), app.items)

    def test_users_section_invite_error(self):
        a = Session.register(self.shared, "alice", "pw")
        app = tui.App(a)
        app.items = [("usr", "ghost")]
        app.sel = 0
        scr = FakeScreen(["y", ENTER])
        with fake_curses():
            app._open_selected(scr)
        self.assertIn("error", app.status)

    def test_pending_item_shown_and_selected(self):
        a = Session.register(self.shared, "alice", "pw")
        Session.register(self.shared, "carol", "pw")
        a.invite_contact("carol")
        app, scr = self.run_app(a, [ENTER, -1])
        self.assertIn("carol (invited...)", scr.rendered())
        self.assertIn("waiting for carol", app.status)

    # ------------------------------------------------------------- removals
    def test_unfriend_command_closes_active_chat(self):
        a, b = self.make_pair()
        keys = [ENTER] + list("/unfriend alice") + [ENTER, -1]
        app, _ = self.run_app(b, keys)
        self.assertNotIn("alice", b.config["contacts"])
        self.assertIsNone(app.active)
        self.assertIn("unfriended alice", app.status)
        self.assertTrue(any("ended your chat" in n for n in a.process_replies()))

    def test_unfriend_unknown_user(self):
        a = Session.register(self.shared, "alice", "pw")
        app, _ = self.run_app(a, list("/unfriend ghost") + [ENTER, -1])
        self.assertIn("error", app.status)

    def test_active_chat_closes_when_peer_unfriends(self):
        a, b = self.make_pair()

        def unfriend():
            a.remove_contact("bob")
            return -1

        app, _ = self.run_app(b, [ENTER, unfriend, -1])
        self.assertIsNone(app.active)
        self.assertNotIn("alice", b.config["contacts"])

    def test_gremove_command(self):
        a, b = self.make_pair()
        gid = b.create_group("g")
        b.invite_group(gid, "alice")
        fname, payload = a.list_invites()[0]
        a.accept_invite(fname, payload)
        b.process_replies()
        # bob: move from the chat row to the group row, open it, remove alice
        keys = [curses.KEY_DOWN, ENTER] + list("/gremove alice") + [ENTER, -1]
        app, _ = self.run_app(b, keys)
        self.assertIn("removal notice for #g pushed to alice", app.status)
        self.assertTrue(any("removed you" in n for n in a.process_replies()))
        self.assertNotIn(gid, a.config["groups"])

    def test_gremove_self_leaves_group(self):
        b = Session.register(self.shared, "bob", "pw")
        gid = b.create_group("g")
        keys = [ENTER] + list("/gremove bob") + [ENTER, -1]
        app, _ = self.run_app(b, keys)
        self.assertNotIn(gid, b.config["groups"])
        self.assertIsNone(app.active)
        self.assertIn("left group #g", app.status)

    def test_gremove_needs_group(self):
        b = Session.register(self.shared, "bob", "pw")
        app, _ = self.run_app(b, list("/gremove x") + [ENTER, -1])
        self.assertIn("open a group first", app.status)

    def test_gremove_unknown_user(self):
        b = Session.register(self.shared, "bob", "pw")
        b.create_group("g")
        keys = [ENTER] + list("/gremove ghost") + [ENTER, -1]
        app, _ = self.run_app(b, keys)
        self.assertIn("error", app.status)

    # ----------------------------------------------------------------- mouse
    def test_mouse_click_opens_conversation(self):
        a, b = self.make_pair()
        # sidebar rows: 0=CHATS hdr, 1=alice, 2=GROUPS hdr
        events = [
            (0, 2, 1, 0, curses.BUTTON1_CLICKED),   # click alice -> open
            (0, 2, 0, 0, curses.BUTTON1_CLICKED),   # click header -> ignored
            (0, 2, 15, 0, curses.BUTTON1_CLICKED),  # click empty row -> ignored
            (0, 2, 1, 0, 0),                        # no button bit -> ignored
        ]
        app, _ = self.run_app(
            b,
            [curses.KEY_MOUSE, curses.KEY_MOUSE, curses.KEY_MOUSE,
             curses.KEY_MOUSE, curses.KEY_MOUSE],  # last: getmouse raises
            mouse_events=events,
        )
        self.assertEqual(app.active, ("dm", "alice"))

    # --------------------------------------------------------------- actions
    def test_ctrl_n_invite_contact(self):
        a = Session.register(self.shared, "alice", "pw")
        Session.register(self.shared, "bob", "pw")
        app, _ = self.run_app(
            a,
            [
                CTRL_N, ESC,                          # cancelled prompt
                CTRL_N, "g", "h", "o", "s", "t", ENTER,  # unknown user
                CTRL_N, "b", "o", "b", ENTER,         # success (hint shown)
                -1,
            ],
        )
        self.assertIn("invite sent to bob", app.status)
        self.assertEqual(a.config["pending"], [{"type": "contact", "to": "bob"}])

    def test_ctrl_n_no_registered_users_hint(self):
        a = Session.register(self.shared, "alice", "pw")
        app, _ = self.run_app(a, [CTRL_N, "z", ENTER])
        self.assertIn("error", app.status)

    def test_ctrl_g_create_group(self):
        a = Session.register(self.shared, "alice", "pw")
        app, _ = self.run_app(
            a,
            [
                CTRL_G, ESC,                 # cancelled
                CTRL_G, " ", ENTER,          # blank -> StoreError
                CTRL_G, "g", "1", ENTER,     # created and opened
                -1,
            ],
        )
        self.assertEqual(app.active[0], "grp")
        self.assertIn("created", app.status)
        self.assertEqual(len(a.config["groups"]), 1)

    def test_ctrl_o_invite_to_group(self):
        a = Session.register(self.shared, "alice", "pw")
        Session.register(self.shared, "bob", "pw")
        gid = a.create_group("g")
        app, _ = self.run_app(
            a,
            [
                ENTER,                              # open the group
                CTRL_O, ESC,                        # active group, cancelled
                CTRL_O, "n", "o", "p", "e", ENTER,  # unknown user -> error
                CTRL_O, "b", "o", "b", ENTER,       # success
                -1,
            ],
        )
        self.assertEqual(app.active, ("grp", gid))
        self.assertIn("group key pushed to bob", app.status)

    def test_ctrl_o_via_sidebar_selection(self):
        a = Session.register(self.shared, "alice", "pw")
        Session.register(self.shared, "bob", "pw")
        a.create_group("g")
        app = tui.App(a)
        with fake_curses():
            app._poll()  # selection snaps to the group row
            app.active = None
            scr = FakeScreen([CTRL_O, "b", "o", "b", ENTER])
            app._main(scr)
        self.assertIn("group key pushed to bob", app.status)

    def test_ctrl_o_without_group(self):
        a = Session.register(self.shared, "alice", "pw")
        app, _ = self.run_app(a, [CTRL_O, -1])
        self.assertIn("open a group first", app.status)

    # -------------------------------------------------------- slash commands
    def test_slash_commands_full_flow(self):
        a = Session.register(self.shared, "alice", "pw")
        Session.register(self.shared, "bob", "pw")
        keys = (
            list("/invite bob") + [ENTER]
            + list("/group g1") + [ENTER]
            + list("/ginvite bob") + [ENTER]
            + list("/bogus") + [ENTER]
            + [-1]
        )
        app, _ = self.run_app(a, keys)
        self.assertIn({"type": "contact", "to": "bob"}, a.config["pending"])
        self.assertEqual(app.active[0], "grp")  # /group opened the new group
        self.assertTrue(
            any(p["type"] == "group" and p["to"] == "bob"
                for p in a.config["pending"])
        )
        self.assertIn("commands:", app.status)  # unknown command shows usage

    def test_slash_quit_exits_immediately(self):
        a = Session.register(self.shared, "alice", "pw")
        scr = FakeScreen(list("/quit") + [ENTER, "X"])
        app = tui.App(a)
        with fake_curses():
            app._main(scr)
        self.assertEqual(scr.keys, ["X"])  # loop ended before the sentinel

    def test_slash_command_edge_cases(self):
        a = Session.register(self.shared, "alice", "pw")
        keys = (
            list("/invite") + [ENTER]           # missing argument -> usage
            + list("/invite ghost") + [ENTER]   # unknown user -> error
            + list("/ginvite bob") + [ENTER]    # no group open
            + [-1]
        )
        app, _ = self.run_app(a, keys)
        self.assertIn("open a group first", app.status)

    def test_function_key_aliases(self):
        a = Session.register(self.shared, "alice", "pw")
        app, _ = self.run_app(
            a,
            [curses.KEY_F2, ESC, curses.KEY_F3, ESC, curses.KEY_F4, -1],
        )
        self.assertIn("open a group first", app.status)

    # ------------------------------------------------------- direct coverage
    def test_draw_failures_are_tolerated(self):
        a, b = self.make_pair()
        a.send_message("dm", "bob", "msg")

        scr = FakeScreen([])

        def enable_fail():
            scr.fail_draws = True
            return -1

        # open the conversation, then a full draw pass with failing calls
        scr.keys = [ENTER, enable_fail, -1]
        app = tui.App(b)
        with fake_curses():
            app._main(scr)

    def test_prompt_edge_keys(self):
        a = Session.register(self.shared, "alice", "pw")
        app = tui.App(a)
        scr = FakeScreen(
            ["a", curses.KEY_BACKSPACE, curses.KEY_RESIZE, "b", ENTER]
        )
        with fake_curses():
            self.assertEqual(app._prompt(scr, "? "), "b")
        scr.fail_draws = True
        scr.keys = ["c", ENTER]
        with fake_curses():
            self.assertEqual(app._prompt(scr, "? "), "c")

    def test_sidebar_overflow_and_badge_cap(self):
        a = Session.register(self.shared, "alice", "pw")
        for i in range(30):
            a.config["contacts"][f"user{i:02d}"] = "fakekey"
        app = tui.App(a)
        with fake_curses():
            app._poll()
            app.unread["dm:user00"] = 150
            scr = FakeScreen([], rows=8, cols=80)
            app._draw_sidebar(scr, 8, 22)
        self.assertIn("●99+", scr.rendered())
        self.assertLess(len(scr.drawn), 30)

    def test_title_and_fetch_edge_cases(self):
        a = Session.register(self.shared, "alice", "pw")
        app = tui.App(a)
        self.assertEqual(app._conversation_title(), "phone_a_friend")
        app.active = ("grp", "missing")
        self.assertEqual(app._conversation_title(), "?")
        app._fetch_active("dm", "ghost")  # no key -> silently ignored
        app.items = []
        app._open_selected(None)  # nothing to open
        app._move_selection(1)    # nothing to move
        app._snap_selection(1)    # nothing to snap
        app.sel = 99
        with fake_curses():
            app._rebuild_items()  # clamps selection
        self.assertLess(app.sel, len(app.items))

    def test_open_conv_without_key_is_safe(self):
        a = Session.register(self.shared, "alice", "pw")
        app = tui.App(a)
        app._open_conv("dm", "ghost")
        self.assertEqual(app.active, ("dm", "ghost"))


if __name__ == "__main__":
    unittest.main()
