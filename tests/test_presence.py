# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher

"""Presence, typing and read receipts through beacon files (PAF-005)."""

import builtins
import contextlib
import io
import json
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
from test_tui import FakeScreen, TuiCase, fake_curses  # noqa: E402


class Clock:
    """A clock the test moves by hand, starting now (file mtimes are real)."""

    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


class BeaconStoreTest(TuiCase):
    def beacon_files(self):
        try:
            return sorted(os.listdir(self.shared.beacons_dir()))
        except FileNotFoundError:
            return []

    def test_on_by_default_and_one_file_per_user_and_device(self):
        a, b = self.make_pair()
        self.assertTrue(a.shares_presence)
        self.assertTrue(a.write_beacon())
        self.assertTrue(a.write_beacon())
        self.assertEqual(self.beacon_files(), [f"alice.{a.device}.json"])
        self.assertEqual(len(a.device), 8)
        self.assertNotIn(os.uname().nodename, a.device)

    def test_contents_are_encrypted_per_conversation(self):
        a, b = self.make_pair()
        gid = a.create_group("secret-plans")
        eve = Session.register(self.shared, "eve", "pw")
        a.send_message("dm", "bob", "x")
        a.mark_read("dm", "bob")
        a.write_beacon(typing=("dm", "bob"))
        path = os.path.join(self.shared.beacons_dir(), f"alice.{a.device}.json")
        with open(path) as fh:
            raw = fh.read()
        for word in ("bob", "secret-plans", gid, "read", "typing"):
            self.assertNotIn(word, raw)
        self.assertEqual(len(json.loads(raw)["entries"]), 2)
        # bob reads his conversation's entry (not one for his own group,
        # which alice is not in); eve, in none, reads nothing
        b.create_group("bobs-own")
        seen = b.read_beacons({})["alice"]["convs"]
        self.assertEqual(set(seen), {"dm:alice"})
        self.assertIn("typing_at", seen["dm:alice"])
        self.assertEqual(eve.read_beacons({})["alice"]["convs"], {})

    def test_presence_from_the_beacons_age(self):
        a, b = self.make_pair()
        a.write_beacon()
        path = os.path.join(self.shared.beacons_dir(), f"alice.{a.device}.json")
        mtime = os.stat(path).st_mtime
        beacon = b.read_beacons({})["alice"]
        self.assertEqual(Session.presence(beacon, mtime + 5), "active")
        self.assertEqual(Session.presence(beacon, mtime + 120), "idle")
        self.assertEqual(Session.presence(beacon, mtime + 3600), "away")
        self.assertEqual(Session.presence(None), "away")
        self.assertEqual(Session.presence(beacon), "active")

    def test_devices_merge(self):
        a, b = self.make_pair()
        a.config["last_read"]["dm:bob"] = 5.0
        with mock.patch.object(Session, "device", "aaaaaaaa"):
            a.write_beacon()
        a.config["last_read"]["dm:bob"] = 9.0
        with mock.patch.object(Session, "device", "bbbbbbbb"):
            a.write_beacon(typing=("dm", "bob"), now=42.0)
        merged = b.read_beacons({})["alice"]["convs"]["dm:alice"]
        self.assertEqual(merged, {"read": 9.0, "typing_at": 42.0})

    def test_opt_out_writes_nothing_and_removes_ours(self):
        a, b = self.make_pair()
        b.write_beacon()
        a.write_beacon()
        with mock.patch.object(Session, "device", "otherdev"):
            a.write_beacon()
        a.set_share_presence(False)
        self.assertFalse(Session.login(self.shared, "alice", "pw").shares_presence)
        self.assertEqual(self.beacon_files(), [f"bob.{b.device}.json"])
        self.assertFalse(a.write_beacon())
        self.assertEqual(self.beacon_files(), [f"bob.{b.device}.json"])
        a.set_share_presence(True)
        self.assertTrue(a.write_beacon())
        os.rename(self.shared.beacons_dir(), self.shared.beacons_dir() + "-x")
        a.set_share_presence(False)  # no beacons directory at all: fine

    def test_junk_is_ignored(self):
        a, b = self.make_pair()
        a.write_beacon()
        d = self.shared.beacons_dir()
        for name, body in [
            ("notes.txt", "x"),
            ("carol.dev.json", "{ nope"),
            ("dave.dev.json", json.dumps({"v": 99, "entries": {}})),
            ("erin.dev.json", json.dumps({"v": 2})),
            ("frank.dev.json", json.dumps([1])),
        ]:
            with open(os.path.join(d, name), "w") as fh:
                fh.write(body)
        os.mkdir(os.path.join(d, "x.y.json"))
        with open(os.path.join(d, f"alice.{a.device}.json")) as fh:
            body = json.load(fh)
        tag = next(iter(body["entries"]))
        body["entries"][tag] = "AAAA"
        with open(os.path.join(d, "alice.forged.json"), "w") as fh:
            json.dump(body, fh)
        got = b.read_beacons({})
        self.assertEqual(got["alice"]["convs"]["dm:alice"], {"read": 0.0})
        for user in ("carol", "dave", "erin", "frank"):
            self.assertEqual(got[user]["convs"], {})
        self.assertEqual(Session.read_beacons(b, {}).keys(), got.keys())

    def test_a_vanishing_file_and_a_contact_still_on_0x(self):
        a, b = self.make_pair()
        a.write_beacon()
        a.config["contacts"]["old"] = store.crypto.b64e(bytes(32))
        a.write_beacon()  # the 0.x contact is skipped, not an error
        cache = {}
        b.read_beacons(cache)
        self.assertEqual(len(cache), 1)
        real_stat = os.stat

        def stat(path, *args, **kw):
            if path.endswith(".json") and "beacons" in path:
                raise FileNotFoundError(path)
            return real_stat(path, *args, **kw)

        with mock.patch.object(store.os, "stat", stat):
            self.assertEqual(b.read_beacons(cache), {})
        self.assertEqual(cache, {})

    def test_tick_cost_with_fifty_users(self):
        a, b = self.make_pair()
        d = self.shared.beacons_dir()
        os.makedirs(d, exist_ok=True)
        for i in range(50):
            with open(os.path.join(d, f"user{i}.dev{i}.json"), "w") as fh:
                json.dump({"v": 2, "entries": {}}, fh)
        cache = {}
        b.read_beacons(cache)
        calls = {"listdir": 0, "open": 0}
        real_listdir, real_open = os.listdir, builtins.open

        def listdir(path):
            calls["listdir"] += 1
            return real_listdir(path)

        def counting_open(path, *args, **kw):
            calls["open"] += 1
            return real_open(path, *args, **kw)

        with mock.patch.object(store.os, "listdir", listdir), \
                mock.patch.object(builtins, "open", counting_open):
            b.read_beacons(cache)
        self.assertEqual(calls, {"listdir": 1, "open": 0})
        # one beacon changes: exactly one read
        os.utime(os.path.join(d, "user7.dev7.json"), (1, 1))
        calls.update(listdir=0, open=0)
        with mock.patch.object(store.os, "listdir", listdir), \
                mock.patch.object(builtins, "open", counting_open):
            b.read_beacons(cache)
        self.assertEqual(calls, {"listdir": 1, "open": 1})


