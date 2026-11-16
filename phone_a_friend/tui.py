# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher

"""Curses TUI for phone_a_friend.

Left pane: invites, chats (contacts), groups, and registered users you have
not connected with yet, with unread indicators. It scrolls when it has more
rows than the terminal, and ▲ or ▼ on its border shows that rows are hidden
above or below.
Right pane: the active conversation plus an input line.
Bottom rows: a status line, and a key bar whose keys can be clicked.

Keys::

    Up/Down          select an item in the left pane
    Enter            open the selected item (or send, if the input line has
                     text); on an invite: prompts to accept/decline; on a
                     user under USERS: prompts to send them a chat invite
    F1               help: the keys, the mouse and the commands
    F2 or Ctrl-N     invite a user to chat (pushes your public key to them)
    F3 or Ctrl-G     create a new group
    F4 or Ctrl-O     invite a user to the active group (pushes the group key)
    F9               a menu of the selected item's actions
    F10              quit
    PgUp/PgDn        scroll message history
    Left/Right       move the cursor in the input line
    Home/End         jump to the start / end of the input line (also
                     Ctrl-A / Ctrl-E)
    Backspace/Delete delete the character before / under the cursor
    Esc              clear the input line / quit

Mouse::

    click            open a chat or group; on an invite or a user under
                     USERS, a click only selects it
    double-click     open or act on any item (answer an invite, invite a
                     user to chat)
    right-click      a menu of the item's actions
    wheel            scroll whatever is under the pointer: the
                     conversation, or the left pane
    key bar          click a key on the bottom row to press it

Slash commands typed into the input line work in any terminal - including
ones whose host application intercepts Ctrl or function keys, such as the
VS Code integrated terminal (VS Code binds Ctrl-N/Ctrl-G/Ctrl-O itself)::

    /invite USER     invite a user to chat
    /unfriend USER   stop chatting with a user (a new invite can restore it)
    /group NAME      create a group
    /ginvite USER    invite a user to the open (or selected) group
    /gremove USER    remove a user from the open (or selected) group
    /leave           leave the open (or selected) group
    /menu            the selected item's menu, as F9
    /help            help, as F1
    /quit            exit

Typing takes any character, accents, CJK and emoji included: keys are read
with ``get_wch()``, so a character arrives whole rather than one UTF-8 byte
at a time, and a key that is not text (an arrow, a function key) never adds
a character. Widths are counted in terminal cells, so a double-width
character takes two.

The client polls the shared directory a few times per second; new messages
in the active conversation appear immediately, others light up an unread
badge next to the chat or group in the left pane.
"""

from __future__ import annotations

import curses
import os
import textwrap
import time
import unicodedata
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

from .store import Session, StoreError

#: A key as :meth:`App._read_key` gives it: a ``str`` for text, an ``int``
#: for anything else (curses' ``KEY_*`` codes, and control keys such as
#: Enter, which is 10).
Key = Union[int, str]

POLL_MS = 400
#: How long curses waits after Esc for the rest of a key's sequence. Its
#: default is a whole second, which made Esc lag; a terminal sends a key's
#: sequence in one write, so 25 ms is ample. An ESCDELAY set in the
#: environment is the user's own choice and is left alone. (Meridian's.)
ESCDELAY_MS = 25
#: The window in which two clicks make a double-click. (Meridian's.)
CLICK_MS = 200
#: Lines a notch of the mouse wheel scrolls. (Meridian's.)
WHEEL_LINES = 3
MIN_ROWS, MIN_COLS = 7, 40

HELP = "Enter open/send  F1 help  F9 or right-click: menu"
QUIT_PROMPT = "Quit phone_a_friend? [y/N] "

#: The key bar on the bottom row: (key, label, action). Clicking a label
#: does what pressing its key does.
KEYBAR = (
    ("1", "Help", "help"),
    ("2", "Invite", "invite"),
    ("3", "Group", "group"),
    ("4", "GInvite", "ginvite"),
    ("9", "Menu", "menu"),
    ("10", "Quit", "quit"),
)

ENTER_KEYS = (10, 13, curses.KEY_ENTER)
BACKSPACE_KEYS = (curses.KEY_BACKSPACE, 127, 8)
ESC = 27
CTRL_A, CTRL_E = 1, 5

#: Keys that run the same actions as the key bar.
KEY_ACTIONS = {
    curses.KEY_F1: "help",
    curses.KEY_F2: "invite",
    14: "invite",  # Ctrl-N
    curses.KEY_F3: "group",
    7: "group",  # Ctrl-G
    curses.KEY_F4: "ginvite",
    15: "ginvite",  # Ctrl-O
    curses.KEY_F9: "menu",
    curses.KEY_F10: "quit",
}

HELP_LINES = [
    "Keys",
    "  Up/Down          select in the left pane",
    "  Enter            open the selection, or send the input line",
    "  F1 or /help      this help",
    "  F2 or Ctrl-N     invite a user to chat",
    "  F3 or Ctrl-G     create a group",
    "  F4 or Ctrl-O     invite a user to the open group",
    "  F9 or /menu      a menu of the selected item's actions",
    "  F10              quit",
    "  PgUp/PgDn        scroll the conversation",
    "  Left/Right       move the cursor in the input line",
    "  Home/End         start / end of the line (Ctrl-A / Ctrl-E)",
    "  Backspace/Del    delete before / under the cursor",
    "  Esc              clear the input line, or quit",
    "",
    "Mouse",
    "  click            open a chat or group; select an invite or user",
    "  double-click     open or act on any item",
    "  right-click      a menu of the item's actions",
    "  wheel            scroll what is under the pointer",
    "  bottom row       click a key to press it",
    "",
    "Commands, typed into the input line",
    "  /invite USER     invite a user to chat",
    "  /unfriend USER   stop chatting with a user",
    "  /group NAME      create a group",
    "  /ginvite USER    invite a user to the open group",
    "  /gremove USER    remove a user from the open group",
    "  /leave           leave the open group",
    "  /menu  /help  /quit",
    "",
    "Esc, Enter or a click closes this.",
]

