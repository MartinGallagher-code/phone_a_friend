# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher

"""Entry point: `python -m phone_a_friend` (or the `paf` script).

By default launches the TUI. A few subcommands are provided for scripting
and testing without a terminal UI.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import time

from . import __copyright__, __version__, crypto
from .store import SharedDir, Session, StoreError

VERSION_TEXT = f"""\
phone_a_friend (paf) {__version__}
{__copyright__}
License: GPL-3.0-or-later <https://www.gnu.org/licenses/gpl-3.0.html>
This is free software: you are free to change and redistribute it.
There is no warranty, to the extent permitted by law.
Crypto: {{crypto}}"""

FIPS_WARNING = (
    "warning: {status}: phone_a_friend uses only FIPS-approved algorithms, "
    "but this OpenSSL is not running as a FIPS-validated module "
    "(set PAF_FIPS_WARNING=0 to silence)"
)


def fips_warning() -> None:
    """Warn on stderr when OpenSSL is not in FIPS mode (never refuses)."""
    if crypto.fips_enabled() or os.environ.get("PAF_FIPS_WARNING") == "0":
        return
    print(FIPS_WARNING.format(status=crypto.fips_status()), file=sys.stderr)


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
    for notice in session.process_replies() + session.sync_groups():
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


def cmd_remove(args) -> None:
    s = _open_session_synced(args)
    if args.group:
        gid = s.group_by_name(args.group)
        if gid is None:
            raise StoreError(f"unknown group: {args.group}")
        s.remove_group_member(gid, args.to)
        if args.to == s.name:
            print(f"left group '{args.group}'")
        else:
            print(f"removal notice for '{args.group}' pushed to {args.to}")
    else:
        s.remove_contact(args.to)
        print(f"unfriended {args.to} - a future invite can restore the chat")


def _print_notices(s: Session) -> None:
    """Print what list_invites refused."""
    for notice in s.take_notices():
        print(f"* {notice}")


def cmd_invites(args) -> None:
    s = _open_session_synced(args)
    invites = s.list_invites()
    _print_notices(s)
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
    invites = s.list_invites()
    _print_notices(s)
    for fname, payload in invites:
        if args.sender in (None, payload["from"]):
            print("accepted:", s.accept_invite(fname, payload))
            accepted += 1
    if not accepted:
        print("nothing to accept")


def cmd_create_group(args) -> None:
    s = _open_session_synced(args)
    gid = s.create_group(args.name)
    print(f"created group '{args.name}' ({gid})")


def _group_arg(s: Session, name: str) -> str:
    gid = s.group_by_name(name)
    if gid is None:
        raise StoreError(f"unknown group: {name}")
    return gid


def cmd_rename_group(args) -> None:
    s = _open_session_synced(args)
    s.rename_group(_group_arg(s, args.group), args.name)
    print(f"renamed '{args.group}' to '{args.name.strip()}'")


def cmd_delete_group(args) -> None:
    s = _open_session_synced(args)
    until = s.delete_group(_group_arg(s, args.group))
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(until))
    print(f"deleted '{args.group}' - readable until {when}")


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


def cmd_migrate(args) -> None:
    s = _open_session_synced(args)
    left = s.migrate()
    if not left:
        print("everything you can read is in the current format")
        return
    for label, count in left:
        print(
            f"{label}: {count} message(s) still in the 0.x format - "
            "theirs, until they run phone_a_friend 1.0, or unreadable here"
        )


def cmd_search(args) -> None:
    s = _open_session_synced(args)
    terms = " ".join(args.terms).split()
    found = s.search(terms)
    if not found:
        print(f"no messages contain: {' '.join(terms)}")
        return
    for kind, target, hits in found:
        name = target if kind == "dm" else "#" + s.config["groups"][target]["name"]
        print(f"{name}:")
        for _, m in hits:
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(m.get("ts", 0)))
            print(f"  {stamp} [{m.get('from', '?')}] {m['text']}")


def cmd_presence(args) -> None:
    s = _open_session_synced(args)
    if args.state is not None:
        s.set_share_presence(args.state == "on")
        s.write_beacon()
    print(f"sharing presence: {'on' if s.shares_presence else 'off'}")
    beacons = s.read_beacons({})
    for c in sorted(s.config["contacts"]):
        print(f"  {c}: {Session.presence(beacons.get(c))}")


def cmd_verify(args) -> None:
    s = _open_session_synced(args)
    name = args.contact
    number, key, changed = s.safety_number(name)
    groups = number.split()
    if changed:
        print(f"{name}'s key changed: paf still uses the old one.")
        print(f"safety number with {name}'s NEW key:")
    else:
        print(f"safety number with {name}:")
    for i in range(0, len(groups), 4):
        print("  " + " ".join(groups[i:i + 4]))
    if not changed and s.is_verified(name):
        print(f"{name} is verified")
        return
    print(f"{name} sees the same number for you: compare every digit, "
          "in person or by phone.")
    question = (
        f"accept {name}'s new key - do the numbers match? [y/N] " if changed
        else f"do the numbers match? mark {name} verified [y/N] "
    )
    try:
        answer = input(question)
    except EOFError:
        answer = ""  # nobody to ask: a script, or input from /dev/null
    if not answer.strip().lower().startswith("y"):
        print(f"{name} is not verified")
        return
    moved = s.confirm_key(name, key)
    if moved:
        print(f"{name}'s new key accepted - verified; "
              f"your key was pushed to {name} as a chat invite")
    else:
        print(f"{name} verified")


def _key_note(s: Session, name: str) -> str:
    if name in s.config["key_changes"]:
        return f"  key changed: paf verify {name}"
    return "  verified" if s.is_verified(name) else ""


def cmd_status(args) -> None:
    s = _open_session_synced(args)
    print(f"user: {s.name}")
    print("contacts:")
    for c in sorted(s.config["contacts"]):
        print(f"  {c}  (unread: {s.unread_count('dm', c)}){_key_note(s, c)}")
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
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=VERSION_TEXT.format(crypto=crypto.fips_status()),
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
    p = sub.add_parser(
        "remove", help="unfriend a user, or remove a user from a group"
    )
    p.add_argument("to")
    p.add_argument(
        "--group", "-g", help="remove from this group instead (yourself = leave)"
    )
    p = sub.add_parser(
        "verify",
        help="show a contact's safety number, to compare with theirs, "
        "and mark them verified",
    )
    p.add_argument("contact")
    sub.add_parser("invites", help="list incoming invites")
    p = sub.add_parser("accept", help="accept pending invites")
    p.add_argument("--from", dest="sender", help="only invites from this user")
    p = sub.add_parser("create-group", help="create a group")
    p.add_argument("name")
    p = sub.add_parser("rename-group", help="rename a group you own")
    p.add_argument("group")
    p.add_argument("name")
    p = sub.add_parser(
        "delete-group",
        help="delete a group you own (readable for a few days, then removed)",
    )
    p.add_argument("group")
    p = sub.add_parser("send", help="send a message")
    p.add_argument("--to", help="recipient user")
    p.add_argument("--group", "-g", help="recipient group")
    p.add_argument("text")
    p = sub.add_parser("read", help="print a conversation")
    p.add_argument("--to", help="peer user")
    p.add_argument("--group", "-g", help="group")
    sub.add_parser("status", help="show contacts, groups and unread counts")
    p = sub.add_parser(
        "search", help="print every message containing all the words"
    )
    p.add_argument("terms", nargs="+")
    p = sub.add_parser(
        "presence",
        help="show contacts' presence; on/off: share yours (default on)",
    )
    p.add_argument("state", nargs="?", choices=["on", "off"])
    sub.add_parser(
        "migrate", help="re-encrypt your 0.x messages with the FIPS algorithms"
    )

    args = parser.parse_args(argv)

    handlers = {
        None: cmd_tui,
        "tui": cmd_tui,
        "register": cmd_register,
        "invite": cmd_invite,
        "remove": cmd_remove,
        "verify": cmd_verify,
        "invites": cmd_invites,
        "accept": cmd_accept,
        "create-group": cmd_create_group,
        "rename-group": cmd_rename_group,
        "delete-group": cmd_delete_group,
        "send": cmd_send,
        "read": cmd_read,
        "status": cmd_status,
        "migrate": cmd_migrate,
        "presence": cmd_presence,
        "search": cmd_search,
    }
    fips_warning()
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
