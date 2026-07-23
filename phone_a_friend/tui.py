# SPDX-FileCopyrightText: 2026 Martin Gallagher
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Curses TUI for phone_a_friend.

Left pane: invites, chats (contacts), groups, and registered users you have
not connected with yet, with unread indicators.
Right pane: the active conversation plus an input line.

Keys
----
Up/Down          select an item in the left pane
Enter            open the selected item (or send, if the input line has text)
                 on an invite: prompts to accept/decline
                 on a user under USERS: prompts to send them a chat invite
F2 or Ctrl-N     invite a user to chat (pushes your public key to them)
F3 or Ctrl-G     create a new group
F4 or Ctrl-O     invite a user to the active group (pushes the group key)
F10              quit
PgUp/PgDn        scroll message history
Esc              clear the input line / quit
Mouse click      select + open items in the left pane

Slash commands typed into the input line work in any terminal - including
ones whose host application intercepts Ctrl or function keys, such as the
VS Code integrated terminal (VS Code binds Ctrl-N/Ctrl-G/Ctrl-O itself):

/invite USER     invite a user to chat
/unfriend USER   stop chatting with a user (a new invite can restore it)
/group NAME      create a group
/ginvite USER    invite a user to the open (or selected) group
/gremove USER    remove a user from the open (or selected) group
/quit            exit