WELCOME = [
    "Welcome! Select a chat or group on the left.",
    "",
    "Click a chat or group to open it. Double-click",
    "an invite to answer it, or a name under USERS",
    "to invite them to chat. Right-click (or F9)",
    "for a menu of what you can do with an item.",
    "",
    "F1 or /help    keys, mouse and commands",
    "F2 or Ctrl-N   invite someone to chat by name",
    "F3 or Ctrl-G   create a group",
    "F4 or Ctrl-O   invite someone to the open group",
    "",
    "Commands typed into the input line, such as",
    "/invite USER or /group NAME, work even in",
    "terminals that swallow Ctrl or function keys.",
    "",
    "Invites push public keys: without an accepted",
    "invite, messages cannot be decrypted.",
]

CP_HEADER = 1
CP_SELECT = 2
CP_UNREAD = 3
CP_STATUS = 4
CP_SENDER = 5
CP_DIM = 6
CP_KEYBAR = 7


# ------------------------------------------------------------- text widths
def char_cells(ch: str) -> int:
    """Terminal columns a character takes: 2 for wide and full-width
    characters (CJK, most emoji), 0 for combining marks and format
    characters such as the zero-width joiner, otherwise 1."""
    if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def cells(text: str) -> int:
    """Terminal columns a string takes."""
    return sum(char_cells(ch) for ch in text)


def clip(text: str, width: int) -> str:
    """The longest start of ``text`` that fits in ``width`` columns."""
    used = 0
    for i, ch in enumerate(text):
        used += char_cells(ch)
        if used > width:
            return text[:i]
    return text


def pad(text: str, width: int) -> str:
    """``text`` cut or padded with spaces to exactly ``width`` columns."""
    text = clip(text, width)
    return text + " " * (width - cells(text))


def tail(text: str, width: int) -> str:
    """The longest end of ``text`` that fits in ``width`` columns."""
    return clip(text[::-1], width)[::-1]


def wrap(text: str, width: int) -> List[str]:
    """Word-wrap ``text`` to lines of at most ``width`` columns. A line of
    double-width text, which has no spaces to break at, is cut by width."""
    out: List[str] = []
    for line in textwrap.wrap(text, width=width):
        while line:
            head = clip(line, width) or line[0]  # one wide char, if need be
            out.append(head)
            line = line[len(head):]
    return out or [""]


def is_text(ch: str) -> bool:
    """Whether a typed character belongs in the text. Control characters do
    not, nor do line and paragraph separators. Format characters do (the
    zero-width joiner holds emoji sequences together), which is why this is
    not ``str.isprintable()``."""
    return unicodedata.category(ch) not in ("Cc", "Cs", "Zl", "Zp")


def _bits(*names: str) -> int:
    """curses mouse bits by name, 0 for any this curses lacks (older
    ncurses builds have no BUTTON5, the wheel's downward notch)."""
    mask = 0
    for name in names:
        mask |= getattr(curses, name, 0)
    return mask


def _clicked() -> int:
    """A finished left click. A press alone only selects, so a press and
    its release arriving as two events still act once."""
    return _bits(
        "BUTTON1_CLICKED",
        "BUTTON1_RELEASED",
        "BUTTON1_DOUBLE_CLICKED",
        "BUTTON1_TRIPLE_CLICKED",
    )


def _put(scr, y: int, x: int, text: str, attr: int = 0) -> None:
    """Draw ``text`` at (y, x), ignoring what curses refuses (the
    bottom-right cell, a terminal shrinking mid-draw)."""
    if not text:
        return
    try:
        scr.addnstr(y, x, text, len(text), attr)
    except curses.error:
        pass


