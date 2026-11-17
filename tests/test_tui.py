# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher

"""TUI tests driven through a scripted fake curses screen.

The App event loop is exercised end-to-end: polling the shared directory,
sidebar rendering with unread badges, invite accept/decline prompts,
sending/receiving messages, scrolling, mouse clicks and all keyboard
shortcuts - without needing a real terminal.
"""

import contextlib
import curses
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from phone_a_friend import __main__ as cli
from phone_a_friend import tui
from phone_a_friend.store import SharedDir, Session

ENTER = 10
ESC = 27
CTRL_N, CTRL_G, CTRL_O = 14, 7, 15
CTRL_A, CTRL_E = 1, 5
MOUSE = curses.KEY_MOUSE

B1_PRESS = curses.BUTTON1_PRESSED
B1_RELEASE = curses.BUTTON1_RELEASED
B1_CLICK = curses.BUTTON1_CLICKED
B1_DOUBLE = curses.BUTTON1_DOUBLE_CLICKED
B3_PRESS = curses.BUTTON3_PRESSED
B3_RELEASE = curses.BUTTON3_RELEASED
B3_CLICK = curses.BUTTON3_CLICKED
WHEEL_UP = curses.BUTTON4_PRESSED


def _spare_mouse_bit():
    """A bit no curses mouse constant uses: wheel-down for a curses built
    without BUTTON5 (ncurses' first mouse version has none)."""
    used = 0
    for name in dir(curses):
        if name.startswith(("BUTTON", "REPORT_MOUSE")):
            used |= getattr(curses, name)
    bit = 1 << 20
    while bit & used:
        bit <<= 1
    return bit


WHEEL_DOWN = getattr(curses, "BUTTON5_PRESSED", None) or _spare_mouse_bit()

# Keys that are not text: none of them may ever add a character.
NOT_TEXT = [
    curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_HOME, curses.KEY_END,
    curses.KEY_IC, curses.KEY_DC, curses.KEY_F5, curses.KEY_F6,
    curses.KEY_F7, curses.KEY_F8, curses.KEY_F11, curses.KEY_F12,
    curses.KEY_RESIZE, curses.KEY_MOUSE,
]


def ev(x, y, bstate):
    """A curses.getmouse() result: (id, x, y, z, bstate)."""
    return (0, x, y, 0, bstate)


@contextlib.contextmanager
def button5(present):
    """curses with or without BUTTON5_PRESSED, the wheel's downward notch,
    whichever way this Python's curses was built."""
    had = hasattr(curses, "BUTTON5_PRESSED")
    saved = getattr(curses, "BUTTON5_PRESSED", None)
    if present:
        curses.BUTTON5_PRESSED = WHEEL_DOWN
    elif had:
        del curses.BUTTON5_PRESSED
    try:
        yield
    finally:
        if had:
            curses.BUTTON5_PRESSED = saved
        elif hasattr(curses, "BUTTON5_PRESSED"):
            del curses.BUTTON5_PRESSED