class BeaconTuiTest(TuiCase):
    def app(self, session, clock):
        app = tui.App(session)
        app._clock = clock
        app._last_activity = clock()
        return app

    def render(self, app):
        scr = FakeScreen([])
        with fake_curses():
            app._draw(scr)
        return scr.rendered()

    def test_two_clients_see_each_other(self):
        a, b = self.make_pair()
        clock = Clock()
        alice, bob = self.app(a, clock), self.app(b, clock)
        alice._poll()  # writes alice's first beacon at once
        bob._poll()
        self.assertEqual(bob._presence("alice"), "active")
        self.assertIn("•alice", self.render(bob))

        # alice opens the chat and types: bob sees it on his next tick
        alice._open_conv("dm", "bob")
        alice.input = "hel"
        clock.t += tui.BEACON_MIN
        alice._poll()
        bob._open_conv("dm", "alice")
        bob._poll()
        self.assertIn("alice is typing...", self.render(bob))
        self.assertIn("chat with alice (active)", self.render(bob))
        # a slash command is not typing; typing expires without a refresh
        clock.t += store.TYPING_TTL + 1
        self.assertNotIn("typing", self.render(bob))

        # alice sends; bob reads it; alice sees "read by bob"
        alice.input = ""
        a.send_message("dm", "bob", "hello")
        alice._poll()
        self.assertNotIn("read by", self.render(alice))
        clock.t += tui.BEACON_MIN
        bob._poll()  # bob's client marks it read, and its beacon says so
        clock.t += tui.BEACON_MIN
        bob._poll()
        alice._poll()
        self.assertIn("✓ read by bob", self.render(alice))

        # bob goes quiet: no more writes, and his beacon ages to idle, away
        clock.t += tui.IDLE_AFTER + 1
        bob._poll()
        alice.beacons["bob"]["mtime"] = clock.t - 120
        self.assertEqual(alice._presence("bob"), "idle")
        self.assertIn("◦bob", self.render(alice))
        alice.beacons["bob"]["mtime"] = clock.t - 3600
        self.assertIn("chat with bob (away)", self.render(alice))

    def test_group_typing_and_receipts(self):
        a, b = self.make_pair()
        c = Session.register(self.shared, "carol", "pw")
        gid = a.create_group("g")
        for other in (b, c):
            a.invite_group(gid, other.name)
            other.accept_invite(*other.list_invites()[0])
        clock = Clock()
        apps = [self.app(s, clock) for s in (a, b, c)]
        for app in apps:
            app._open_conv("grp", gid)
        a.send_message("grp", gid, "hi all")
        for app in apps[1:]:
            app.input = "typing"
        for _ in range(3):
            clock.t += tui.BEACON_EVERY
            for app in apps:
                app._poll()
        apps[0]._poll()  # sees this round's beacons
        shown = self.render(apps[0])
        self.assertIn("✓ read by bob, carol", shown)
        self.assertIn("bob, carol are typing...", shown)

    def test_throttling(self):
        a, b = self.make_pair()
        clock = Clock()
        alice = self.app(a, clock)
        with mock.patch.object(a, "write_beacon", wraps=a.write_beacon) as w:
            alice._poll()
            alice._poll()
            self.assertEqual(w.call_count, 1)  # nothing changed
            clock.t += tui.BEACON_EVERY
            alice._poll()
            self.assertEqual(w.call_count, 2)  # the heartbeat
            alice._open_conv("dm", "bob")
            alice.input = "x"
            alice._poll()
            self.assertEqual(w.call_count, 2)  # changed, but too soon
            clock.t += tui.BEACON_MIN
            alice._poll()
            self.assertEqual(w.call_count, 3)
            clock.t += tui.TYPING_REFRESH
            alice._poll()
            self.assertEqual(w.call_count, 4)  # typing refreshed
            clock.t += tui.IDLE_AFTER + 1
            alice._poll()
            self.assertEqual(w.call_count, 4)  # idle: nothing written
            alice._handle_key(None, "y")  # a key makes her active again
            alice._poll()
            self.assertEqual(w.call_count, 5)
        with mock.patch.object(a, "write_beacon", side_effect=OSError), \
                mock.patch.object(a, "read_beacons", side_effect=OSError):
            clock.t += tui.BEACON_EVERY
            alice._poll()  # neither failure stops the TUI

    def test_presence_command(self):
        a, b = self.make_pair()
        app, _ = self.run_app(a, list("/presence off") + [10, -1])
        self.assertIn("presence off", app.status)
        self.assertFalse(a.shares_presence)
        self.assertEqual(
            [f for f in os.listdir(self.shared.beacons_dir()) if f.startswith("alice.")],
            [],
        )
        app, _ = self.run_app(a, list("/presence on") + [10, -1])
        self.assertIn("sharing presence", app.status)
        self.assertTrue(a.shares_presence)


class PresenceCliTest(TuiCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.dict(os.environ, {"PAF_FIPS_WARNING": "0"}):
            cli.main(["-d", self.tmp, "-u", "alice", "--passphrase", "pw", *argv])
        return out.getvalue()

    def test_presence(self):
        a, b = self.make_pair()
        b.write_beacon()
        out = self.run_cli("presence")
        self.assertIn("sharing presence: on", out)
        self.assertIn("bob: active", out)
        self.assertIn("sharing presence: off", self.run_cli("presence", "off"))
        self.assertIn("sharing presence: on", self.run_cli("presence", "on"))
        self.assertTrue(
            os.path.exists(os.path.join(self.shared.beacons_dir(), f"alice.{a.device}.json"))
        )


if __name__ == "__main__":
    unittest.main()