class App:
    def __init__(self, session: Session):
        self.s = session
        self.items: List[Tuple[str, object]] = []  # (kind, data) sidebar rows
        self.sel = 0
        self.top = 0  # first sidebar row on screen
        self.active: Optional[Tuple[str, str]] = None  # ("dm"|"grp", target)
        self.input = ""
        self.cursor = 0  # position in self.input
        self.hscroll = 0  # first character of self.input on screen
        self.status = f"logged in as {session.name} - {HELP}"
        self.scroll = 0  # lines scrolled up from the bottom
        self.invites: List[Tuple[str, dict]] = []
        self.msg_cache: Dict[str, Dict[str, dict]] = {}  # conv -> fname -> msg
        self.unread: Dict[str, int] = {}
        self._row_map: Dict[int, int] = {}  # screen row -> items index
        self._side_rows = 0  # sidebar rows on screen
        self._layout: Optional[Tuple[int, int, int]] = None  # rows, cols, leftw
        self._cursor_at = (0, 0)

    # ------------------------------------------------------------------ run
    def run(self) -> None:
        curses.wrapper(self._main)

    def _main(self, scr) -> None:
        curses.curs_set(1)
        scr.timeout(POLL_MS)
        if "ESCDELAY" not in os.environ:
            try:
                curses.set_escdelay(ESCDELAY_MS)
            except curses.error:
                pass
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
            curses.mouseinterval(CLICK_MS)
        except curses.error:
            pass
        if curses.has_colors():
            curses.use_default_colors()
            curses.init_pair(CP_HEADER, curses.COLOR_CYAN, -1)
            curses.init_pair(CP_SELECT, curses.COLOR_BLACK, curses.COLOR_CYAN)
            curses.init_pair(CP_UNREAD, curses.COLOR_YELLOW, -1)
            curses.init_pair(CP_STATUS, curses.COLOR_BLACK, curses.COLOR_WHITE)
            curses.init_pair(CP_SENDER, curses.COLOR_GREEN, -1)
            curses.init_pair(CP_DIM, curses.COLOR_BLUE, -1)
            curses.init_pair(CP_KEYBAR, curses.COLOR_BLACK, curses.COLOR_CYAN)

        while True:
            self._poll()
            self._draw(scr)
            try:
                key = self._read_key(scr)
            except KeyboardInterrupt:
                break
            if key is None:
                continue
            if not self._handle_key(scr, key):
                break

    @staticmethod
    def _read_key(scr) -> Optional[Key]:
        """The next key, or None if none came before the timeout.

        ``get_wch()`` gives a character as a ``str``, whole, however many
        UTF-8 bytes it took (``getch()`` gave them one byte at a time, so
        "é" arrived as two keys), and a function key as an ``int``. Control
        characters come back as their codes, so Enter is 10 and Ctrl-N 14
        whichever way the terminal sent them."""
        try:
            key = scr.get_wch()
        except curses.error:
            return None
        if isinstance(key, str) and (key < " " or key == "\x7f"):
            return ord(key)
        return key

    # ----------------------------------------------------------------- poll
    def _poll(self) -> None:
        try:
            self.invites = self.s.list_invites()
            for notice in self.s.process_replies():
                self.status = notice
        except (StoreError, OSError):
            pass

        self.unread = {}
        for contact in self.s.config["contacts"]:
            self.unread[f"dm:{contact}"] = self.s.unread_count("dm", contact)
        for gid in self.s.config["groups"]:
            self.unread[f"grp:{gid}"] = self.s.unread_count("grp", gid)

        if self.active is not None:
            kind, target = self.active
            # the peer may have unfriended us / removed us from the group
            gone = (
                target not in self.s.config["contacts"]
                if kind == "dm"
                else target not in self.s.config["groups"]
            )
            if gone:
                self.active = None
            else:
                self._fetch_active(kind, target)

        self._rebuild_items()

    def _fetch_active(self, kind: str, target: str) -> None:
        conv = self.s.conv_key(kind, target)
        cache = self.msg_cache.setdefault(conv, {})
        try:
            files = self.s.list_message_files(kind, target)
        except StoreError:
            return
        new = [f for f in files if f not in cache]
        if new:
            cache.update(self.s.load_messages(kind, target, new))
            if self.scroll == 0:
                self.s.mark_read(kind, target)
                self.unread[conv] = 0

    def _rebuild_items(self) -> None:
        items: List[Tuple[str, object]] = []
        if self.invites:
            items.append(("hdr", "INVITES"))
            for fname, payload in self.invites:
                items.append(("inv", (fname, payload)))
        items.append(("hdr", "CHATS"))
        for name in sorted(self.s.config["contacts"]):
            items.append(("dm", name))
        for p in self.s.config["pending"]:
            if p["type"] == "contact":
                items.append(("pend", p["to"]))
        items.append(("hdr", "GROUPS"))
        groups = sorted(
            self.s.config["groups"].items(), key=lambda kv: kv[1]["name"]
        )
        for gid, g in groups:
            items.append(("grp", gid))
        # registered users we have no connection with yet: select to invite
        strangers = [
            u
            for u in self.s.shared.list_users()
            if u != self.s.name
            and u not in self.s.config["contacts"]
            and not any(
                p["type"] == "contact" and p["to"] == u
                for p in self.s.config["pending"]
            )
        ]
        if strangers:
            items.append(("hdr", "USERS"))
            for u in strangers:
                items.append(("usr", u))
        self.items = items
        if self.sel >= len(items):
            self.sel = max(0, len(items) - 1)
        self._snap_selection(1)

    def _snap_selection(self, direction: int) -> None:
        """Move selection off header rows."""
        if not self.items:
            return
        n = len(self.items)
        i = self.sel
        for _ in range(n):
            if self.items[i][0] != "hdr":
                self.sel = i
                return
            i = (i + direction) % n
        self.sel = 0

    # ----------------------------------------------------------------- draw
    def _draw(self, scr) -> None:
        scr.erase()
        rows, cols = scr.getmaxyx()
        if rows < MIN_ROWS or cols < MIN_COLS:
            self._layout = None
            scr.addstr(0, 0, "terminal too small"[: cols - 1])
            scr.refresh()
            return
        leftw = max(22, min(32, cols // 4))
        self._layout = (rows, cols, leftw)
        side_rows = rows - 2  # above the status line and the key bar
        self._draw_sidebar(scr, side_rows, leftw)
        try:
            for y in range(side_rows):
                scr.addch(y, leftw, curses.ACS_VLINE)
        except curses.error:
            pass
        # ▲ / ▼ on the border: sidebar rows hidden above / below
        if self.top > 0:
            _put(scr, 0, leftw, "▲")
        if self.top + side_rows < len(self.items):
            _put(scr, side_rows - 1, leftw, "▼")
        self._draw_conversation(scr, rows, cols, leftw)
        self._draw_status(scr, rows, cols)
        self._draw_keybar(scr, rows, cols)
        self._place_cursor(scr)
        scr.refresh()

    def _draw_sidebar(self, scr, height: int, leftw: int) -> None:
        """The sidebar's rows from ``self.top``, ``height`` of them, first
        scrolling so that the selection is on screen."""
        self._row_map = {}
        self._side_rows = height
        n = len(self.items)
        top = min(self.top, max(0, n - height))
        top = max(min(top, self.sel), self.sel - height + 1, 0)
        if top and top == self.sel and self.items[top - 1][0] == "hdr":
            top -= 1  # show the selection's heading with it
        self.top = top
        for y, idx in enumerate(range(top, min(n, top + height))):
            kind, data = self.items[idx]
            label, attr = self._sidebar_label(kind, data)
            if idx == self.sel and kind != "hdr":
                attr = curses.color_pair(CP_SELECT)
                label = pad(label, leftw - 1)
            else:
                label = clip(label, leftw - 1)
            _put(scr, y, 0, label, attr)
            self._row_map[y] = idx

    def _sidebar_label(self, kind: str, data: object) -> Tuple[str, int]:
        attr = 0
        if kind == "hdr":
            label = str(data)
            attr = curses.color_pair(CP_HEADER) | curses.A_BOLD
        elif kind == "inv":
            _, payload = data  # type: ignore[misc]
            if payload["type"] == "contact":
                label = f" ✉ {payload['from']} (chat)"
            else:
                label = f" ✉ {payload['from']} → {payload['group_name']}"
            attr = curses.color_pair(CP_UNREAD) | curses.A_BOLD
        elif kind == "dm":
            label = f"  {data}"
            label += self._badge(f"dm:{data}")
        elif kind == "pend":
            label = f"  {data} (invited...)"
            attr = curses.color_pair(CP_DIM)
        elif kind == "usr":
            label = f" + {data}"
            attr = curses.color_pair(CP_DIM)
        else:  # "grp"
            g = self.s.config["groups"][data]
            label = f"  #{g['name']}"
            label += self._badge(f"grp:{data}")
        if "●" in label and kind in ("dm", "grp"):
            attr |= curses.color_pair(CP_UNREAD) | curses.A_BOLD
        return label, attr

    def _badge(self, conv: str) -> str:
        n = self.unread.get(conv, 0)
        if n <= 0:
            return ""
        return f" ●{n if n < 100 else '99+'}"

    def _conversation_title(self) -> str:
        if self.active is None:
            return "phone_a_friend"
        kind, target = self.active
        if kind == "dm":
            return f"chat with {target}"
        g = self.s.config["groups"].get(target)
        return f"#{g['name']}" if g else "?"

    def _draw_conversation(self, scr, rows: int, cols: int, leftw: int) -> None:
        x0 = leftw + 2
        width = cols - x0 - 1
        _put(scr, 0, x0, clip(self._conversation_title(), width), curses.A_BOLD)
        try:
            scr.hline(1, x0, curses.ACS_HLINE, width)
        except curses.error:
            pass

        body_top, body_bottom = 2, rows - 5  # inclusive rows for messages
        input_row = rows - 3

        lines: List[Tuple[str, int]] = []
        if self.active is None:
            lines = [(ln, 0) for ln in WELCOME]
        else:
            conv = self.s.conv_key(*self.active)
            msgs = sorted(
                self.msg_cache.get(conv, {}).values(), key=lambda m: m.get("ts", 0)
            )
            for m in msgs[-500:]:
                stamp = time.strftime("%H:%M", time.localtime(m.get("ts", 0)))
                sender = m.get("from", "?")
                attr = (
                    curses.color_pair(CP_SENDER)
                    if sender == self.s.name
                    else curses.A_BOLD
                )
                prefix = f"{stamp} {sender}: "
                wrapped = wrap(m.get("text", ""), max(10, width - cells(prefix)))
                lines.append((prefix + wrapped[0], attr))
                for cont in wrapped[1:]:
                    lines.append((" " * cells(prefix) + cont, attr))

        height = body_bottom - body_top + 1
        max_scroll = max(0, len(lines) - height)
        self.scroll = min(self.scroll, max_scroll)
        end = len(lines) - self.scroll
        visible = lines[max(0, end - height):end]
        y = body_bottom - len(visible) + 1
        for text, attr in visible:
            _put(scr, y, x0, clip(text, width), attr)
            y += 1

        try:
            scr.hline(input_row - 1, x0, curses.ACS_HLINE, width)
        except curses.error:
            pass
        prompt = "> " if self.active else "  "
        shown, cursor_x = self._input_view(width - 2)
        _put(scr, input_row, x0, prompt + shown)
        self._cursor_at = (input_row, x0 + 2 + cursor_x)

    def _input_view(self, avail: int) -> Tuple[str, int]:
        """The part of the input line that fits in ``avail`` columns, and
        the cursor's column within it. A line too long for the pane
        scrolls sideways so that the cursor stays on screen."""
        avail = max(1, avail)
        if cells(self.input) < avail:
            self.hscroll = 0
        self.hscroll = min(self.hscroll, self.cursor)
        while cells(self.input[self.hscroll:self.cursor]) > avail - 1:
            self.hscroll += 1
        shown = clip(self.input[self.hscroll:], avail)
        return shown, cells(self.input[self.hscroll:self.cursor])

    def _input_index_at(self, column: int) -> int:
        """The position in the input line of a click ``column`` columns
        into the text on screen."""
        used = 0
        for i in range(self.hscroll, len(self.input)):
            used += char_cells(self.input[i])
            if used > column:
                return i
        return len(self.input)

    def _draw_status(self, scr, rows: int, cols: int) -> None:
        _put(
            scr, rows - 2, 0, pad(f" {self.status} ", cols - 1),
            curses.color_pair(CP_STATUS),
        )

    @staticmethod
    def _keybar_layout(cols: int) -> List[Tuple[int, int, str, str, str]]:
        """(x, width, key, label, action) for each key on the key bar, the
        same for drawing it and for finding the key under a click."""
        seg = max(1, (cols - 1) // len(KEYBAR))
        return [
            (i * seg, seg, num, label, action)
            for i, (num, label, action) in enumerate(KEYBAR)
        ]

    def _draw_keybar(self, scr, rows: int, cols: int) -> None:
        y = rows - 1
        _put(scr, y, 0, " " * (cols - 1), curses.color_pair(CP_KEYBAR))
        for x, seg, num, label, _ in self._keybar_layout(cols):
            _put(scr, y, x, num, curses.A_BOLD)
            _put(
                scr, y, x + len(num), pad(label, seg - len(num)),
                curses.color_pair(CP_KEYBAR),
            )

    def _place_cursor(self, scr) -> None:
        try:
            scr.move(*self._cursor_at)
        except curses.error:
            pass

    # ----------------------------------------------------------------- keys
    def _handle_key(self, scr, key: Key) -> bool:
        """Act on a key; False means quit."""
        if isinstance(key, str):
            self._insert(key)
            return True
        action = KEY_ACTIONS.get(key)
        if action is not None:
            return self._do(scr, action)
        if key == curses.KEY_MOUSE:
            return self._handle_mouse(scr)
        if key in (curses.KEY_UP, curses.KEY_DOWN):
            self._move_selection(-1 if key == curses.KEY_UP else 1)
        elif key == curses.KEY_PPAGE:
            self._scroll_history(5)
        elif key == curses.KEY_NPAGE:
            self._scroll_history(-5)
        elif key in ENTER_KEYS:
            text = self.input.strip()
            if text.startswith("/"):
                self._clear_input()
                return self._run_command(scr, text)
            if text and self.active:
                self._send()
            else:
                self._open_selected(scr)
        elif key in BACKSPACE_KEYS:
            if self.cursor > 0:
                self.input = self.input[: self.cursor - 1] + self.input[self.cursor:]
                self.cursor -= 1
        elif key == curses.KEY_DC:
            self.input = self.input[: self.cursor] + self.input[self.cursor + 1:]
        elif key == curses.KEY_LEFT:
            self.cursor = max(0, self.cursor - 1)
        elif key == curses.KEY_RIGHT:
            self.cursor = min(len(self.input), self.cursor + 1)
        elif key in (curses.KEY_HOME, CTRL_A):
            self.cursor = 0
        elif key in (curses.KEY_END, CTRL_E):
            self.cursor = len(self.input)
        elif key == ESC:
            if not self.input:
                return not self._confirm(scr, QUIT_PROMPT)
            self._clear_input()
        # anything else (KEY_RESIZE, an unbound function key) is ignored
        return True

    def _insert(self, text: str) -> None:
        if not all(is_text(ch) for ch in text):
            return
        self.input = self.input[: self.cursor] + text + self.input[self.cursor:]
        self.cursor += len(text)

    def _clear_input(self) -> None:
        self.input = ""
        self.cursor = 0

    def _do(self, scr, action: str) -> bool:
        """Run a key-bar action, from its key or a click; False means quit."""
        if action == "help":
            self._help(scr)
        elif action == "invite":
            self._action_invite_contact(scr)
        elif action == "group":
            self._action_create_group(scr)
        elif action == "ginvite":
            self._action_invite_group(scr)
        elif action == "menu":
            self._context_menu(scr)
        else:  # "quit"
            return not self._confirm(scr, QUIT_PROMPT)
        return True

    def _scroll_history(self, lines: int) -> None:
        """Scroll the conversation ``lines`` up (negative: down). Back at
        the bottom, it has been read."""
        self.scroll = max(0, self.scroll + lines)
        if self.scroll == 0 and self.active:
            self.s.mark_read(*self.active)

    def _scroll_sidebar(self, lines: int) -> None:
        """Scroll the sidebar ``lines`` down (negative: up), taking the
        selection along when it would go off screen."""
        n, height = len(self.items), max(1, self._side_rows)
        self.top = max(0, min(self.top + lines, n - height))
        shown = range(self.top, min(n, self.top + height))
        if self.sel in shown:
            return
        rows = [i for i in shown if self.items[i][0] != "hdr"]
        if rows:
            self.sel = rows[0] if self.sel < self.top else rows[-1]

    def _region(self, y: int, x: int) -> Optional[str]:
        """The part of the screen at (y, x): "sidebar", "conversation",
        "input", "status" or "keybar"; None when nothing is drawn."""
        if self._layout is None:
            return None
        rows, _, leftw = self._layout
        if y == rows - 1:
            return "keybar"
        if y == rows - 2:
            return "status"
        if x <= leftw:
            return "sidebar"
        return "input" if y == rows - 3 else "conversation"

    def _handle_mouse(self, scr) -> bool:
        """Act on a mouse event; False means quit (from the key bar)."""
        try:
            _, x, y, _, bstate = curses.getmouse()
        except curses.error:
            return True
        where = self._region(y, x)
        wheel_up = _bits("BUTTON4_PRESSED")
        wheel_down = _bits("BUTTON5_PRESSED")
        if bstate & (wheel_up | wheel_down):
            notch = -WHEEL_LINES if bstate & wheel_up else WHEEL_LINES
            if where == "sidebar":
                self._scroll_sidebar(notch)
            elif where in ("conversation", "input"):
                self._scroll_history(-notch)
            return True
        clicked = bstate & _clicked()
        if where == "keybar":
            if clicked:
                for kx, seg, _, _, action in self._keybar_layout(self._layout[1]):
                    if kx <= x < kx + seg:
                        return self._do(scr, action)
            return True
        if where == "input":
            if clicked or bstate & curses.BUTTON1_PRESSED:
                self.cursor = self._input_index_at(x - self._layout[2] - 4)
            return True
        idx = self._row_map.get(y) if where == "sidebar" else None
        if idx is None or idx >= len(self.items) or self.items[idx][0] == "hdr":
            return True
        kind = self.items[idx][0]
        if bstate & _bits("BUTTON3_PRESSED", "BUTTON3_CLICKED"):
            self.sel = idx
            self._context_menu(scr, at=(y, x))
        elif bstate & _bits("BUTTON1_DOUBLE_CLICKED", "BUTTON1_TRIPLE_CLICKED"):
            self.sel = idx
            self._open_selected(scr)
        elif clicked:
            self.sel = idx
            # an invite or a user under USERS: a click only selects it, so
            # a stray click cannot pop up a prompt; a double-click acts
            if kind not in ("inv", "usr"):
                self._open_selected(scr)
        elif bstate & curses.BUTTON1_PRESSED:
            self.sel = idx  # the release, or the click, acts
        return True

    def _move_selection(self, direction: int) -> None:
        if not self.items:
            return
        n = len(self.items)
        i = self.sel
        for _ in range(n):
            i = (i + direction) % n
            if self.items[i][0] != "hdr":
                self.sel = i
                return

    # -------------------------------------------------------------- actions
    def _open_selected(self, scr) -> None:
        if not self.items or self.sel >= len(self.items):
            return
        kind, data = self.items[self.sel]
        if kind == "dm":
            self._open_conv("dm", str(data))
        elif kind == "grp":
            self._open_conv("grp", str(data))
        elif kind == "pend":
            self.status = f"waiting for {data} to accept your invite"
        elif kind == "usr":
            if self._confirm(scr, f"send a chat invite to {data}? [y/N] "):
                self._invite_contact(str(data))
        elif kind == "inv":
            fname, payload = data  # type: ignore[misc]
            self._handle_invite(scr, fname, payload)

    def _open_conv(self, kind: str, target: str) -> None:
        self.active = (kind, target)
        self.scroll = 0
        self._fetch_active(kind, target)
        self.s.mark_read(kind, target)
        self.unread[self.s.conv_key(kind, target)] = 0
        self.status = HELP

    def _handle_invite(self, scr, fname: str, payload: dict) -> None:
        if payload["type"] == "contact":
            what = f"chat invite from {payload['from']}"
        else:
            what = f"invite from {payload['from']} to group '{payload['group_name']}'"
        answer = self._prompt(scr, f"{what} - accept? [y=yes / n=decline / Esc] ")
        if answer is None:
            return
        if answer.strip().lower().startswith("y"):
            self._accept_invite(fname, payload)
        elif answer.strip().lower().startswith("n"):
            self._decline_invite(fname, payload)

    def _accept_invite(self, fname: str, payload: dict) -> None:
        try:
            desc = self.s.accept_invite(fname, payload)
            self.status = f"accepted: {desc}"
        except (StoreError, KeyError) as exc:
            self.status = f"error: {exc}"

    def _decline_invite(self, fname: str, payload: dict) -> None:
        try:
            self.s.decline_invite(fname, payload)
            self.status = "invite declined"
        except (StoreError, KeyError) as exc:
            self.status = f"error: {exc}"

    def _send(self) -> None:
        assert self.active is not None
        kind, target = self.active
        text = self.input.strip()
        try:
            self.s.send_message(kind, target, text)
        except StoreError as exc:
            self.status = f"error: {exc}"
            return
        self._clear_input()
        self.scroll = 0
        self._fetch_active(kind, target)
        self.s.mark_read(kind, target)

    def _invite_contact(self, name: str) -> None:
        try:
            self.s.invite_contact(name)
            self.status = f"invite sent to {name} (your key was pushed)"
        except StoreError as exc:
            self.status = f"error: {exc}"

    def _unfriend(self, name: str) -> None:
        try:
            self.s.remove_contact(name)
        except StoreError as exc:
            self.status = f"error: {exc}"
            return
        if self.active == ("dm", name):
            self.active = None
        self.status = f"unfriended {name} - a future invite can restore the chat"

    def _remove_from_group(self, gid: str, name: str) -> None:
        gname = self.s.config["groups"][gid]["name"]
        try:
            self.s.remove_group_member(gid, name)
        except StoreError as exc:
            self.status = f"error: {exc}"
            return
        if name == self.s.name:
            if self.active == ("grp", gid):
                self.active = None
            self.status = f"left group #{gname}"
        else:
            self.status = f"removal notice for #{gname} pushed to {name}"

    def _create_group(self, name: str) -> None:
        try:
            gid = self.s.create_group(name)
            self._open_conv("grp", gid)
            self.status = (
                f"group '{name.strip()}' created - invite with F4 or /ginvite USER"
            )
        except StoreError as exc:
            self.status = f"error: {exc}"

    def _invite_to_group(self, gid: str, name: str) -> None:
        gname = self.s.config["groups"][gid]["name"]
        try:
            self.s.invite_group(gid, name)
            self.status = f"group key pushed to {name} for #{gname}"
        except StoreError as exc:
            self.status = f"error: {exc}"

    def _current_gid(self) -> Optional[str]:
        """The open group, or the group selected in the sidebar."""
        if self.active and self.active[0] == "grp":
            return self.active[1]
        if self.items and self.items[self.sel][0] == "grp":
            return str(self.items[self.sel][1])
        return None

    def _action_invite_contact(self, scr) -> None:
        users = [
            u
            for u in self.s.shared.list_users()
            if u != self.s.name and u not in self.s.config["contacts"]
        ]
        hint = f" (registered: {', '.join(users[:8])})" if users else ""
        name = self._prompt(scr, f"invite user to chat{hint}: ")
        if not name:
            return
        self._invite_contact(name.strip())

    def _action_create_group(self, scr) -> None:
        name = self._prompt(scr, "new group name: ")
        if not name:
            return
        self._create_group(name)

    def _action_invite_group(self, scr, gid: Optional[str] = None) -> None:
        gid = gid or self._current_gid()
        if gid is None:
            self.status = "open a group first, then F4 (or /ginvite USER) to invite"
            return
        gname = self.s.config["groups"][gid]["name"]
        name = self._prompt(scr, f"invite user to #{gname}: ")
        if not name:
            return
        self._invite_to_group(gid, name.strip())

    def _action_remove_member(self, scr, gid: str) -> None:
        gname = self.s.config["groups"][gid]["name"]
        name = self._prompt(scr, f"remove user from #{gname}: ")
        if not name:
            return
        self._remove_from_group(gid, name.strip())

    def _action_unfriend(self, scr, name: str) -> None:
        if self._confirm(scr, f"unfriend {name}? [y/N] "):
            self._unfriend(name)

    def _action_leave(self, scr, gid: str) -> None:
        gname = self.s.config["groups"][gid]["name"]
        if self._confirm(scr, f"leave #{gname}? [y/N] "):
            self._remove_from_group(gid, self.s.name)

    def _action_group_for(self, scr, name: str) -> None:
        """Invite a contact to a group chosen from a menu of your groups."""
        groups = sorted(
            self.s.config["groups"].items(), key=lambda kv: kv[1]["name"]
        )
        if not groups:
            self.status = "no groups yet - create one with F3 or /group NAME"
            return
        choice = self._popup(
            scr, f"invite {name} to", [f"#{g['name']}" for _, g in groups]
        )
        if choice is not None:
            self._invite_to_group(groups[choice][0], name)

    # ---------------------------------------------------------- menu, help
    def _menu_entries(
        self, scr, kind: str, data: object
    ) -> Tuple[str, List[Tuple[str, Callable[[], None]]]]:
        """A sidebar item's menu: its title, and (label, action) pairs. Each
        action runs the same code as the item's key or slash command."""
        if kind == "inv":
            fname, payload = data  # type: ignore[misc]
            return f"invite from {payload['from']}", [
                ("Accept", lambda: self._accept_invite(fname, payload)),
                ("Decline", lambda: self._decline_invite(fname, payload)),
            ]
        target = str(data)
        if kind == "dm":
            return target, [
                ("Open", lambda: self._open_conv("dm", target)),
                ("Unfriend", lambda: self._action_unfriend(scr, target)),
                ("Invite to a group", lambda: self._action_group_for(scr, target)),
            ]
        if kind == "grp":
            return f"#{self.s.config['groups'][target]['name']}", [
                ("Open", lambda: self._open_conv("grp", target)),
                ("Invite a user", lambda: self._action_invite_group(scr, target)),
                ("Remove a member", lambda: self._action_remove_member(scr, target)),
                ("Leave", lambda: self._action_leave(scr, target)),
            ]
        # "usr"
        return target, [
            ("Send a chat invite", lambda: self._invite_contact(target)),
        ]

    def _context_menu(self, scr, at: Optional[Tuple[int, int]] = None) -> None:
        """The selected item's menu: from a right-click (``at`` the
        pointer), F9, the key bar or /menu (under the item)."""
        if not self.items or self.items[self.sel][0] == "hdr":
            self.status = "nothing selected - select a chat, group, invite or user"
            return
        kind, data = self.items[self.sel]
        if kind == "pend":
            self.status = f"waiting for {data} to accept your invite"
            return
        if at is None:
            row = next((y for y, i in self._row_map.items() if i == self.sel), 0)
            at = (row + 1, 2)
        title, entries = self._menu_entries(scr, kind, data)
        choice = self._popup(scr, title, [label for label, _ in entries], at=at)
        if choice is not None:
            entries[choice][1]()

    def _help(self, scr) -> None:
        self._popup(scr, "help - Up/Down scroll, Esc closes", HELP_LINES, pick=False)

    def _popup(
        self,
        scr,
        title: str,
        lines: Sequence[str],
        at: Optional[Tuple[int, int]] = None,
        pick: bool = True,
    ) -> Optional[int]:
        """A framed box over the screen, at ``at`` or centred.

        As a menu (``pick``) it returns the index of the line chosen with
        Enter or a click, or None for Esc or a click outside it. Otherwise it
        shows text, scrolling with the arrows, PgUp/PgDn and the wheel, and
        Esc, Enter, q, F1 or any click closes it."""
        chosen = 0
        first = 0
        scr.timeout(-1)
        try:
            while True:
                rows, cols = scr.getmaxyx()
                w = min(cols - 2, max(cells(title) + 6,
                                      max(map(cells, lines), default=0) + 4))
                h = min(rows - 2, len(lines) + 2)
                body = max(1, h - 2)
                if at is None:
                    y0, x0 = (rows - h) // 2, (cols - w) // 2
                else:
                    y0 = max(0, min(at[0], rows - h))
                    x0 = max(0, min(at[1], cols - w))
                if pick:
                    first = max(min(first, chosen), chosen - body + 1, 0)
                else:
                    first = max(0, min(first, len(lines) - body))
                self._draw_box(scr, (y0, x0, h, w), title, lines, first,
                               chosen if pick else None)
                key = self._read_key(scr)
                if key is None:
                    continue
                if key == curses.KEY_RESIZE:
                    self._draw(scr)
                elif key == ESC:
                    return None
                elif key in ENTER_KEYS:
                    return chosen if pick else None
                elif not pick and key in ("q", "Q", curses.KEY_F1):
                    return None
                elif key == curses.KEY_MOUSE:
                    try:
                        _, mx, my, _, bstate = curses.getmouse()
                    except curses.error:
                        continue
                    if bstate & _bits("BUTTON4_PRESSED", "BUTTON5_PRESSED"):
                        if not pick:
                            up = bstate & _bits("BUTTON4_PRESSED")
                            first += -WHEEL_LINES if up else WHEEL_LINES
                        continue
                    if not bstate & _clicked():
                        continue  # a press, or the right button's release
                    inside = y0 <= my < y0 + h and x0 <= mx < x0 + w
                    if not pick or not inside:
                        return None
                    row = first + my - y0 - 1
                    if 0 <= my - y0 - 1 < body and row < len(lines):
                        return row
                elif key in (curses.KEY_UP, curses.KEY_DOWN):
                    step = -1 if key == curses.KEY_UP else 1
                    if pick:
                        chosen = (chosen + step) % len(lines)
                    else:
                        first += step
                elif not pick and key in (curses.KEY_PPAGE, curses.KEY_NPAGE):
                    first += -body if key == curses.KEY_PPAGE else body
        finally:
            scr.timeout(POLL_MS)

    def _draw_box(self, scr, box, title, lines, first, chosen) -> None:
        y0, x0, h, w = box
        inner = w - 2
        head = clip(f" {title} ", inner - 1)
        _put(scr, y0, x0, "┌─" + head + "─" * (inner - 1 - cells(head)) + "┐",
             curses.A_BOLD)
        for r in range(h - 2):
            i = first + r
            text = pad(" " + (lines[i] if i < len(lines) else ""), inner)
            _put(scr, y0 + 1 + r, x0, "│" + text + "│")
            if i == chosen:
                _put(scr, y0 + 1 + r, x0 + 1, text, curses.color_pair(CP_SELECT))
        _put(scr, y0 + h - 1, x0, "└" + "─" * inner + "┘")
        if first > 0:
            _put(scr, y0, x0 + w - 2, "▲")
        if first + h - 2 < len(lines):
            _put(scr, y0 + h - 1, x0 + w - 2, "▼")
        try:
            if chosen is None:
                scr.move(y0, x0)
            else:
                scr.move(y0 + 1 + chosen - first, x0 + 1)
        except curses.error:
            pass
        scr.refresh()

    # ---------------------------------------------------------- / commands
    USAGE = (
        "commands: /invite /unfriend /group /ginvite /gremove /leave "
        "/menu /help /quit"
    )

    def _run_command(self, scr, text: str) -> bool:
        """Slash commands typed into the input line. These work in any
        terminal, including ones whose host application swallows Ctrl or
        function keys (e.g. the VS Code integrated terminal)."""
        parts = text.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("/quit", "/exit", "/q"):
            return False
        if cmd == "/help":
            self._help(scr)
        elif cmd == "/menu":
            self._context_menu(scr)
        elif cmd == "/invite" and arg:
            self._invite_contact(arg)
        elif cmd == "/group" and arg:
            self._create_group(arg)
        elif cmd == "/unfriend" and arg:
            self._unfriend(arg)
        elif cmd == "/leave":
            gid = self._current_gid()
            if gid is None:
                self.status = "open a group first, then /leave"
            else:
                self._remove_from_group(gid, self.s.name)
        elif cmd in ("/ginvite", "/gremove") and arg:
            gid = self._current_gid()
            if gid is None:
                self.status = f"open a group first, then {cmd} USER"
            elif cmd == "/ginvite":
                self._invite_to_group(gid, arg)
            else:
                self._remove_from_group(gid, arg)
        else:
            self.status = self.USAGE
        return True

    # -------------------------------------------------------------- prompts
    def _prompt(self, scr, label: str) -> Optional[str]:
        """Modal single-line prompt on the status line; Esc cancels.

        It takes any text, as the input line does. A key that is not text
        adds nothing, and a mouse event is read and dropped, so the release
        of the click that opened the prompt cannot type into it."""
        buf = ""
        scr.timeout(-1)
        curses.curs_set(1)
        try:
            while True:
                rows, cols = scr.getmaxyx()
                line = tail(f" {label}{buf}", cols - 2)
                _put(scr, rows - 2, 0, pad(line, cols - 1),
                     curses.color_pair(CP_STATUS))
                try:
                    scr.move(rows - 2, cells(line))
                except curses.error:
                    pass
                scr.refresh()
                key = self._read_key(scr)
                if key is None:
                    continue
                if isinstance(key, str):
                    if is_text(key):
                        buf += key
                elif key in ENTER_KEYS:
                    return buf
                elif key == ESC:
                    return None
                elif key in BACKSPACE_KEYS:
                    buf = buf[:-1]
                elif key == curses.KEY_MOUSE:
                    try:
                        curses.getmouse()
                    except curses.error:
                        pass
                elif key == curses.KEY_RESIZE:
                    self._draw(scr)
        finally:
            scr.timeout(POLL_MS)

    def _confirm(self, scr, label: str) -> bool:
        answer = self._prompt(scr, label)
        return bool(answer and answer.strip().lower().startswith("y"))


def run(session: Session) -> None:
    App(session).run()