class FakeScreen:
    """Scripted stand-in for a curses window.

    `keys` items may be ints, single-character strings, or callables
    (invoked at read time, returning the key). get_wch() answers as curses
    does: a str for a character (an int below 256 is one, so 10 comes back
    as "\n"), an int for a KEY_* code, and curses.error for -1, a poll
    timeout. When the script runs dry it raises KeyboardInterrupt, which
    cleanly ends the App loop.
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
    def get_wch(self):
        if not self.keys:
            raise KeyboardInterrupt
        k = self.keys.pop(0)
        if callable(k):
            k = k()
        if k == -1:
            raise curses.error("no input")
        if isinstance(k, int) and 0 <= k < 256:
            return chr(k)
        return k


@contextlib.contextmanager
def fake_curses(mouse_events=(), mousemask_fails=False, wheel_down=True):
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
            mock.patch.object(curses, "mouseinterval", lambda ms: ms), \
            mock.patch.object(curses, "set_escdelay", lambda ms: None), \
            mock.patch.object(curses, "has_colors", lambda: True), \
            mock.patch.object(curses, "use_default_colors", lambda: None), \
            mock.patch.object(curses, "init_pair", lambda *a: None), \
            mock.patch.object(curses, "color_pair", lambda n: 0), \
            mock.patch.object(curses, "getmouse", getmouse), \
            mock.patch.object(curses, "ACS_VLINE", 0, create=True), \
            mock.patch.object(curses, "ACS_HLINE", 0, create=True), \
            button5(wheel_down):
        yield


class TuiCase(unittest.TestCase):
    """A shared directory per test, and the helpers to drive an App."""

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


class TuiTest(TuiCase):
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


# ======================================================================
# PAF-001: typing any character, editing the input line, a scrolling
# sidebar, the mouse wheel, clicks, right-click menus and help. PAF-002:
# the key bar is gone, and the key reference is on screen.
#
# Screen geometry the tests rely on (80 columns: the sidebar is 22 wide,
# the conversation text starts at column 24, typed text at column 26):
#   24 rows: sidebar rows 0-22, conversation 2-20, input line 22, status 23
#   12 rows: sidebar rows 0-10, conversation 2-8,  input line 10, status 11
# ======================================================================


class TextWidthTest(unittest.TestCase):
    def test_cells(self):
        self.assertEqual(tui.cells("ab"), 2)
        self.assertEqual(tui.cells("é"), 1)
        self.assertEqual(tui.cells("e\u0301"), 1)  # e + combining acute
        self.assertEqual(tui.cells("日本"), 4)
        self.assertEqual(tui.cells("😀"), 2)
        self.assertEqual(tui.cells("👩\u200d💻"), 4)  # the joiner takes none

    def test_clip_pad_tail(self):
        self.assertEqual(tui.clip("日本語", 5), "日本")
        self.assertEqual(tui.clip("abc", 5), "abc")
        self.assertEqual(tui.pad("日本", 5), "日本 ")
        self.assertEqual(tui.pad("abcdef", 3), "abc")
        self.assertEqual(tui.tail("abc日", 3), "c日")

    def test_wrap(self):
        self.assertEqual(tui.wrap("", 10), [""])
        self.assertEqual(tui.wrap("one two three", 7), ["one two", "three"])
        self.assertEqual(
            tui.wrap("日本語テキスト", 6), ["日本語", "テキス", "ト"]
        )
        # a double-width character wider than the pane still moves on
        self.assertEqual(tui.wrap("日本", 1), ["日", "本"])
        for line in tui.wrap("日本語 abc " * 20, 11):
            self.assertLessEqual(tui.cells(line), 11)

    def test_is_text(self):
        for ch in ("a", " ", "é", "日", "😀", "\u200d", "\u00a0"):
            self.assertTrue(tui.is_text(ch), repr(ch))
        for ch in ("\x1b", "\x7f", "\x85", "\u2028", "\u2029"):
            self.assertFalse(tui.is_text(ch), repr(ch))


class UnicodeAndKeysTest(TuiCase):
    TEXT = "é ß 日本 😀 👩\u200d💻"

    def test_typed_unicode_reaches_the_peer_unchanged(self):
        a, b = self.make_pair()
        app, scr = self.run_app(b, [ENTER] + list(self.TEXT) + [ENTER, -1])
        files = a.list_message_files("dm", "bob")
        texts = [m["text"] for m in a.load_messages("dm", "bob", files).values()]
        self.assertEqual(texts, [self.TEXT])
        self.assertIn(self.TEXT, scr.rendered())
        # and `paf read` prints the same string
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["-d", self.tmp, "-u", "alice", "--passphrase", "pw",
                      "read", "--to", "bob"])
        self.assertIn(self.TEXT, out.getvalue())

    def test_unicode_in_prompts(self):
        a = Session.register(self.shared, "alice", "pw")
        self.run_app(a, [CTRL_G] + list("日本 😀 club") + [ENTER, -1])
        names = [g["name"] for g in a.config["groups"].values()]
        self.assertEqual(names, ["日本 😀 club"])
        # the F2 and F4 prompts are the same prompt
        app = tui.App(a)
        scr = FakeScreen(list("é日😀") + [ENTER])
        with fake_curses():
            self.assertEqual(app._prompt(scr, "? "), "é日😀")

    def test_keys_that_are_not_text_type_nothing(self):
        a = Session.register(self.shared, "alice", "pw")
        keys = (
            list("ab") + NOT_TEXT + ["\x85", "\u2028"]
            + [curses.KEY_F1, ESC]                 # help, closed
            + [curses.KEY_F9]                      # nothing to make a menu for
            + [curses.KEY_F10, "n", ENTER]         # quit, declined
            + [curses.KEY_END, -1]
        )
        app, _ = self.run_app(a, keys)
        self.assertEqual(app.input, "ab")
        # nor in a prompt
        scr = FakeScreen(
            ["x"] + NOT_TEXT
            + [curses.KEY_F1, curses.KEY_F9, curses.KEY_F10, "\x85", "\u2028",
               -1, ENTER]
        )
        with fake_curses():
            self.assertEqual(tui.App(a)._prompt(scr, "? "), "x")

    def test_read_key(self):
        scr = FakeScreen(["é", ENTER, "\x7f", CTRL_N, curses.KEY_F2, -1])
        read = tui.App._read_key
        self.assertEqual(read(scr), "é")
        self.assertEqual(read(scr), 10)
        self.assertEqual(read(scr), 127)
        self.assertEqual(read(scr), 14)
        self.assertEqual(read(scr), curses.KEY_F2)
        self.assertIsNone(read(scr))  # the poll timed out

    def test_editing_the_input_line(self):
        a = Session.register(self.shared, "alice", "pw")
        app = tui.App(a)

        def press(*keys):
            for k in keys:
                app._handle_key(None, k)
            return app.input, app.cursor

        self.assertEqual(press(*"acd"), ("acd", 3))
        self.assertEqual(press(curses.KEY_LEFT, curses.KEY_LEFT, "b"),
                         ("abcd", 2))
        self.assertEqual(press(curses.KEY_HOME, curses.KEY_DC), ("bcd", 0))
        self.assertEqual(press(curses.KEY_END, curses.KEY_BACKSPACE),
                         ("bc", 2))
        self.assertEqual(press(CTRL_A, "x"), ("xbc", 1))
        self.assertEqual(press(CTRL_E, "y"), ("xbcy", 4))
        # nothing before the start, nothing after the end
        self.assertEqual(press(curses.KEY_HOME, 127, curses.KEY_LEFT),
                         ("xbcy", 0))
        self.assertEqual(press(curses.KEY_END, curses.KEY_RIGHT, curses.KEY_DC),
                         ("xbcy", 4))
        # Backspace and Delete in the middle
        self.assertEqual(press(curses.KEY_LEFT, curses.KEY_LEFT, 8),
                         ("xcy", 1))
        self.assertEqual(press(curses.KEY_DC), ("xy", 1))

    def test_esc_clears_the_line_and_the_cursor(self):
        a = Session.register(self.shared, "alice", "pw")
        app, _ = self.run_app(a, list("abc") + [curses.KEY_LEFT, ESC, "z", -1])
        self.assertEqual((app.input, app.cursor), ("z", 1))

    def test_a_long_line_scrolls_sideways(self):
        a = Session.register(self.shared, "alice", "pw")
        scr = FakeScreen([], rows=24, cols=40)  # 13 columns for the text
        x0, right = 26, 24 + 15 - 1             # text start, last column
        app = tui.App(a)
        with fake_curses():
            app._poll()
            for ch in "x" * 30:
                app._handle_key(scr, ch)
            app._draw(scr)
            self.assertEqual(app._cursor_at, (22, x0 + 12))
            self.assertGreater(app.hscroll, 0)
            app._handle_key(scr, curses.KEY_HOME)
            app._draw(scr)
            self.assertEqual((app.hscroll, app._cursor_at), (0, (22, x0)))
            # double-width characters: the cursor still stays in the pane
            app._handle_key(scr, ESC)
            for ch in "日" * 20:
                app._handle_key(scr, ch)
            app._draw(scr)
            _, col = app._cursor_at
            self.assertTrue(x0 <= col <= right, col)
            shown, cursor_x = app._input_view(13)
            self.assertLessEqual(tui.cells(shown), 13)
            self.assertEqual(cursor_x, col - x0)
            # once the line fits again, it is shown from its start
            for _ in range(17):
                app._handle_key(scr, curses.KEY_BACKSPACE)
            app._draw(scr)
            self.assertEqual(app.hscroll, 0)

    def test_escdelay_and_double_click_interval(self):
        a = Session.register(self.shared, "alice", "pw")
        esc, clicks = mock.Mock(), mock.Mock()
        with mock.patch.dict(os.environ):
            os.environ.pop("ESCDELAY", None)
            with fake_curses(), \
                    mock.patch.object(curses, "set_escdelay", esc), \
                    mock.patch.object(curses, "mouseinterval", clicks):
                tui.App(a)._main(FakeScreen([-1]))
            esc.assert_called_once_with(25)
            clicks.assert_called_once_with(200)
            # a curses that refuses is no reason to stop
            with fake_curses(), mock.patch.object(
                curses, "set_escdelay", side_effect=curses.error("no")
            ):
                tui.App(a)._main(FakeScreen([-1]))
        # an ESCDELAY of the user's own is left alone
        esc.reset_mock()
        with mock.patch.dict(os.environ, {"ESCDELAY": "100"}), fake_curses(), \
                mock.patch.object(curses, "set_escdelay", esc):
            tui.App(a)._main(FakeScreen([-1]))
        esc.assert_not_called()


class SidebarScrollTest(TuiCase):
    def crowd(self, n=30):
        """alice and n other users: 33 sidebar rows (CHATS, GROUPS, USERS
        and the n users), indexes 3 to n + 2 being the users."""
        a = Session.register(self.shared, "alice", "pw")
        for i in range(n):
            Session.register(self.shared, f"user{i:02d}", "pw")
        return a

    def test_every_row_can_be_reached_and_is_drawn(self):
        a = self.crowd()
        app = tui.App(a)
        scr = FakeScreen([], rows=12, cols=80)
        seen = []

        def down():
            # the selection after each draw must be a row on screen
            self.assertIn(app.sel, app._row_map.values())
            seen.append(app.sel)
            return curses.KEY_DOWN

        first = []
        scr.keys = [lambda: first.append(list(scr.drawn)) or -1] + [down] * 30
        with fake_curses():
            app._main(scr)
        self.assertEqual(sorted(seen), list(range(3, 33)))
        self.assertIn("▼", "\n".join(first[0]))
        self.assertNotIn("▲", "\n".join(first[0]))
        self.assertIn(" + user29", scr.rendered())
        self.assertIn("▲", scr.rendered())
        # Down from the last row wraps to the first, and its heading shows
        self.assertEqual(app.sel, 3)
        self.assertEqual(app.top, 2)

    def test_click_after_scrolling_selects_the_row_drawn_there(self):
        a = self.crowd()
        scr = FakeScreen([], rows=12, cols=80)
        # Up from the first user wraps to the last: the list scrolls down
        app, _ = self.run_app(
            a, [curses.KEY_UP, MOUSE, -1], scr=scr,
            mouse_events=[ev(3, 4, B1_CLICK)],
        )
        # 33 rows, 11 on screen: the last screenful starts at row 22
        self.assertEqual(app.top, 22)
        self.assertEqual(app.items[app.sel], ("usr", "user23"))
        self.assertEqual(a.config["pending"], [])  # a click only selects

    def test_wheel_scrolls_the_sidebar(self):
        a = self.crowd()
        app = tui.App(a)
        scr = FakeScreen([], rows=12, cols=80)
        states = []

        def note():
            states.append((app.top, app.sel))
            return -1

        scr.keys = [MOUSE, note, MOUSE, note, MOUSE, note]
        events = [
            ev(3, 2, WHEEL_DOWN),    # the selection stays on screen
            ev(22, 2, WHEEL_DOWN),   # the border is the sidebar's too
            ev(3, 2, WHEEL_UP),
        ]
        with fake_curses(mouse_events=events):
            app._main(scr)
        self.assertEqual(states[0], (2, 3))   # USERS heading kept in view
        self.assertEqual(states[1], (5, 5))   # the selection came along
        self.assertEqual(states[2], (2, 5))
        # from the last row (Up wraps to it), the wheel up takes the
        # selection to the lowest row still shown
        app = tui.App(a)
        states.clear()
        scr.keys = [curses.KEY_UP, -1, MOUSE, note]
        with fake_curses(mouse_events=[ev(3, 2, WHEEL_UP)]):
            app._main(scr)
        self.assertEqual(states, [(19, 29)])


class WheelTest(TuiCase):
    def test_wheel_scrolls_the_conversation_and_marks_it_read(self):
        a, b = self.make_pair()
        for i in range(30):
            a.send_message("dm", "bob", f"m{i}")
        app = tui.App(b)
        scr = FakeScreen([])
        checks = []

        def late():
            a.send_message("dm", "bob", "late")
            return -1

        def check():
            checks.append((app.scroll, b.unread_count("dm", "alice")))
            return -1

        scr.keys = [ENTER, MOUSE, MOUSE, late, check, MOUSE, check, MOUSE,
                    check]
        events = [
            ev(40, 5, WHEEL_UP),
            ev(40, 5, WHEEL_UP),
            ev(40, 22, WHEEL_DOWN),  # over the input line, too
            ev(40, 5, WHEEL_DOWN),
        ]
        with fake_curses(mouse_events=events):
            app._main(scr)
        self.assertEqual(checks, [(6, 1), (3, 1), (0, 0)])

    def test_wheel_elsewhere_does_nothing(self):
        a, b = self.make_pair()
        events = [ev(5, 23, WHEEL_UP), ev(40, 23, WHEEL_UP)]  # status line
        app, _ = self.run_app(b, [ENTER, MOUSE, MOUSE, -1],
                              mouse_events=events)
        self.assertEqual(app.scroll, 0)

    def test_a_curses_without_button5(self):
        a, b = self.make_pair()
        for i in range(30):
            a.send_message("dm", "bob", f"m{i}")
        # the other notch's bit then means nothing, and nothing fails
        events = [ev(40, 5, WHEEL_UP), ev(40, 5, WHEEL_DOWN)]
        app, _ = self.run_app(b, [ENTER, MOUSE, MOUSE, -1],
                              mouse_events=events, wheel_down=False)
        self.assertEqual(app.scroll, 3)


class ClickTest(TuiCase):
    def sidebar_of_every_kind(self):
        """bob's sidebar: an invite (row 1), a chat (3), a group (5) and a
        user under USERS (8)."""
        a, b = self.make_pair()
        b.create_group("g")
        carol = Session.register(self.shared, "carol", "pw")
        carol.invite_contact("bob")
        Session.register(self.shared, "dave", "pw")
        return b

    def test_each_kind_of_row_acts_once_or_not_at_all(self):
        b = self.sidebar_of_every_kind()
        patterns = {
            "click": [B1_CLICK],
            "double": [B1_DOUBLE],
            "press, release": [B1_PRESS, B1_RELEASE],
        }
        expected = {  # row: (kind, acts on click, double, press + release)
            1: ("inv", 0, 1, 0),
            3: ("dm", 1, 1, 1),
            5: ("grp", 1, 1, 1),
            8: ("usr", 0, 1, 0),
        }
        for y, (kind, *counts) in expected.items():
            for (name, bits), count in zip(patterns.items(), counts):
                with self.subTest(row=kind, mouse=name), mock.patch.object(
                    tui.App, "_open_selected", autospec=True
                ) as acted:
                    app, _ = self.run_app(
                        b, [MOUSE] * len(bits) + [-1],
                        mouse_events=[ev(3, y, bit) for bit in bits],
                    )
                    self.assertEqual(acted.call_count, count)
                    self.assertEqual(app.items[app.sel][0], kind)

    def test_double_click_answers_an_invite(self):
        b = self.sidebar_of_every_kind()
        self.run_app(b, [MOUSE, "y", ENTER, -1],
                     mouse_events=[ev(3, 1, B1_DOUBLE)])
        self.assertIn("carol", b.config["contacts"])

    def test_a_release_while_a_prompt_is_open_types_nothing(self):
        a = Session.register(self.shared, "alice", "pw")
        Session.register(self.shared, "bob", "pw")
        # alice: CHATS, GROUPS, USERS, bob (row 3)
        events = [ev(3, 3, B1_DOUBLE), ev(3, 3, B1_RELEASE)]
        app, _ = self.run_app(a, [MOUSE, MOUSE, "y", ENTER, -1],
                              mouse_events=events)
        self.assertEqual(a.config["pending"], [{"type": "contact", "to": "bob"}])

    def test_clicks_that_do_nothing(self):
        a, b = self.make_pair()
        events = [
            ev(3, 0, B1_CLICK),    # a heading
            ev(3, 0, B3_CLICK),    # a heading, right button
            ev(3, 15, B1_CLICK),   # below the last row
            ev(40, 5, B1_CLICK),   # the conversation
            ev(40, 5, B3_CLICK),
            ev(5, 23, B1_CLICK),   # the status line
            ev(40, 23, B1_CLICK),
            ev(3, 1, 0),           # no button
        ]
        app, _ = self.run_app(b, [MOUSE] * len(events) + [-1],
                              mouse_events=events)
        self.assertIsNone(app.active)
        # nothing is drawn on a terminal too small to use, so nothing acts
        scr = FakeScreen([], rows=5, cols=80)
        app, _ = self.run_app(b, [MOUSE, -1], scr=scr,
                              mouse_events=[ev(3, 1, B1_CLICK)])
        self.assertIsNone(app.active)

    def test_click_in_the_input_line_moves_the_cursor(self):
        a = Session.register(self.shared, "alice", "pw")
        app = tui.App(a)
        scr = FakeScreen([])
        cursors = []

        def note():
            cursors.append(app.cursor)
            return -1

        scr.keys = (list("abcdef") + [MOUSE, note, MOUSE, note, MOUSE, note]
                    + [ESC] + list("日本語") + [MOUSE, note])
        events = [
            ev(26 + 3, 22, B1_CLICK),
            ev(26 + 3, 22, 0),          # no button: nothing
            ev(26 + 40, 22, B1_PRESS),  # past the end
            ev(26 + 3, 22, B1_RELEASE),  # inside the second, wide, character
        ]
        with fake_curses(mouse_events=events):
            app._main(scr)
        self.assertEqual(cursors, [3, 3, 6, 1])


class MenuTest(TuiCase):
    # a right-click on bob's chat with alice (row 1) opens a three-item menu
    # at (1, 3): its items on rows 2, 3 and 4, from column 4
    def test_chat_menu_items(self):
        a, b = self.make_pair()
        app, scr = self.run_app(b, [MOUSE, ENTER, -1],
                                mouse_events=[ev(3, 1, B3_CLICK)])
        self.assertEqual(app.active, ("dm", "alice"))  # Open
        for label in ("Open", "Unfriend", "Invite to a group"):
            self.assertIn(label, scr.rendered())

        with mock.patch.object(b, "remove_contact",
                               wraps=b.remove_contact) as remove:
            self.run_app(b, [MOUSE, curses.KEY_DOWN, ENTER, "n", ENTER, -1],
                         mouse_events=[ev(3, 1, B3_CLICK)])
            remove.assert_not_called()
            self.run_app(b, [MOUSE, curses.KEY_DOWN, ENTER, "y", ENTER, -1],
                         mouse_events=[ev(3, 1, B3_CLICK)])
            remove.assert_called_once_with("alice")

    def test_chat_menu_invite_to_a_group(self):
        a, b = self.make_pair()
        # no groups yet: Up wraps to the last item
        app, _ = self.run_app(b, [MOUSE, curses.KEY_UP, ENTER, -1],
                              mouse_events=[ev(3, 1, B3_CLICK)])
        self.assertIn("no groups yet", app.status)
        # with groups, a menu of them; Esc there does nothing
        gids = {b.create_group(f"g{i:02d}"): f"g{i:02d}" for i in range(15)}
        by_name = {name: gid for gid, name in gids.items()}
        scr = FakeScreen([], rows=12, cols=80)
        with mock.patch.object(b, "invite_group", wraps=b.invite_group) as inv:
            self.run_app(b, [MOUSE, curses.KEY_UP, ENTER, ESC, -1], scr=scr,
                         mouse_events=[ev(3, 1, B3_CLICK)])
            inv.assert_not_called()
            # a menu longer than the screen scrolls, and shows that it does
            keys = [MOUSE, curses.KEY_UP, ENTER] + [curses.KEY_DOWN] * 10
            app, scr = self.run_app(b, keys + [ENTER, -1], scr=scr,
                                    mouse_events=[ev(3, 1, B3_CLICK)])
            inv.assert_called_once_with(by_name["g10"], "alice")
        self.assertIn("▲", scr.rendered())
        self.assertIn("group key pushed to alice for #g10", app.status)

    def test_group_menu_items(self):
        a, b = self.make_pair()
        g1 = b.create_group("g1")
        g2 = b.create_group("g2")
        # bob: CHATS, alice, GROUPS, g1 (row 3), g2 (row 4)
        right_g2 = ev(3, 4, B3_CLICK)
        app, _ = self.run_app(b, [MOUSE, ENTER, -1], mouse_events=[right_g2])
        self.assertEqual(app.active, ("grp", g2))  # Open

        with mock.patch.object(b, "invite_group", wraps=b.invite_group) as inv:
            # with g1 open, "Invite a user" on g2 invites to g2
            keys = [curses.KEY_DOWN, ENTER, MOUSE, curses.KEY_DOWN, ENTER]
            self.run_app(b, keys + ["a", "l", "i", "c", "e", ENTER, -1],
                         mouse_events=[right_g2])
            inv.assert_called_once_with(g2, "alice")
            self.run_app(b, [MOUSE, curses.KEY_DOWN, ENTER, ESC, -1],
                         mouse_events=[right_g2])
            self.assertEqual(inv.call_count, 1)  # cancelled

        with mock.patch.object(b, "remove_group_member",
                               wraps=b.remove_group_member) as rm:
            down2 = [curses.KEY_DOWN] * 2
            self.run_app(b, [MOUSE] + down2 + [ENTER, ESC, -1],
                         mouse_events=[right_g2])
            rm.assert_not_called()
            self.run_app(b, [MOUSE] + down2 + [ENTER] + list("alice")
                         + [ENTER, -1], mouse_events=[right_g2])
            rm.assert_called_once_with(g2, "alice")
            # Leave: asks first
            self.run_app(b, [MOUSE, curses.KEY_UP, ENTER, "n", ENTER, -1],
                         mouse_events=[right_g2])
            self.assertIn(g2, b.config["groups"])
            app, _ = self.run_app(
                b, [MOUSE, curses.KEY_UP, ENTER, "y", ENTER, -1],
                mouse_events=[right_g2],
            )
            rm.assert_called_with(g2, "bob")
        self.assertNotIn(g2, b.config["groups"])
        self.assertIn("left group #g2", app.status)
        self.assertIn(g1, b.config["groups"])

    def test_invite_menu(self):
        a = Session.register(self.shared, "alice", "pw")
        b = Session.register(self.shared, "bob", "pw")
        a.invite_contact("bob")
        # bob: INVITES, the invite (row 1)
        with mock.patch.object(b, "decline_invite",
                               wraps=b.decline_invite) as decline:
            self.run_app(b, [MOUSE, ESC, -1], mouse_events=[ev(3, 1, B3_CLICK)])
            app, scr = self.run_app(b, [MOUSE, curses.KEY_DOWN, ENTER, -1],
                                    mouse_events=[ev(3, 1, B3_CLICK)])
            self.assertEqual(decline.call_count, 1)
        self.assertIn("invite declined", app.status)
        self.assertIn("Accept", scr.rendered())
        a.process_replies()  # alice hears of the decline, and asks again
        a.invite_contact("bob")
        with mock.patch.object(b, "accept_invite",
                               wraps=b.accept_invite) as accept:
            self.run_app(b, [MOUSE, ENTER, -1],
                         mouse_events=[ev(3, 1, B3_CLICK)])
            self.assertEqual(accept.call_count, 1)
        self.assertIn("alice", b.config["contacts"])
        # a decline that fails says so
        app = tui.App(b)
        app._decline_invite("x.json", {"type": "contact", "from": "z"})
        self.assertIn("error", app.status)

    def test_user_and_pending_menus(self):
        a = Session.register(self.shared, "alice", "pw")
        Session.register(self.shared, "bob", "pw")
        # alice: CHATS, GROUPS, USERS, bob (row 3)
        with mock.patch.object(a, "invite_contact",
                               wraps=a.invite_contact) as invite:
            app, _ = self.run_app(a, [MOUSE, ENTER, -1],
                                  mouse_events=[ev(3, 3, B3_CLICK)])
            invite.assert_called_once_with("bob")
        # bob is now pending (row 1): no menu, just where it stands
        app, _ = self.run_app(a, [MOUSE, -1], mouse_events=[ev(3, 1, B3_CLICK)])
        self.assertIn("waiting for bob", app.status)

    def test_menu_from_the_keyboard(self):
        a, b = self.make_pair()
        app, _ = self.run_app(b, [curses.KEY_F9, ENTER, -1])
        self.assertEqual(app.active, ("dm", "alice"))
        app, _ = self.run_app(b, list("/menu") + [ENTER, ENTER, -1])
        self.assertEqual(app.active, ("dm", "alice"))
        # with nothing to select, there is nothing to make a menu for
        c = Session.register(self.shared, "carol", "pw")
        self.run_app(c, [curses.KEY_DOWN, -1])  # (carol sees alice, bob)
        lone = SharedDir(tempfile.mkdtemp(prefix="paf-test-"))
        self.addCleanup(shutil.rmtree, lone.root, True)
        d = Session.register(lone, "dave", "pw")
        app, _ = self.run_app(d, [curses.KEY_F9, -1])
        self.assertIn("nothing selected", app.status)
        app = tui.App(d)
        app._context_menu(None)  # before the first poll: no rows at all
        self.assertIn("nothing selected", app.status)

    def test_menu_closes_without_acting(self):
        a, b = self.make_pair()
        right = ev(3, 1, B3_CLICK)
        cases = {
            "Esc": ([MOUSE, ESC], [right]),
            "a click outside": ([MOUSE, MOUSE], [right, ev(70, 20, B1_CLICK)]),
            "a click on its frame, then Esc": (
                [MOUSE, MOUSE, ESC], [right, ev(3, 1, B1_CLICK)]),
            "the right button's press, then its release": (
                [MOUSE, MOUSE, ESC], [ev(3, 1, B3_PRESS), ev(3, 1, B3_RELEASE)]),
            "a left press, the wheel, nothing": (
                [MOUSE, MOUSE, MOUSE, MOUSE, ESC],
                [right, ev(5, 3, B1_PRESS), ev(5, 3, WHEEL_UP)]),
            "a resize, a key it does not use": (
                [MOUSE, curses.KEY_RESIZE, "x", -1, ESC], [right]),
        }
        for name, (keys, events) in cases.items():
            with self.subTest(name):
                app, _ = self.run_app(b, keys + [-1], mouse_events=events)
                self.assertIsNone(app.active)
                self.assertIn("alice", b.config["contacts"])

    def test_click_on_a_menu_item_chooses_it(self):
        a, b = self.make_pair()
        # item 0, "Open", is on row 2; a press there only waits for the click
        events = [ev(3, 1, B3_CLICK), ev(5, 2, B1_PRESS), ev(5, 2, B1_RELEASE)]
        app, _ = self.run_app(b, [MOUSE, MOUSE, MOUSE, -1],
                              mouse_events=events)
        self.assertEqual(app.active, ("dm", "alice"))


class HelpAndKeyReferenceTest(TuiCase):
    LAST_HELP_LINE = "Esc, Enter or a click closes this."

    def test_help_opens_and_closes(self):
        a = Session.register(self.shared, "alice", "pw")
        closers = {
            "Esc": [ESC], "Enter": [ENTER], "q": ["q"], "F1": [curses.KEY_F1],
            "a click": [MOUSE],
        }
        for name, keys in closers.items():
            for opener in ([curses.KEY_F1], list("/help") + [ENTER]):
                with self.subTest(close=name, open=opener[0]):
                    app, scr = self.run_app(
                        a, opener + keys + [-1],
                        mouse_events=[ev(70, 20, B1_CLICK)],
                    )
                    self.assertIn("double-click", scr.rendered())
                    self.assertIn("right-click", scr.rendered())

    def test_help_scrolls(self):
        a = Session.register(self.shared, "alice", "pw")
        # 32 lines in a 20-line box: the last one shows only once scrolled
        app, scr = self.run_app(a, [curses.KEY_F1, ESC, -1])
        self.assertNotIn(self.LAST_HELP_LINE, scr.rendered())
        self.assertIn("▼", scr.rendered())
        keys = [curses.KEY_F1, curses.KEY_DOWN, curses.KEY_UP,
                curses.KEY_NPAGE, curses.KEY_PPAGE, MOUSE, MOUSE,
                curses.KEY_NPAGE, curses.KEY_RESIZE, MOUSE, MOUSE, MOUSE,
                ESC, -1]
        events = [ev(40, 10, WHEEL_DOWN), ev(40, 10, WHEEL_UP),
                  ev(40, 10, B1_PRESS), ev(40, 10, B3_RELEASE)]
        app, scr = self.run_app(a, keys, mouse_events=events)
        self.assertIn(self.LAST_HELP_LINE, scr.rendered())
        self.assertIn("▲", scr.rendered())

    def test_help_survives_failing_draws(self):
        a = Session.register(self.shared, "alice", "pw")
        scr = FakeScreen([])

        def fail():
            scr.fail_draws = True
            return curses.KEY_F1

        self.run_app(a, [fail, ESC, -1], scr=scr)

    def test_clicks_on_the_bottom_row_do_nothing(self):
        # The bottom row was the key bar (PAF-001); it is the status line now
        # (PAF-002), and no column of it runs an action.
        a, b = self.make_pair()
        for rows in (24, 12):
            with self.subTest(rows=rows):
                events = [ev(x, rows - 1, B1_CLICK) for x in range(80)]
                events += [ev(x, rows - 1, B1_DOUBLE) for x in (3, 40, 78)]
                scr = FakeScreen([MOUSE] * len(events) + [-1], rows=rows)
                app = tui.App(b)
                with mock.patch.object(tui.App, "_do", autospec=True) as do, \
                        mock.patch.object(tui.App, "_open_selected",
                                          autospec=True) as opened, \
                        fake_curses(mouse_events=events):
                    app._main(scr)
                self.assertEqual(do.call_count, 0)
                self.assertEqual(opened.call_count, 0)
                self.assertIsNone(app.active)
                self.assertEqual((app.input, app.cursor), ("", 0))

    def test_the_status_line_is_the_last_row(self):
        a = Session.register(self.shared, "alice", "pw")
        for rows in (24, 12):
            with self.subTest(rows=rows):
                drawn = []

                class Screen(FakeScreen):
                    def addnstr(self, y, x, s, n, *attr):
                        drawn.append((y, x, s))
                        super().addnstr(y, x, s, n, *attr)

                with fake_curses():
                    tui.App(a)._main(Screen([-1], rows=rows))
                # drawn once per frame, always on the last row
                status = [(y, x, s) for y, x, s in drawn
                          if "logged in as alice" in s]
                self.assertEqual({(y, x) for y, x, _ in status},
                                 {(rows - 1, 0)})
                line = status[0][2]
                self.assertEqual(tui.cells(line), 79)
                self.assertTrue(line.endswith(" /help: keys "), line)
                self.assertFalse(any(y >= rows for y, _, _ in drawn))

    def test_a_prompt_is_on_the_bottom_row(self):
        a = Session.register(self.shared, "alice", "pw")
        drawn = []

        class Screen(FakeScreen):
            def addnstr(self, y, x, s, n, *attr):
                drawn.append((y, s))
                super().addnstr(y, x, s, n, *attr)

        with fake_curses():
            tui.App(a)._main(Screen([CTRL_G, ESC, -1]))
        self.assertEqual({y for y, s in drawn if "new group name:" in s}, {23})

    def test_nothing_on_screen_names_a_function_key(self):
        fkey = re.compile(r"\bF[0-9]+\b")
        for name in ("HELP", "HELP_HINT", "KEY_REFERENCE", "HELP_LINES",
                     "WELCOME"):
            text = getattr(tui, name)
            text = text if isinstance(text, str) else "\n".join(text)
            with self.subTest(constant=name):
                self.assertIsNone(fkey.search(text), text)
        a, b = self.make_pair()
        b.create_group("g")
        screens = {
            "start-up": [-1],
            "a conversation": [ENTER, -1],
            "the help box": [ENTER] + list("/help") + [ENTER, ESC, -1],
            "the menu": [ENTER, curses.KEY_F9, ESC, -1],
            "a missing group": [CTRL_O, -1],
            "a new group": list("/group h") + [ENTER, -1],
        }
        for name, keys in screens.items():
            with self.subTest(screen=name):
                _, scr = self.run_app(b, keys)
                self.assertIsNone(fkey.search(scr.rendered()), scr.rendered())
        app = tui.App(b)
        with fake_curses():
            app._action_group_for(FakeScreen([ESC]), "alice")  # a menu
        b.config["groups"].clear()
        with fake_curses():
            app._action_group_for(FakeScreen([]), "alice")
        self.assertIsNone(fkey.search(app.status), app.status)

    def test_the_key_reference_matches_the_code(self):
        ref = "\n".join(tui.KEY_REFERENCE)
        # every command /help's usage line names, and nothing else
        usage = {w for w in tui.App.USAGE.split() if w.startswith("/")}
        listed = {ln.split()[0] for ln in tui.KEY_REFERENCE
                  if ln.startswith("  /")}
        self.assertEqual(listed, usage)
        # the Ctrl keys it names are bound to what it says they do
        for code, name, action in ((CTRL_N, "Ctrl-N", "invite"),
                                   (CTRL_G, "Ctrl-G", "group"),
                                   (CTRL_O, "Ctrl-O", "ginvite")):
            self.assertIn(name, ref)
            self.assertEqual(tui.KEY_ACTIONS[code], action)
        # the reference is /help's text and the welcome pane's
        self.assertEqual(tui.HELP_LINES[:len(tui.KEY_REFERENCE)],
                         tui.KEY_REFERENCE)
        self.assertEqual(tui.WELCOME[-len(tui.KEY_REFERENCE):],
                         tui.KEY_REFERENCE)
        # and it fits the welcome pane of an 80-column screen, with a
        # column to spare for the scroll marks
        for line in tui.WELCOME:
            self.assertLessEqual(tui.cells(line), 54, line)

    def test_the_welcome_pane_is_the_reference_and_scrolls(self):
        a = Session.register(self.shared, "alice", "pw")
        app = tui.App(a)
        scr = FakeScreen([], rows=12, cols=80)  # 7 rows of it at a time
        last = tui.WELCOME[-1]
        seen = []

        def look():
            # the last frame drawn: each ends with the status line
            ends = [i for i, d in enumerate(scr.drawn) if "/help: keys" in d]
            frame = scr.drawn[ends[-2] + 1:] if len(ends) > 1 else scr.drawn
            seen.append((app.welcome_top, list(frame)))
            scr.drawn.clear()
            return -1

        scr.keys = ([look] + [curses.KEY_NPAGE] * 9 + [look]
                    + [curses.KEY_PPAGE, look]
                    + [MOUSE, look, MOUSE, look, MOUSE, look])
        events = [ev(40, 5, WHEEL_UP), ev(40, 10, WHEEL_DOWN),  # the input line
                  ev(5, 5, WHEEL_DOWN)]                         # the sidebar
        with fake_curses(mouse_events=events):
            app._main(scr)
        tops = [top for top, _ in seen]
        end = len(tui.WELCOME) - 7
        self.assertEqual(tops, [0, end, end - 5, end - 8, end - 5, end - 5])
        first, at_end = seen[0][1], seen[1][1]
        self.assertIn(tui.WELCOME[0], first)
        self.assertIn("▼", first)
        self.assertNotIn("▲", first)
        self.assertNotIn(last, first)
        self.assertIn(last, at_end)
        self.assertIn("▲", at_end)
        self.assertNotIn("▼", at_end)
        # opening a conversation shows the conversation, not the reference
        b = Session.register(self.shared, "bob", "pw")
        a.invite_contact("bob")
        fname, payload = b.list_invites()[0]
        b.accept_invite(fname, payload)
        a.process_replies()
        _, scr = self.run_app(a, [ENTER, -1])
        ends = [i for i, d in enumerate(scr.drawn) if "/help: keys" in d]
        frame = scr.drawn[ends[-2] + 1:]
        self.assertIn("chat with bob", frame)
        self.assertNotIn(tui.WELCOME[0], frame)

    def test_the_smallest_screen(self):
        a = Session.register(self.shared, "alice", "pw")
        rows, cols = tui.MIN_ROWS, tui.MIN_COLS
        app, scr = self.run_app(a, list("hi") + [-1],
                                scr=FakeScreen([], rows=rows, cols=cols))
        self.assertNotIn("terminal too small", scr.rendered())
        self.assertEqual(app._cursor_at[0], rows - 2)  # the input line
        self.assertIn("  hi", scr.drawn)
        self.assertIn(tui.WELCOME[0][:cols - 25], scr.rendered())
        _, scr = self.run_app(a, [-1],
                              scr=FakeScreen([], rows=rows - 1, cols=cols))
        self.assertIn("terminal too small", scr.rendered())

    def test_no_other_program_is_started(self):
        # F1 opening a web page was the terminal's doing, never paf's; and
        # paf has no way to start one.
        code = ("import sys, phone_a_friend.tui, phone_a_friend.__main__; "
                "print(sorted({'webbrowser', 'subprocess'} & set(sys.modules)))")
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
            cwd=os.path.join(os.path.dirname(__file__), ".."), check=True,
        ).stdout.strip()
        self.assertEqual(out, "[]")


class LeaveCommandTest(TuiCase):
    def test_leave(self):
        b = Session.register(self.shared, "bob", "pw")
        app, _ = self.run_app(b, list("/leave") + [ENTER, -1])
        self.assertIn("open a group first, then /leave", app.status)
        gid = b.create_group("g")
        app, _ = self.run_app(b, [ENTER] + list("/leave") + [ENTER, -1])
        self.assertNotIn(gid, b.config["groups"])
        self.assertIn("left group #g", app.status)


if __name__ == "__main__":
    unittest.main()
