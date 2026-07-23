# SPDX-FileCopyrightText: 2026 Martin Gallagher
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Entry point: `python -m phone_a_friend` (or the `paf` script).

By default launches the TUI. A few subcommands are provided for scripting
and testing without a terminal UI.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from .store import SharedDir, Session, StoreError


def _shared_dir(args) -> SharedDir:
    path = args.dir or os.environ.get("PAF_DIR")
    if not path:
        print(
            "error: no shared directory given (use --dir or set PAF_DIR)",
            file=sys.stderr,
        )
        sys.exit(2)
    shared = SharedDir(path)
    shared.ensure_root()
    return shared


def _passphrase(args, confirm: bool = False) -> str:
    if args.passphrase is not None:
        return args.passphrase
    env = os.environ.get("PAF_PASSPHRASE")
    if env:
        return env
    pw = getpass.getpass("passphrase: ")
    if confirm:
        again = getpass.getpass("repeat passphrase: ")
        if pw != again:
            print("error: passphrases do not match", file=sys.stderr)
            sys.exit(1)
    return pw


def _open_session(args) -> Session:
    shared = _shared_dir(args)
    user = args.user or os.environ.get("PAF_USER") or input("username: ").strip()
    if shared.user_exists(user):
        return Session.login(shared, user, _passphrase(args))
    answer = input(f"user '{user}' not found - register? [y/N] ")
    if not answer.strip().lower().startswith("y"):
        sys.exit(1)
    return Session.register(shared, user, _passphrase(args, confirm=True))


def _open_session_synced(args) -> Session:
    """Open a session and consume pending invite replies first, so that
    freshly accepted invites are usable immediately."""
    session = _open_session(args)
    for notice in session.process_replies():
        print(f"* {notice}")
    return session


def cmd_tui(args) -> None:
    session = _open_session(args)
    from . import tui  # imported lazily: curses needs a real terminal

    tui.run(session)


def cmd_register(args) -> None:
    shared = _shared_dir(args)
    if not args.user:
        raise StoreError("specify --user")
    Session.register(shared, args.user, _passphrase(args, confirm=True))
    print(f"registered {args.user}")


def cmd_invite(args) -> None:
    s = _open_session_synced(args)
    if args.group:
        gid = s.group_by_name(args.group)
        if gid is None:
            raise StoreError(f"unknown group: {args.group}")
        s.invite_group(gid, args.to)
        print(f"group key for '{args.group}' pushed to {args.to}")
    else:
        s.invite_contact(args.to)
        print(f"chat invite (with your public key) pushed to {args.to}")


def cmd_invites(args) -> None:
    s = _open_session_synced(args)
    invites = s.list_invites()
    if not invites:
        print("no pending invites")
        return
    for fname, payload in invites:
        if payload["type"] == "contact":
            print(f"{fname}: chat invite from {payload['from']}")
        else:
            print(
                f"{fname}: group invite from {payload['from']} "
                f"to '{payload['group_name']}'"
            )


def cmd_accept(args) -> None:
    s = _open_session_synced(args)
    accepted = 0
    for fname, payload in s.list_invites():
        if args.sender in (None, payload["from"]):
            print("accepted:", s.accept_invite(fname, payload))
            accepted += 1
    if not accepted:
        print("nothing to accept")


def cmd_create_group(args) -> None:
    s = _open_session_synced(args)
    gid = s.create_group(args.name)
    print(f"created group '{args.name}' ({gid})")


def cmd_send(args) -> None:
    s = _open_session_synced(args)
    if not args.group and not args.to:
        raise StoreError("specify --to USER or --group GROUP")
    if args.group:
        gid = s.group_by_name(args.group)
        if gid is None:
            raise StoreError(f"unknown group: {args.group}")
        s.send_message("grp", gid, args.text)
    else:
        s.send_message("dm", args.to, args.text)
    print("sent")


def cmd_read(args) -> None:
    s = _open_session_synced(args)
    if not args.group and not args.to:
        raise StoreError("specify --to USER or --group GROUP")
    if args.group:
        gid = s.group_by_name(args.group)
        if gid is None:
            raise StoreError(f"unknown group: {args.group}")
        kind, target = "grp", gid
    else:
        kind, target = "dm", args.to
    files = s.list_message_files(kind, target)
    msgs = s.load_messages(kind, target, files)
    for fname in files:
        m = msgs.get(fname)
        if m:
            print(f"[{m['from']}] {m['text']}")
    s.mark_read(kind, target)


def cmd_status(args) -> None:
    s = _open_session_synced(args)
    print(f"user: {s.name}")
    print("contacts:")
    for c in sorted(s.config["contacts"]):
        print(f"  {c}  (unread: {s.unread_count('dm', c)})")
    print("groups:")
    for gid, g in s.config["groups"].items():
        print(f"  #{g['name']} [{gid}]  (unread: {s.unread_count('grp', gid)})")
    if s.config["pending"]:
        print("pending invites sent:")
        for p in s.config["pending"]:
            print(f"  {p['type']} -> {p['to']}")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        prog="paf",
        description="phone_a_friend - encrypted chat over a shared directory",
    )
    parser.add_argument("--dir", "-d", help="shared directory (or $PAF_DIR)")
    parser.add_argument("--user", "-u", help="username (or $PAF_USER)")
    parser.add_argument(
        "--passphrase",
        help="passphrase (or $PAF_PASSPHRASE; prompted if omitted)",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("tui", help="launch the TUI (default)")
    sub.add_parser("register", help="register a new user")
    p = sub.add_parser("invite", help="invite a user to chat or to a group")
    p.add_argument("to")
    p.add_argument("--group", "-g", help="push this group's key instead")
    sub.add_parser("invites", help="list incoming invites")
    p = sub.add_parser("accept", help="accept pending invites")
    p.add_argument("--from", dest="sender", help="only invites from this user")
    p = sub.add_parser("create-group", help="create a group")
    p.add_argument("name")
    p = sub.add_parser("send", help="send a message")
    p.add_argument("--to", help="recipient user")
    p.add_argument("--group", "-g", help="recipient group")
    p.add_argument("text")
    p = sub.add_parser("read", help="print a conversation")
    p.add_argument("--to", help="peer user")
    p.add_argument("--group", "-g", help="group")
    sub.add_parser("status", help="show contacts, groups and unread counts")

    args = parser.parse_args(argv)

    handlers = {
        None: cmd_tui,
        "tui": cmd_tui,
        "register": cmd_register,
        "invite": cmd_invite,
        "invites": cmd_invites,
        "accept": cmd_accept,
        "create-group": cmd_create_group,
        "send": cmd_send,
        "read": cmd_read,
        "status": cmd_status,
    }
    try:
        handlers[args.command](args)
    except StoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(130)


if __name__ == "__main__":
    main()