The client polls the shared directory a few times per second; new messages
in the active conversation appear immediately, others light up an unread
badge next to the chat or group in the left pane.
"""

from __future__ import annotations

import curses
import textwrap
import time
from typing import Dict, List, Optional, Tuple

from .store import Session, StoreError

POLL_MS = 400
HELP = "F2/^N invite  F3/^G group  F4/^O g-invite  F10 quit  Enter open/send"

CP_HEADER = 1
CP_SELECT = 2
CP_UNREAD = 3
CP_STATUS = 4
CP_SENDER = 5
CP_DIM = 6


class App:
    def __init__(self, session: Session):
        self.s = session
        self.items: List[Tuple[str, object]] = []  # (kind, data) sidebar rows
        self.sel = 0
        self.active: Optional[Tuple[str, str]] = None  # ("dm"|"grp", target)
        self.input = ""
        self.status = f"logged in as {session.name} - {HELP}"
        self.scroll = 0  # lines scrolled up from the bottom
        self.invites: List[Tuple[str, dict]] = []
        self.msg_cache: Dict[str, Dict[str, dict]] = {}  # conv -> fname -> msg
        self.unread: Dict[str, int] = {}
        self._row_map: Dict[int, int] = {}  # screen row -> items index

    # ------------------------------------------------------------------ run
    def run(self) -> None:
        curses.wrapper(self._main)

    def _main(self, scr) -> None:
        curses.curs_set(1)
        scr.timeout(POLL_MS)
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
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

        while True:
            self._poll()
            self._draw(scr)
            try:
                ch = scr.getch()
            except KeyboardInterrupt:
                break
            if ch == -1:
                continue
            if not self._handle_key(scr, ch):
                break

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
        if rows < 6 or cols < 40:
            scr.addstr(0, 0, "terminal too small"[: cols - 1])
            scr.refresh()
            return
        leftw = max(22, min(32, cols // 4))
        self._draw_sidebar(scr, rows, leftw)
        try:
            for y in range(rows - 1):
                scr.addch(y, leftw, curses.ACS_VLINE)
        except curses.error:
            pass
        self._draw_conversation(scr, rows, cols, leftw)
        self._draw_status(scr, rows, cols)
        self._place_cursor(scr, rows, cols, leftw)
        scr.refresh()

    def _draw_sidebar(self, scr, rows: int, leftw: int) -> None:
        self._row_map = {}
        y = 0
        for idx, (kind, data) in enumerate(self.items):
            if y >= rows - 1:
                break
            attr = 0
            if kind == "hdr":
                label = str(data)
                attr = curses.color_pair(CP_HEADER) | curses.A_BOLD
            elif kind == "inv":
                _, payload = data
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
            unread_here = ("●" in label)
            if unread_here and kind in ("dm", "grp"):
                attr |= curses.color_pair(CP_UNREAD) | curses.A_BOLD
            if idx == self.sel and kind != "hdr":
                attr = curses.color_pair(CP_SELECT)
                label = label.ljust(leftw - 1)
            try:
                scr.addnstr(y, 0, label, leftw - 1, attr)
            except curses.error:
                pass
            self._row_map[y] = idx
            y += 1

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
        title = self._conversation_title()
        try:
            scr.addnstr(0, x0, title, width, curses.A_BOLD)
            scr.hline(1, x0, curses.ACS_HLINE, width)
        except curses.error:
            pass

        body_top, body_bottom = 2, rows - 4  # inclusive rows for messages
        input_row = rows - 2

        lines: List[Tuple[str, int]] = []
        if self.active is None:
            for ln in [
                "Welcome! Select a chat or group on the left.",
                "",
                "Registered users you have not connected with",
                "appear under USERS - select one and press",
                "Enter to send them a chat invite.",
                "",
                "F2 or Ctrl-N   invite someone to chat by name",
                "F3 or Ctrl-G   create a group",
                "F4 or Ctrl-O   invite someone to the open group",
                "F10            quit",
                "",
                "Or type a command into the input line:",
                "/invite USER   /group NAME   /ginvite USER",
                "/unfriend USER   /gremove USER   /quit",
                "(commands always work, even in terminals that",
                "swallow Ctrl or function keys, like VS Code)",
                "",
                "Invites push public keys: without an accepted",
                "invite, messages cannot be decrypted.",
            ]:
                lines.append((ln, 0))
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
                wrapped = textwrap.wrap(
                    m.get("text", ""),
                    width=max(10, width - len(prefix)),
                ) or [""]
                lines.append((prefix + wrapped[0], attr))
                for cont in wrapped[1:]:
                    lines.append((" " * len(prefix) + cont, attr))

        height = body_bottom - body_top + 1
        max_scroll = max(0, len(lines) - height)
        self.scroll = min(self.scroll, max_scroll)
        end = len(lines) - self.scroll
        visible = lines[max(0, end - height):end]
        y = body_bottom - len(visible) + 1
        for text, attr in visible:
            try:
                scr.addnstr(y, x0, text, width, attr)
            except curses.error:
                pass
            y += 1

        try:
            scr.hline(input_row - 1, x0, curses.ACS_HLINE, width)
            prompt = "> " if self.active else "  "
            scr.addnstr(input_row, x0, prompt + self.input, width)
        except curses.error:
            pass

    def _draw_status(self, scr, rows: int, cols: int) -> None:
        bar = f" {self.status} "[: cols - 1]
        try:
            scr.addnstr(
                rows - 1, 0, bar.ljust(cols - 1), cols - 1,
                curses.color_pair(CP_STATUS),
            )
        except curses.error:
            pass

    def _place_cursor(self, scr, rows: int, cols: int, leftw: int) -> None:
        x0 = leftw + 2
        width = cols - x0 - 1
        x = min(x0 + 2 + len(self.input), x0 + width - 1)
        try:
            scr.move(rows - 2, x)
        except curses.error:
            pass

    # ----------------------------------------------------------------- keys
    def _handle_key(self, scr, ch: int) -> bool:
        if ch == curses.KEY_RESIZE:
            return True
        if ch == curses.KEY_MOUSE:
            self._handle_mouse(scr)
            return True
        if ch in (curses.KEY_UP, curses.KEY_DOWN):
            self._move_selection(-1 if ch == curses.KEY_UP else 1)
            return True
        if ch == curses.KEY_PPAGE:
            self.scroll += 5
            return True
        if ch == curses.KEY_NPAGE:
            self.scroll = max(0, self.scroll - 5)
            if self.scroll == 0 and self.active:
                self.s.mark_read(*self.active)
            return True
        if ch in (10, 13, curses.KEY_ENTER):
            text = self.input.strip()
            if text.startswith("/"):
                self.input = ""
                return self._run_command(text)
            if text and self.active:
                self._send()
            else:
                self._open_selected(scr)
            return True
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            self.input = self.input[:-1]
            return True
        if ch == 27:  # Esc
            if self.input:
                self.input = ""
                return True
            return not self._confirm(scr, "Quit phone_a_friend? [y/N] ")
        if ch in (14, curses.KEY_F2):  # Ctrl-N / F2
            self._action_invite_contact(scr)
            return True
        if ch in (7, curses.KEY_F3):  # Ctrl-G / F3
            self._action_create_group(scr)
            return True
        if ch in (15, curses.KEY_F4):  # Ctrl-O / F4
            self._action_invite_group(scr)
            return True
        if ch == curses.KEY_F10:
            return not self._confirm(scr, "Quit phone_a_friend? [y/N] ")
        if 32 <= ch < 0x110000 and ch != 127:
            self.input += chr(ch)
        return True

    def _handle_mouse(self, scr) -> None:
        try:
            _, x, y, _, bstate = curses.getmouse()
        except curses.error:
            return
        if not bstate & (
            curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED | curses.BUTTON1_RELEASED
        ):
            return
        idx = self._row_map.get(y)
        if idx is not None and self.items and self.items[idx][0] != "hdr":
            self.sel = idx
            self._open_selected(scr)

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
            fname, payload = data
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
        try:
            if answer is None:
                return
            if answer.strip().lower().startswith("y"):
                desc = self.s.accept_invite(fname, payload)
                self.status = f"accepted: {desc}"
            elif answer.strip().lower().startswith("n"):
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
        self.input = ""
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

    def _action_invite_group(self, scr) -> None:
        gid = self._current_gid()
        if gid is None:
            self.status = "open a group first, then F4 (or /ginvite USER) to invite"
            return
        gname = self.s.config["groups"][gid]["name"]
        name = self._prompt(scr, f"invite user to #{gname}: ")
        if not name:
            return
        self._invite_to_group(gid, name.strip())

    # ---------------------------------------------------------- / commands
    USAGE = (
        "commands: /invite USER  /unfriend USER  /group NAME  "
        "/ginvite USER  /gremove USER  /quit"
    )

    def _run_command(self, text: str) -> bool:
        """Slash commands typed into the input line. These work in any
        terminal, including ones whose host application swallows Ctrl or
        function keys (e.g. the VS Code integrated terminal)."""
        parts = text.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("/quit", "/exit", "/q"):
            return False
        if cmd == "/invite" and arg:
            self._invite_contact(arg)
        elif cmd == "/group" and arg:
            self._create_group(arg)
        elif cmd == "/unfriend" and arg:
            self._unfriend(arg)
        elif cmd == "/ginvite" and arg:
            gid = self._current_gid()
            if gid is None:
                self.status = "open a group first, then /ginvite USER"
            else:
                self._invite_to_group(gid, arg)
        elif cmd == "/gremove" and arg:
            gid = self._current_gid()
            if gid is None:
                self.status = "open a group first, then /gremove USER"
            else:
                self._remove_from_group(gid, arg)
        else:
            self.status = self.USAGE
        return True

    # -------------------------------------------------------------- prompts
    def _prompt(self, scr, label: str) -> Optional[str]:
        """Modal single-line prompt on the status bar; Esc cancels."""
        rows, cols = scr.getmaxyx()
        buf = ""
        scr.timeout(-1)
        curses.curs_set(1)
        try:
            while True:
                line = f" {label}{buf}"
                try:
                    scr.addnstr(
                        rows - 1, 0, line.ljust(cols - 1), cols - 1,
                        curses.color_pair(CP_STATUS),
                    )
                    scr.move(rows - 1, min(len(line), cols - 2))
                except curses.error:
                    pass
                scr.refresh()
                ch = scr.getch()
                if ch in (10, 13, curses.KEY_ENTER):
                    return buf
                if ch == 27:
                    return None
                if ch in (curses.KEY_BACKSPACE, 127, 8):
                    buf = buf[:-1]
                elif 32 <= ch < 0x110000 and ch != curses.KEY_RESIZE:
                    buf += chr(ch)
        finally:
            scr.timeout(POLL_MS)

    def _confirm(self, scr, label: str) -> bool:
        answer = self._prompt(scr, label)
        return bool(answer and answer.strip().lower().startswith("y"))


def run(session: Session) -> None:
    App(session).run()
