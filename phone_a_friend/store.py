# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher

"""Shared-directory message store for phone_a_friend.

There is no server: every client reads and writes plain files inside one
shared directory. Layout::

    <shared>/
      users/<name>/identity.json     public identity (name + public key)
      users/<name>/config.enc        that user's encrypted client config
      invites/<name>/<id>.json       sealed invites pushed TO <name>
      replies/<name>/<id>.json       sealed invite replies pushed TO <name>
      dm/<hidden>/<ts>-<rand>.json   direct messages (pair-key encrypted); the
                                     directory is named from the pair key
      groups/<gid>/meta.json         group metadata (name encrypted)
      groups/<gid>/msgs/<...>.json   group messages (group-key encrypted)
      beacons/<name>.<device>.json   presence, typing and read receipts

Access control = filesystem permissions on the shared directory plus
encryption: a message can only be decrypted by holders of the right keys.

Every encrypted record (``config.enc``, invites, replies and messages) is a
JSON wrapper with a ``"v"`` field naming its format, currently
:data:`crypto.FORMAT`. Records written by 0.x have no ``"v"``; they are read
through :mod:`phone_a_friend.legacy` and migrated (see :meth:`Session.login`
and :meth:`Session.migrate`). A record with a ``"v"`` this client does not
know is never guessed at: it is skipped, or refused for ``config.enc``.

Invites and replies are checked before anything is done with them (PAF-009),
because anyone who can write to the shared directory can drop a file into
another user's ``invites/`` or ``replies/``:

* the sender they name must be a registered user, and on a multi-user system
  the file must belong to the same Unix account as that user's
  ``identity.json``;
* a key they carry must be the one that user's ``identity.json`` publishes;
* an acceptance must answer an invite we sent;
* a decline, a group join and a removal carry no key, so 2.1 signs them
  (HMAC-SHA256 under a key derived from the sender's and the recipient's
  P-256 ECDH secret, and for a group removal the group key too);
* a contact's stored key is never replaced: a different one is recorded as
  a key change, and the contact moves to it only when the user has compared
  safety numbers (:meth:`Session.safety_number`, :meth:`Session.confirm_key`).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import secrets
import shutil
import stat
import time
from typing import Dict, List, Optional, Tuple

from . import crypto

# \Z, not $: "$" also matches before a trailing newline, so "bob\n" passed
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}\Z")

# Directories other users must be able to drop files into. The sticky bit
# keeps writers from deleting each other's files while still letting the
# directory owner clean up.
DROP_DIR_MODE = 0o1777
USER_DIR_MODE = 0o755
PUBLIC_FILE_MODE = 0o644
PRIVATE_FILE_MODE = 0o600


# 0.x identity keys were raw X25519 public keys, 32 bytes long.
LEGACY_PUB_LEN = 32

#: Presence, from the age of a user's newest beacon file (PAF-005).
PRESENCE_ACTIVE = 60          # seconds: "active"
PRESENCE_IDLE = 15 * 60       # seconds: "idle"; older than this is "away"
#: How long a typing indicator lasts without being refreshed.
TYPING_TTL = 8

#: How long a deleted group stays readable before clients remove it.
GROUP_GRACE_DAYS = 3
GROUP_GRACE = GROUP_GRACE_DAYS * 24 * 3600

#: Invites and replies, by their "type", as notices name them.
WHAT = {
    "contact": "chat invite",
    "group": "group invite",
    "contact_accept": "chat acceptance",
    "contact_decline": "chat decline",
    "group_accept": "group acceptance",
    "group_decline": "group decline",
    "contact_removed": "chat removal notice",
    "group_removed": "group removal notice",
}
#: Replies that carry no key, so that only a signature (or, from a client
#: older than 2.1, which signs nothing, the file's owner) shows who sent them.
SIGNED = {
    "contact_decline", "group_accept", "group_decline",
    "contact_removed", "group_removed",
}
#: Every kind of reply.
REPLIES = SIGNED | {"contact_accept"}


class StoreError(Exception):
    pass


def _not_upgraded(name: str) -> StoreError:
    return StoreError(
        f"{name} has not upgraded to phone_a_friend 1.0 yet - "
        "their messages so far can still be read"
    )


def _wrap(field: str, blob: bytes) -> bytes:
    """An encrypted record as written to disk: its format, then its bytes."""
    return json.dumps({"v": crypto.FORMAT, field: crypto.b64e(blob)}).encode()


def _atomic_write(path: str, data: bytes, mode: int) -> None:
    tmp = os.path.join(
        os.path.dirname(path), f".tmp-{secrets.token_hex(6)}"
    )
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _make_dir(path: str, mode: int) -> None:
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError:
        pass  # not the owner; permissions were set by whoever created it


def _sticky_dir(path: str) -> None:
    """Make ``path`` a directory everyone can add to and no one can take
    another's entry from: created with mode 1777, or, if it exists and is
    ours, given the sticky bit it lacks. Without it, anyone who can write to
    ``users/`` could rename another user's directory away and put their own
    ``identity.json`` in its place."""
    try:
        os.mkdir(path)
    except FileExistsError:
        try:
            st = os.stat(path)
            if st.st_uid == os.getuid() and not st.st_mode & stat.S_ISVTX:
                os.chmod(path, stat.S_IMODE(st.st_mode) | stat.S_ISVTX)
        except OSError:
            pass  # not ours to change
        return
    _make_dir(path, DROP_DIR_MODE)


#: The fields each kind of invite or reply needs, all strings.
FIELDS = {
    "contact": ("from", "from_pub"),
    "group": ("from", "from_pub", "group_id", "group_name", "group_key"),
    "contact_accept": ("from", "from_pub"),
    "contact_decline": ("from",),
    "group_accept": ("from", "group_id"),
    "group_decline": ("from", "group_id"),
    "contact_removed": ("from",),
    "group_removed": ("from", "group_id"),
}


def _well_formed(payload: dict, kind: str) -> bool:
    """Whether an invite or reply has the fields its kind needs, as strings
    (a list where a name belongs must not crash a reader)."""
    return all(isinstance(payload.get(f), str) for f in FIELDS[kind])


def _auth_data(payload: dict) -> bytes:
    """The bytes a reply's signature covers: every field but the signature,
    in one canonical form."""
    body = {k: v for k, v in payload.items() if k != "auth"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def _msg_filename() -> str:
    return f"{time.time_ns():020d}-{secrets.token_hex(4)}.json"


def _ts_of(filename: str) -> float:
    try:
        return int(filename.split("-", 1)[0]) / 1e9
    except (ValueError, IndexError):
        return 0.0


class SharedDir:
    """Path helpers + public (unauthenticated) reads on the shared dir."""

    def __init__(self, root: str):
        self.root = os.path.abspath(os.path.expanduser(root))

    # -- layout ------------------------------------------------------------
    def users_dir(self) -> str:
        return os.path.join(self.root, "users")

    def harden(self) -> None:
        """Give ``users/``, ``invites/`` and ``replies/`` the sticky bit
        (:func:`_sticky_dir`): created so, or added where this user owns
        them."""
        for top in ("users", "invites", "replies"):
            _sticky_dir(os.path.join(self.root, top))

    def user_dir(self, name: str) -> str:
        return os.path.join(self.root, "users", name)

    def identity_path(self, name: str) -> str:
        return os.path.join(self.user_dir(name), "identity.json")

    def config_path(self, name: str) -> str:
        return os.path.join(self.user_dir(name), "config.enc")

    def invites_dir(self, name: str) -> str:
        return os.path.join(self.root, "invites", name)

    def replies_dir(self, name: str) -> str:
        return os.path.join(self.root, "replies", name)

    def dm_dir(self, a: str, b: str) -> str:
        """The DM directory by its names, as before 2.0: still read, and
        emptied into the hidden one (:meth:`Session.dm_directory`)."""
        lo, hi = sorted([a, b])
        return os.path.join(self.root, "dm", f"{lo}__{hi}")

    def group_dir(self, gid: str) -> str:
        return os.path.join(self.root, "groups", gid)

    def group_msgs_dir(self, gid: str) -> str:
        return os.path.join(self.group_dir(gid), "msgs")

    def beacons_dir(self) -> str:
        return os.path.join(self.root, "beacons")

    # -- public reads --------------------------------------------------------
    def ensure_root(self) -> None:
        if not os.path.isdir(self.root):
            raise StoreError(
                f"shared directory does not exist: {self.root}"
            )

    def user_exists(self, name: str) -> bool:
        return os.path.isfile(self.identity_path(name))

    def list_users(self) -> List[str]:
        try:
            names = os.listdir(self.users_dir())
        except FileNotFoundError:
            return []
        return sorted(n for n in names if self.user_exists(n))

    def read_identity(self, name: str) -> dict:
        try:
            with open(self.identity_path(name), "rb") as fh:
                ident = json.loads(fh.read())
        except (OSError, ValueError) as exc:
            raise StoreError(f"no such user: {name}") from exc
        if ident.get("name") != name or "pub" not in ident:
            raise StoreError(f"corrupt identity for {name}")
        return ident

    def read_group_meta(self, gid: str) -> dict:
        try:
            with open(os.path.join(self.group_dir(gid), "meta.json"), "rb") as fh:
                return json.loads(fh.read())
        except (OSError, ValueError) as exc:
            raise StoreError(f"no such group: {gid}") from exc


class Session:
    """A logged-in user: private key + decrypted config + store operations."""

    def __init__(self, shared: SharedDir, name: str, key: bytes, config: dict):
        self.shared = shared
        self.name = name
        self._cfg_key = key  # passphrase-derived key protecting config.enc
        self._salt = b""
        self._iterations = 0
        self.config = config
        config.setdefault("legacy_contacts", {})
        config.setdefault("verified", {})     # contact -> the key verified
        config.setdefault("key_changes", {})  # contact -> new key, not taken
        self.priv = crypto.private_key_from_bytes(crypto.b64d(config["priv"]))
        self.pub = crypto.public_key_bytes(self.priv)
        self._legacy = None  # the 0.x X25519 key, loaded when first needed
        self._hidden_dm: Dict[str, str] = {}  # contact -> hidden DM directory
        self._moved_dm: set = set()  # contacts whose old directory we emptied
        self._notices: List[str] = []  # from list_invites: take_notices()
        self._ident_seen: Dict[str, tuple] = {}  # check_contact_keys' cache

    # ------------------------------------------------------------ accounts
    @staticmethod
    def register(shared: SharedDir, name: str, passphrase: str) -> "Session":
        shared.ensure_root()
        if not SAFE_NAME.match(name):
            raise StoreError(
                "username must be 1-32 chars: letters, digits, _ . - "
                "(starting with a letter or digit)"
            )
        if shared.user_exists(name):
            raise StoreError(f"user already exists: {name}")
        if not passphrase:
            raise StoreError("passphrase must not be empty")

        priv = crypto.generate_private_key()
        config = {
            "v": crypto.FORMAT,
            "name": name,
            "priv": crypto.b64e(crypto.private_key_bytes(priv)),
            "contacts": {},        # name -> public key (b64)
            "groups": {},          # gid  -> {"name":..., "key": b64}
            "last_read": {},       # conv key -> unix timestamp
            "pending": [],         # invites we sent, awaiting a reply
        }

        shared.harden()
        _make_dir(shared.user_dir(name), USER_DIR_MODE)
        _make_dir(shared.invites_dir(name), DROP_DIR_MODE)
        _make_dir(shared.replies_dir(name), DROP_DIR_MODE)

        salt = crypto.random_bytes(crypto.SALT_LEN)
        iterations = crypto.PBKDF2_ITERATIONS
        key = crypto.derive_passphrase_key(passphrase, salt, iterations)
        session = Session(shared, name, key, config)
        session._salt = salt
        session._iterations = iterations
        session._write_identity()
        session.save_config()
        return session

    def _write_identity(self) -> None:
        identity = {
            "name": self.name,
            "v": crypto.FORMAT,
            "pub": crypto.b64e(self.pub),
        }
        if "legacy_pub" in self.config:
            # lets contacts who hold our 0.x key move to the new one
            identity["legacy_pub"] = self.config["legacy_pub"]
        _atomic_write(
            self.shared.identity_path(self.name),
            json.dumps(identity, indent=2).encode(),
            PUBLIC_FILE_MODE,
        )

    @staticmethod
    def login(shared: SharedDir, name: str, passphrase: str) -> "Session":
        shared.ensure_root()
        if not shared.user_exists(name):
            raise StoreError(f"no such user: {name}")
        try:
            with open(shared.config_path(name), "rb") as fh:
                wrapper = json.loads(fh.read())
            if not isinstance(wrapper, dict):
                raise ValueError("not a config wrapper")
        except (OSError, ValueError) as exc:
            raise StoreError("could not read config file") from exc
        version = wrapper.get("v")
        try:
            if version == crypto.FORMAT:
                salt = crypto.b64d(wrapper["salt"])
                iterations = int(wrapper["kdf"]["iterations"])
                key = crypto.derive_passphrase_key(passphrase, salt, iterations)
                config = json.loads(
                    crypto.sym_decrypt(
                        key, crypto.b64d(wrapper["blob"]), b"paf-config"
                    )
                )
                migrated = False
            elif version is None:
                config = Session._read_legacy_config(wrapper, passphrase)
                salt = crypto.random_bytes(crypto.SALT_LEN)
                iterations = crypto.PBKDF2_ITERATIONS
                key = crypto.derive_passphrase_key(passphrase, salt, iterations)
                Session._migrate_config(config)
                migrated = True
            else:
                raise StoreError(
                    f"config.enc is in format {version!r}, which this "
                    "phone_a_friend does not know - upgrade it"
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise StoreError("could not read config file") from exc
        except crypto.DecryptError as exc:
            raise StoreError("wrong passphrase") from exc
        session = Session(shared, name, key, config)
        session._salt = salt
        session._iterations = iterations
        shared.harden()
        if migrated:
            session.save_config()
        try:
            published = shared.read_identity(name).get("pub")
        except StoreError:
            published = None
        if published != crypto.b64e(session.pub):
            session._write_identity()
        if migrated:
            session.migrate()
        return session

    @staticmethod
    def _read_legacy_config(wrapper: dict, passphrase: str) -> dict:
        """Decrypt a config.enc written by 0.x (scrypt + ChaCha20-Poly1305)."""
        from . import legacy

        kdf = wrapper.get("kdf", {})
        try:
            key = legacy.derive_passphrase_key(
                passphrase,
                crypto.b64d(wrapper["salt"]),
                kdf.get("n", 2 ** 14),
                kdf.get("r", 8),
                kdf.get("p", 1),
            )
            return json.loads(
                legacy.sym_decrypt(key, crypto.b64d(wrapper["blob"]), b"paf-config")
            )
        except crypto.LegacyUnavailable as exc:
            raise StoreError(
                "this account was made by phone_a_friend 0.x, and moving it "
                "to the FIPS algorithms needs its old ones (scrypt, X25519, "
                "ChaCha20-Poly1305), which this OpenSSL refuses in FIPS mode: "
                "log in once where OpenSSL is not in FIPS mode "
                "(docs/security.rst, Migrating from 0.x)"
            ) from exc

    @staticmethod
    def _migrate_config(config: dict) -> None:
        """Give a 0.x config a P-256 identity, keeping its X25519 key and
        its contacts' X25519 keys only to read what 0.x wrote."""
        from . import legacy

        old = legacy.private_key_from_bytes(crypto.b64d(config["priv"]))
        config["legacy_priv"] = config["priv"]
        config["legacy_pub"] = crypto.b64e(legacy.public_key_bytes(old))
        config["legacy_contacts"] = dict(config.get("contacts", {}))
        config["priv"] = crypto.b64e(
            crypto.private_key_bytes(crypto.generate_private_key())
        )
        config["v"] = crypto.FORMAT

    def _legacy_priv(self):
        """Our 0.x X25519 key, or None for an account that never had one.
        Raises crypto.LegacyUnavailable where OpenSSL refuses X25519."""
        if self._legacy is None and "legacy_priv" in self.config:
            from . import legacy

            self._legacy = legacy.private_key_from_bytes(
                crypto.b64d(self.config["legacy_priv"])
            )
        return self._legacy

    def save_config(self) -> None:
        blob = crypto.sym_encrypt(
            self._cfg_key,
            json.dumps(self.config).encode(),
            b"paf-config",
        )
        wrapper = {
            "v": crypto.FORMAT,
            "salt": crypto.b64e(self._salt),
            "kdf": {"alg": crypto.KDF_NAME, "iterations": self._iterations},
            "aead": crypto.AEAD_NAME,
            "blob": crypto.b64e(blob),
        }
        _atomic_write(
            self.shared.config_path(self.name),
            json.dumps(wrapper).encode(),
            PRIVATE_FILE_MODE,
        )

    # --------------------------------------------------------- peer keys
    def _identity_pub(self, name: str) -> bytes:
        """A user's current public key, from their identity.json."""
        ident = self.shared.read_identity(name)
        if ident.get("v") != crypto.FORMAT:
            raise _not_upgraded(name)
        try:
            raw = crypto.b64d(ident["pub"])
        except ValueError as exc:
            raise StoreError(f"corrupt identity for {name}") from exc
        if not crypto.is_public_key(raw):
            raise StoreError(f"corrupt identity for {name}")
        return raw

    def _peer_pub(self, name: str, pub_b64: str) -> bytes:
        """The P-256 key to use for a peer we know by ``pub_b64``.

        A 0.x (X25519) key is swapped for the peer's new one once their
        identity.json says it replaced exactly that key. Raises StoreError
        when the peer has not upgraded, or the key is corrupt."""
        try:
            raw = crypto.b64d(pub_b64)
        except ValueError as exc:
            raise StoreError(f"corrupt key stored for {name}") from exc
        if crypto.is_public_key(raw):
            return raw
        if len(raw) != LEGACY_PUB_LEN:
            raise StoreError(f"corrupt key stored for {name}")
        try:
            ident = self.shared.read_identity(name)
        except StoreError:
            raise _not_upgraded(name)
        if ident.get("legacy_pub") != pub_b64:
            raise _not_upgraded(name)
        return self._identity_pub(name)

    def _contact_pub(self, name: str) -> bytes:
        """A contact's P-256 key, upgrading a stored 0.x key when it can."""
        pub_b64 = self.config["contacts"][name]  # _conv_location checked it
        raw = self._peer_pub(name, pub_b64)
        new_b64 = crypto.b64e(raw)
        if new_b64 != pub_b64:
            self.config["contacts"][name] = new_b64
            self.save_config()
        return raw

    def _same_key(self, name: str, a_b64: str, b_b64: str) -> bool:
        """Whether two keys for ``name`` are one key: equal, or a 0.x key
        and the P-256 key ``name``'s identity.json says replaced it."""
        if a_b64 == b_b64:
            return True
        try:
            return self._peer_pub(name, a_b64) == self._peer_pub(name, b_b64)
        except StoreError:
            return False

    def _stored_is(self, name: str, raw: bytes) -> bool:
        """Whether the key stored for contact ``name`` is ``raw``."""
        try:
            return self._peer_pub(name, self.config["contacts"][name]) == raw
        except StoreError:
            return False

    def _known_key_differs(self, name: str, pub_b64: str) -> bool:
        """True when ``name`` is a contact whose stored key is not this one."""
        stored = self.config["contacts"].get(name)
        return stored is not None and not self._same_key(name, stored, pub_b64)

    def _key_changed(self, name: str, pub_b64: str) -> str:
        """Record that ``name`` now has another key, without taking it, and
        return the notice for the user."""
        try:
            new = crypto.b64e(self._peer_pub(name, pub_b64))
        except StoreError:
            new = pub_b64
        if self.config["key_changes"].get(name) != new:
            self.config["key_changes"][name] = new
            self.save_config()
        return f"{name}'s key changed — /verify {name}"

    def _add_contact(self, name: str, pub_b64: str) -> None:
        """Store ``name``'s key. A contact's key is never replaced: the same
        key again changes nothing, and a different one is refused and
        recorded as a key change (StoreError)."""
        try:
            raw = crypto.b64d(pub_b64)
        except ValueError as exc:
            raise StoreError(f"corrupt key from {name}") from exc
        if name in self.config["contacts"]:
            if self._known_key_differs(name, pub_b64):
                raise StoreError(self._key_changed(name, pub_b64))
            return
        if len(raw) == LEGACY_PUB_LEN:
            self.config["legacy_contacts"][name] = pub_b64
        self.config["contacts"][name] = pub_b64

    def _forget(self, name: str) -> None:
        """Drop what we kept about a contact besides their key."""
        self.config["verified"].pop(name, None)
        self.config["key_changes"].pop(name, None)
        self._hidden_dm.pop(name, None)
        self._moved_dm.discard(name)
        self._ident_seen.pop(name, None)

    # ------------------------------------------------- who sent an invite
    def _is_identity_key(self, name: str, pub_b64) -> bool:
        """Whether ``pub_b64`` is the key ``name``'s identity.json publishes:
        its P-256 key, or a 0.x (X25519) key it lists as replaced
        (``legacy_pub``), or, while ``name`` is still on 0.x, its key."""
        try:
            raw = crypto.b64d(pub_b64)
            ident = self.shared.read_identity(name)
            if ident.get("v") == crypto.FORMAT:
                known = (
                    ident["pub"] if len(raw) != LEGACY_PUB_LEN
                    else ident.get("legacy_pub")
                )
            elif "v" not in ident:
                known = ident["pub"]
            else:
                return False
            return known is not None and crypto.b64d(known) == raw
        except (StoreError, ValueError, TypeError, AttributeError):
            return False

    def _owner_ok(self, path: str, name: str) -> bool:
        """Whether ``path`` is a plain file owned by the Unix account that
        owns ``name``'s identity.json. On a multi-user system no one else
        can make such a file; where everyone shares one account, every file
        passes, and this proves nothing."""
        try:
            st = os.lstat(path)
            owner = os.lstat(self.shared.identity_path(name)).st_uid
        except OSError:
            return False
        return stat.S_ISREG(st.st_mode) and st.st_uid == owner

    def _owner_proves(self, name: str) -> bool:
        """Whether a file's owner can show it is ``name``'s: true when
        ``name``'s identity.json belongs to another account than ours."""
        try:
            return os.lstat(self.shared.identity_path(name)).st_uid != os.getuid()
        except OSError:
            return False

    def _check_claim(self, payload: dict, kind: str, needs_key: bool) -> Optional[str]:
        """Why an invite or reply cannot be from the sender it names, or None:
        no such user, or (``needs_key``) a key that is not theirs."""
        what = WHAT[kind]
        sender = payload.get("from")
        if not isinstance(sender, str) or not SAFE_NAME.match(sender):
            return f"refused a {what} with no valid sender"
        if sender == self.name:
            return f"refused a {what} claiming to be from you"
        if not self.shared.user_exists(sender):
            return f"refused a {what} claiming to be from {sender}: no such user"
        if needs_key and not self._is_identity_key(sender, payload.get("from_pub")):
            return (
                f"refused a {what} claiming to be from {sender}: "
                f"the key in it is not {sender}'s"
            )
        return None

    def _check_sender(
        self, path: str, payload: dict, kind: str, needs_key: bool
    ) -> Optional[str]:
        """:meth:`_check_claim`, and the file's owner (:meth:`_owner_ok`)."""
        refusal = self._check_claim(payload, kind, needs_key)
        if refusal is None and not self._owner_ok(path, payload["from"]):
            sender = payload["from"]
            refusal = (
                f"refused a {WHAT[kind]} claiming to be from {sender}: "
                f"its file belongs to another account than {sender}'s"
            )
        return refusal

    # ------------------------------------------------------ signed replies
    def _known_pub(self, name: str) -> bytes:
        """The key we hold for ``name``: their stored key if they are a
        contact (never one from their identity.json that differs from it),
        else the one their identity.json publishes."""
        if name in self.config["contacts"]:
            return self._contact_pub(name)
        return self._identity_pub(name)

    def _auth_key(self, peer_pub: bytes, gid: Optional[str] = None) -> bytes:
        """The key a reply between us and ``peer_pub`` is signed with: from
        our ECDH secret, which only the two of us can compute, and for a
        group removal the group key as well, which only members hold."""
        secret = crypto.pair_key(self.priv, peer_pub)
        if gid is None:
            return crypto.hkdf(secret, b"paf-reply-auth-v1")
        return crypto.hkdf(
            secret + self._group_key(gid), b"paf-reply-auth-grp-v1|" + gid.encode()
        )

    def _signed(self, payload: dict, peer_pub: bytes, gid: Optional[str] = None) -> dict:
        tag = crypto.mac(self._auth_key(peer_pub, gid), _auth_data(payload))
        return dict(payload, auth=crypto.b64e(tag))

    def _auth_ok(self, payload: dict, kind: str) -> bool:
        """Whether a reply's signature is its sender's."""
        try:
            gid = payload.get("group_id") if kind == "group_removed" else None
            key = self._auth_key(self._known_pub(payload["from"]), gid)
            tag = crypto.b64d(payload["auth"])
        except (StoreError, ValueError, TypeError, AttributeError, KeyError):
            return False
        return crypto.mac_ok(key, _auth_data(payload), tag)

    def _check_reply(self, path: str, payload: dict, kind: str) -> Optional[str]:
        """Why a reply cannot be acted on, or None (see the module's
        docstring)."""
        refusal = self._check_sender(path, payload, kind, kind == "contact_accept")
        if refusal is not None:
            return refusal
        sender, what = payload["from"], WHAT[kind]
        if "auth" in payload:
            if not self._auth_ok(payload, kind):
                return (
                    f"refused a {what} claiming to be from {sender}: "
                    f"it is not signed with {sender}'s key"
                )
        elif kind in SIGNED and (
            kind == "group_removed" or not self._owner_proves(sender)
        ):
            return (
                f"ignored an unsigned {what} from {sender}: nothing shows it "
                f"is {sender}'s (phone_a_friend before 2.1 signs none)"
            )
        return None

    def take_notices(self) -> List[str]:
        """Notices :meth:`list_invites` queued (invites it refused), once.
        :meth:`process_replies` returns them too, last."""
        notices, self._notices = self._notices, []
        return notices

    # ----------------------------------------------------- safety numbers
    def check_contact_keys(self) -> List[str]:
        """Compare each contact's stored key with the one their identity.json
        publishes now. A different one is not taken: it is recorded, and the
        user is told once, until they compare safety numbers and accept it
        (:meth:`confirm_key`). A contact whose identity.json is back to the
        stored key is no longer marked. Returns notices."""
        notices = []
        for name in sorted(self.config["contacts"]):
            try:
                st = os.stat(self.shared.identity_path(name))
            except OSError:
                continue
            seen = (st.st_mtime_ns, st.st_size, st.st_ino,
                    self.config["contacts"][name])
            if self._ident_seen.get(name) == seen:
                continue
            self._ident_seen[name] = seen
            try:
                current = self._identity_pub(name)
            except StoreError:
                continue  # still on 0.x, or unreadable: nothing to compare
            if self._stored_is(name, current):
                if self.config["key_changes"].pop(name, None) is not None:
                    self.save_config()
            elif self.config["key_changes"].get(name) != crypto.b64e(current):
                notices.append(self._key_changed(name, crypto.b64e(current)))
        return notices

    def safety_number(self, name: str) -> Tuple[str, str, bool]:
        """(safety number, the key it is for, whether that is a new key the
        contact has not been moved to). The number is the same on both
        sides; comparing it out of band shows that each holds the other's
        real key. For a contact with a key change, it is for the new key in
        their identity.json."""
        if name not in self.config["contacts"]:
            raise StoreError(
                f"{name} is not a contact - send or accept an invite first"
            )
        changed = False
        if name in self.config["key_changes"]:
            their = self._identity_pub(name)
            changed = not self._stored_is(name, their)
        else:
            their = self._contact_pub(name)
        return crypto.safety_number(self.pub, their), crypto.b64e(their), changed

    def is_verified(self, name: str) -> bool:
        """Whether the user compared safety numbers for ``name``'s key."""
        key = self.config["verified"].get(name)
        return (
            key is not None
            and key == self.config["contacts"].get(name)
            and name not in self.config["key_changes"]
        )

    def confirm_key(self, name: str, key_b64: str) -> bool:
        """The user compared the safety number for ``key_b64`` (from
        :meth:`safety_number`) and it matched: mark ``name`` verified, moving
        them to that key if it is a new one. Returns True if they moved; a
        contact with a new key no longer has ours, so it is pushed to them
        again, as an invite."""
        _, current, changed = self.safety_number(name)
        if key_b64 != current:
            raise StoreError(f"{name}'s key changed again — /verify {name}")
        if changed:
            self.config["contacts"][name] = current
            self._hidden_dm.pop(name, None)
            self._moved_dm.discard(name)
            self._push_invite(name, crypto.b64d(current))
        self.config["key_changes"].pop(name, None)
        self.config["verified"][name] = current
        self.save_config()
        return changed

    # ------------------------------------------------------------- invites
    def invite_contact(self, other: str) -> None:
        """Push a chat invite (containing our public key) to another user."""
        if other == self.name:
            raise StoreError("cannot invite yourself")
        if other in self.config["contacts"]:
            raise StoreError(f"{other} is already a contact")
        if any(
            p["type"] == "contact" and p["to"] == other
            for p in self.config["pending"]
        ):
            raise StoreError(f"invite to {other} is already pending")
        self._push_invite(other, self._identity_pub(other))
        self.save_config()

    def _push_invite(self, other: str, pub: bytes) -> None:
        """Drop a chat invite with our key for ``other``, and remember it."""
        payload = {
            "type": "contact",
            "from": self.name,
            "from_pub": crypto.b64e(self.pub),
            "ts": time.time(),
        }
        self._drop_sealed(self.shared.invites_dir(other), crypto.b64e(pub), payload)
        pending = {"type": "contact", "to": other}
        if pending not in self.config["pending"]:
            self.config["pending"].append(pending)

    def invite_group(self, gid: str, other: str) -> None:
        """Push a group invite (containing the group key) to another user."""
        group = self.config["groups"].get(gid)
        if group is None:
            raise StoreError("you are not a member of that group")
        if other == self.name:
            raise StoreError("cannot invite yourself")
        pub = self._identity_pub(other)
        payload = {
            "type": "group",
            "from": self.name,
            "from_pub": crypto.b64e(self.pub),
            "group_id": gid,
            "group_name": group["name"],
            "group_key": group["key"],
            "ts": time.time(),
        }
        self._drop_sealed(self.shared.invites_dir(other), crypto.b64e(pub), payload)
        self.config["pending"].append({"type": "group", "to": other, "group_id": gid})
        self.save_config()

    def _drop_sealed(self, directory: str, pub_b64: str, payload: dict) -> None:
        _make_dir(directory, DROP_DIR_MODE)
        blob = crypto.seal(crypto.b64d(pub_b64), json.dumps(payload).encode())
        _atomic_write(
            os.path.join(directory, _msg_filename()),
            _wrap("sealed", blob),
            PUBLIC_FILE_MODE,
        )

    def list_invites(self) -> List[Tuple[str, dict]]:
        """Incoming invites we can decrypt and that are from who they say:
        [(filename, payload)]. Anything else is discarded, with a notice
        (:meth:`take_notices`) when it was refused."""
        out = []
        directory = self.shared.invites_dir(self.name)
        try:
            names = sorted(os.listdir(directory))
        except FileNotFoundError:
            return out
        for fname in names:
            if not fname.endswith(".json"):
                continue
            path = os.path.join(directory, fname)
            payload = self._read_sealed(path)
            if payload is None:
                continue
            kind = payload.get("type")
            if kind not in ("contact", "group") or not _well_formed(payload, kind):
                self._discard(path)
                continue
            refusal = self._check_sender(path, payload, kind, needs_key=True)
            if refusal is None and self._known_key_differs(
                payload["from"], payload["from_pub"]
            ):
                refusal = self._key_changed(payload["from"], payload["from_pub"])
            if refusal is not None:
                self._notices.append(refusal)
                self._discard(path)
                continue
            if kind == "contact" and payload["from"] in self.config["contacts"]:
                # stale invite from someone who is already a contact
                self._discard(path)
                continue
            if kind == "group" and payload.get("group_id") in self.config["groups"]:
                self._discard(path)
                continue
            out.append((fname, payload))
        return out

    def _read_sealed(self, path: str) -> Optional[dict]:
        """Open an invite or reply: the current format, or one 0.x sealed
        to our old key. Anything else (or undecryptable) is None."""
        try:
            with open(path, "rb") as fh:
                wrapper = json.loads(fh.read())
            version = wrapper.get("v")
            blob = crypto.b64d(wrapper["sealed"])
            if version == crypto.FORMAT:
                plain = crypto.unseal(self.priv, blob)
            elif version is None and "legacy_priv" in self.config:
                from . import legacy

                plain = legacy.unseal(self._legacy_priv(), blob)
            else:
                return None
            payload = json.loads(plain)
            return payload if isinstance(payload, dict) else None
        except (
            OSError, ValueError, KeyError, TypeError, AttributeError,
            crypto.DecryptError, crypto.LegacyUnavailable,
        ):
            return None

    @staticmethod
    def _discard(path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass

    def accept_invite(self, fname: str, payload: dict) -> str:
        """Accept an invite; returns a human-readable description."""
        sender = payload["from"]
        if payload["type"] not in ("contact", "group"):
            raise StoreError(f"unknown invite type: {payload['type']}")
        # list_invites checked the file; a payload can come from anywhere
        refusal = self._check_claim(payload, payload["type"], needs_key=True)
        if refusal is not None:
            raise StoreError(refusal)
        if self._known_key_differs(sender, payload["from_pub"]):
            raise StoreError(self._key_changed(sender, payload["from_pub"]))
        # where the reply goes: an invite 0.x sealed carries an X25519 key
        reply_pub = self._peer_pub(sender, payload["from_pub"])
        if payload["type"] == "contact":
            self._add_contact(sender, payload["from_pub"])
            desc = f"chat with {sender}"
            reply = {
                "type": "contact_accept",
                "from": self.name,
                "from_pub": crypto.b64e(self.pub),
            }
        else:
            gid = payload["group_id"]
            self.config["groups"][gid] = {
                "name": payload["group_name"],
                "key": payload["group_key"],
            }
            desc = f"group '{payload['group_name']}'"
            reply = {
                "type": "group_accept",
                "from": self.name,
                "group_id": gid,
                "group_name": payload["group_name"],
            }

        self.save_config()
        self._drop_sealed(
            self.shared.replies_dir(sender),
            crypto.b64e(reply_pub),
            self._signed(reply, reply_pub),
        )
        self._discard(os.path.join(self.shared.invites_dir(self.name), fname))
        return desc

    def decline_invite(self, fname: str, payload: dict) -> None:
        reply = {
            "type": f"{payload['type']}_decline",
            "from": self.name,
            "group_id": payload.get("group_id"),
        }
        from_pub = payload["from_pub"]
        pub = None
        # no answer to an invite that is not from who it says
        if self._check_claim(payload, payload["type"], needs_key=True) is None:
            try:
                pub = self._peer_pub(payload["from"], from_pub)
            except StoreError:
                pass  # a 0.x sender could not read the reply anyway
        if pub is not None:
            self._drop_sealed(
                self.shared.replies_dir(payload["from"]),
                crypto.b64e(pub),
                self._signed(reply, pub),
            )
        self._discard(os.path.join(self.shared.invites_dir(self.name), fname))

    def remove_contact(self, other: str) -> None:
        """Unfriend: forget the contact's key and ask them to forget ours.

        Message files are left in place — the pair key is derived from the
        two static identity keys, so a future re-invite makes the old
        conversation readable again."""
        pub_b64 = self.config["contacts"].pop(other, None)
        if pub_b64 is None:
            raise StoreError(f"{other} is not a contact")
        self._forget(other)
        self.save_config()
        try:
            pub = self._peer_pub(other, pub_b64)
        except StoreError:
            return  # a 0.x client could not read the notice anyway
        self._drop_sealed(
            self.shared.replies_dir(other),
            crypto.b64e(pub),
            self._signed({"type": "contact_removed", "from": self.name}, pub),
        )

    def remove_group_member(self, gid: str, other: str) -> None:
        """Remove a user from a group; any member can, mirroring invites.

        Removing yourself leaves the group. Removal is cooperative: the
        removed user's client discards the group key when it sees the
        notice — the key itself is not rotated. The notice is signed with
        the group key, so only a member can send one."""
        group = self.config["groups"].get(gid)
        if group is None:
            raise StoreError("you are not a member of that group")
        if other == self.name:
            del self.config["groups"][gid]
            self.save_config()
            return
        pub = self._identity_pub(other)
        notice = {
            "type": "group_removed",
            "from": self.name,
            "group_id": gid,
            "group_name": group["name"],
        }
        self._drop_sealed(
            self.shared.replies_dir(other),
            crypto.b64e(pub),
            self._signed(notice, pub, gid),
        )

    def process_replies(self) -> List[str]:
        """Consume invite replies addressed to us. Returns notices: what
        happened, then anything refused or changed (refusals, key changes,
        and the notices :meth:`list_invites` queued), so that the last one,
        which the TUI's status line keeps, is the one that matters most."""
        notices: List[str] = []
        warnings = self.check_contact_keys()
        directory = self.shared.replies_dir(self.name)
        try:
            names = sorted(os.listdir(directory))
        except FileNotFoundError:
            names = []
        changed = False
        for fname in names:
            if not fname.endswith(".json"):
                continue
            path = os.path.join(directory, fname)
            payload = self._read_sealed(path)
            if payload is None:
                continue
            kind = payload.get("type")
            relevant = (
                kind in REPLIES
                and _well_formed(payload, kind)
                and self._concerns_us(payload, kind)
            )
            refusal = self._check_reply(path, payload, kind) if relevant else None
            self._discard(path)
            if not relevant:
                continue  # unknown, malformed, or about nothing of ours
            if refusal is not None:
                warnings.append(refusal)
                continue
            sender = payload["from"]
            if kind == "contact_accept" and not self._has_pending("contact", sender):
                warnings.append(
                    f"ignored a chat acceptance from {sender}: "
                    f"you did not invite {sender}"
                )
                continue
            changed = True
            if kind == "contact_accept":
                self._clear_pending("contact", sender)
                if self._known_key_differs(sender, payload["from_pub"]):
                    warnings.append(self._key_changed(sender, payload["from_pub"]))
                else:
                    self._add_contact(sender, payload["from_pub"])
                    notices.append(f"{sender} accepted your chat invite")
            elif kind == "contact_decline":
                self._clear_pending("contact", sender)
                notices.append(f"{sender} declined your chat invite")
            elif kind == "group_accept":
                self._clear_pending("group", sender, payload.get("group_id"))
                notices.append(
                    f"{sender} joined group '{payload.get('group_name', '?')}'"
                )
            elif kind == "group_decline":
                self._clear_pending("group", sender, payload.get("group_id"))
                notices.append(f"{sender} declined a group invite")
            elif kind == "contact_removed":
                del self.config["contacts"][sender]
                self._forget(sender)
                notices.append(
                    f"{sender} ended your chat - a new invite can restore it"
                )
            else:  # group_removed
                group = self.config["groups"].pop(payload["group_id"])
                notices.append(f"{sender} removed you from group '{group['name']}'")
        if changed:
            self.save_config()
        # a key change can be seen twice in one pass: say it once
        return notices + list(dict.fromkeys(warnings + self.take_notices()))

    def _concerns_us(self, payload: dict, kind: str) -> bool:
        """Whether a reply is about something of ours: an invite we sent, a
        contact we have, a group we are in. One that is not is dropped
        unread, with no notice. An acceptance of an invite we never sent is
        the exception: it is checked, and the notice says so."""
        sender = payload.get("from")
        if kind == "contact_accept":
            return True
        if kind in ("contact_decline", "group_accept", "group_decline"):
            gid = None if kind == "contact_decline" else payload.get("group_id")
            return self._has_pending(kind.split("_")[0], sender, gid)
        if kind == "contact_removed":
            return sender in self.config["contacts"]
        return payload.get("group_id") in self.config["groups"]  # group_removed

    def _has_pending(self, kind: str, to, gid: Optional[str] = None) -> bool:
        """Whether we sent ``to`` an invite of this kind, still unanswered:
        in this session's config, or in the one on disk, which another
        device of ours may have written since we read it."""
        def match(pending):
            return any(
                isinstance(p, dict)
                and p.get("type") == kind
                and p.get("to") == to
                and (kind != "group" or p.get("group_id") == gid)
                for p in pending
            )

        return match(self.config["pending"]) or match(self._pending_on_disk())

    def _pending_on_disk(self) -> list:
        """The pending invites in our config.enc as it is on disk now, or []
        if it cannot be read with this session's key."""
        try:
            with open(self.shared.config_path(self.name), "rb") as fh:
                wrapper = json.loads(fh.read())
            config = json.loads(
                crypto.sym_decrypt(
                    self._cfg_key, crypto.b64d(wrapper["blob"]), b"paf-config"
                )
            )
            return list(config["pending"])
        except (
            OSError, ValueError, KeyError, TypeError, AttributeError,
            crypto.DecryptError,
        ):
            return []

    def _clear_pending(self, kind: str, to: str, gid: Optional[str] = None) -> None:
        self.config["pending"] = [
            p
            for p in self.config["pending"]
            if not (
                p["type"] == kind
                and p["to"] == to
                and (kind != "group" or p.get("group_id") == gid)
            )
        ]

    # -------------------------------------------------------------- groups
    def create_group(self, group_name: str) -> str:
        if not group_name.strip():
            raise StoreError("group name must not be empty")
        gid = crypto.random_bytes(8).hex()
        key = crypto.new_symmetric_key()
        _make_dir(self.shared.group_dir(gid), USER_DIR_MODE)
        _make_dir(self.shared.group_msgs_dir(gid), DROP_DIR_MODE)
        self.config["groups"][gid] = {
            "name": group_name.strip(),
            "key": crypto.b64e(key),
        }
        self._write_group_meta(
            gid, {"id": gid, "owner": self.name, "created": time.time()},
            group_name.strip(),
        )
        self.save_config()
        return gid

    # meta.json: the group's owner, when it was made, and (once deleted) when
    # it was deleted, in the clear; its name encrypted with a key derived from
    # the group key, so a listing of the shared directory does not show it.
    def _group_meta_key(self, gid: str) -> bytes:
        return crypto.hkdf(self._group_key(gid), b"paf-grp-meta-v2|" + gid.encode())

    def _write_group_meta(self, gid: str, meta: dict, name: str) -> None:
        meta = {k: v for k, v in meta.items() if k != "name"}
        meta["v"] = crypto.FORMAT
        meta["name_blob"] = crypto.b64e(
            crypto.sym_encrypt(
                self._group_meta_key(gid), name.encode(), b"paf-grp-meta|" + gid.encode()
            )
        )
        _atomic_write(
            os.path.join(self.shared.group_dir(gid), "meta.json"),
            json.dumps(meta, indent=2).encode(),
            PUBLIC_FILE_MODE,
        )

    def group_meta(self, gid: str) -> Optional[dict]:
        """The group's meta.json with its name decrypted into ``"name"``,
        or None when the group is gone or its meta cannot be read. A meta
        written before names were encrypted has its name in the clear."""
        try:
            meta = self.shared.read_group_meta(gid)
        except StoreError:
            return None
        if not isinstance(meta, dict):
            return None
        if "name_blob" in meta:
            try:
                meta["name"] = crypto.sym_decrypt(
                    self._group_meta_key(gid),
                    crypto.b64d(meta["name_blob"]),
                    b"paf-grp-meta|" + gid.encode(),
                ).decode()
            except (
                StoreError, ValueError, TypeError, AttributeError,
                crypto.DecryptError,
            ):
                return None
        if not isinstance(meta.get("name"), str):
            return None
        return meta

    def _owned_meta(self, gid: str, verb: str) -> dict:
        if gid not in self.config["groups"]:
            raise StoreError("you are not a member of that group")
        meta = self.group_meta(gid)
        if meta is None:
            raise StoreError("that group's metadata cannot be read")
        if meta.get("owner") != self.name:
            raise StoreError(
                f"only the group's owner, {meta.get('owner', '?')}, can {verb} it"
            )
        if meta.get("deleted_at") is not None:
            raise StoreError("that group has been deleted")
        return meta

    def rename_group(self, gid: str, new_name: str) -> None:
        """Rename a group (its owner only); members see it on their next poll."""
        new_name = new_name.strip()
        if not new_name:
            raise StoreError("group name must not be empty")
        meta = self._owned_meta(gid, "rename")
        self._write_group_meta(gid, meta, new_name)
        self.config["groups"][gid]["name"] = new_name
        self.save_config()

    def delete_group(self, gid: str) -> float:
        """Delete a group (its owner only). It stays readable, and read-only,
        for GROUP_GRACE; returns the time after which it is removed."""
        meta = self._owned_meta(gid, "delete")
        meta["deleted_at"] = time.time()
        self._write_group_meta(gid, meta, meta["name"])
        self.config["groups"][gid]["deleted_at"] = meta["deleted_at"]
        self.save_config()
        return meta["deleted_at"] + GROUP_GRACE

    def group_deleted_until(self, gid: str) -> Optional[float]:
        """When a deleted group goes away, or None if it is not deleted."""
        deleted = self.config["groups"].get(gid, {}).get("deleted_at")
        return None if deleted is None else deleted + GROUP_GRACE

    def sync_groups(self) -> List[str]:
        """Bring every group up to date with its meta.json: renames,
        deletions, and removal once a deleted group's grace period is over.
        Returns notices for the user."""
        notices = []
        changed = False
        now = time.time()
        for gid in list(self.config["groups"]):
            group = self.config["groups"][gid]
            meta = self.group_meta(gid)
            if meta is None:
                if not os.path.isdir(self.shared.group_dir(gid)):
                    del self.config["groups"][gid]
                    notices.append(f"group #{group['name']} is gone")
                    changed = True
                continue
            if meta["name"] != group["name"]:
                notices.append(f"#{group['name']} is now #{meta['name']}")
                group["name"] = meta["name"]
                changed = True
            if "name_blob" not in meta and meta.get("owner") == self.name:
                self._write_group_meta(gid, meta, meta["name"])  # encrypt it
            deleted = meta.get("deleted_at")
            if isinstance(deleted, (int, float)) and group.get("deleted_at") != deleted:
                group["deleted_at"] = deleted
                notices.append(
                    f"#{group['name']} was deleted - readable until "
                    + time.strftime("%Y-%m-%d %H:%M", time.localtime(deleted + GROUP_GRACE))
                )
                changed = True
            if isinstance(deleted, (int, float)) and now >= deleted + GROUP_GRACE:
                del self.config["groups"][gid]
                self.cleanup_group(gid)
                notices.append(f"deleted group #{group['name']} removed")
                changed = True
        if changed:
            self.save_config()
        return notices

    def cleanup_group(self, gid: str) -> bool:
        """Remove a group's directory: rename it aside, so a client in the
        middle of reading sees it gone at once, then delete it. Two clients
        doing this at once are harmless: one rename wins, and the other
        finds nothing to do. Returns True if this client removed it."""
        aside = os.path.join(
            self.shared.root, "groups", f".deleted-{gid}-{secrets.token_hex(4)}"
        )
        try:
            os.rename(self.shared.group_dir(gid), aside)
        except OSError:
            return False  # gone already, or not ours to move
        shutil.rmtree(aside, ignore_errors=True)
        return True

    def group_by_name(self, name: str) -> Optional[str]:
        for gid, g in self.config["groups"].items():
            if g["name"] == name or gid == name:
                return gid
        return None

    # ------------------------------------------------------------ messages
    def _conv_location(self, kind: str, target: str) -> Tuple[str, bytes]:
        """Resolve (directory, aad) for a conversation we belong to."""
        if kind == "dm":
            if target not in self.config["contacts"]:
                raise StoreError(
                    f"no key exchanged with {target} - send or accept an invite first"
                )
            directory, legacy = self._dm_dirs(target)
            # the AAD keeps naming the pair, so files decrypt in either place
            aad = os.path.basename(self.shared.dm_dir(self.name, target)).encode()
            return directory, b"paf-dm|" + aad
        if kind == "grp":
            if target not in self.config["groups"]:
                raise StoreError("no key for that group - accept an invite first")
            return self.shared.group_msgs_dir(target), b"paf-grp|" + target.encode()
        raise StoreError(f"bad conversation kind: {kind}")

    # DM directories are named dm/<32 hex characters> derived from the pair
    # key, which only the two parties can compute, so a listing does not
    # show who talks to whom (PAF-008). Until a contact has a P-256 key the
    # directory keeps its old name, dm/<a>__<b>.
    def _dm_dirs(self, target: str) -> Tuple[str, Optional[str]]:
        """(directory to write, old-named directory to read too, or None)."""
        legacy = self.shared.dm_dir(self.name, target)
        hidden = self._hidden_dm.get(target)
        if hidden is None:
            try:
                key = crypto.pair_key(self.priv, self._contact_pub(target))
            except StoreError:
                return legacy, None
            name = crypto.hkdf(key, b"paf-dm-dir-v2").hex()[:32]
            hidden = self._hidden_dm[target] = os.path.join(
                self.shared.root, "dm", name
            )
        return hidden, legacy

    def dm_directory(self, target: str) -> str:
        """Where new direct messages with ``target`` go."""
        return self._conv_location("dm", target)[0]

    def _message_dirs(self, kind: str, target: str) -> List[str]:
        directory, _ = self._conv_location(kind, target)
        if kind != "dm":
            return [directory]
        hidden, legacy = self._dm_dirs(target)
        if legacy is not None:
            self._move_own_dm_files(target, hidden, legacy)
            return [hidden, legacy]
        return [hidden]

    def _move_own_dm_files(self, target: str, hidden: str, legacy: str) -> None:
        """Move the message files we own from dm/<a>__<b> into the hidden
        directory (the sticky bit lets only a file's owner move it), once a
        session, and remove the old directory when it is empty."""
        if target in self._moved_dm:
            return
        self._moved_dm.add(target)
        try:
            names = os.listdir(legacy)
        except FileNotFoundError:
            return
        _make_dir(hidden, DROP_DIR_MODE)
        uid = os.getuid()
        for fname in names:
            path = os.path.join(legacy, fname)
            try:
                if fname.endswith(".json") and os.lstat(path).st_uid == uid:
                    os.rename(path, os.path.join(hidden, fname))
            except OSError:
                continue  # not ours to move: its sender's client will
        try:
            os.rmdir(legacy)
        except OSError:
            pass  # the peer's files are still there

    def _message_path(self, dirs: List[str], fname: str) -> str:
        for directory in dirs[1:]:
            path = os.path.join(directory, fname)
            if os.path.exists(path):
                return path
        return os.path.join(dirs[0], fname)

    def _group_key(self, gid: str) -> bytes:
        try:
            key = crypto.b64d(self.config["groups"][gid]["key"])
        except ValueError as exc:
            raise StoreError("corrupt key stored for that group") from exc
        if len(key) != crypto.KEY_LEN:
            raise StoreError("corrupt key stored for that group")
        return key

    def _conv(self, kind: str, target: str) -> Tuple[str, bytes, bytes]:
        """Resolve (directory, key, aad) for a conversation's new messages."""
        directory, aad = self._conv_location(kind, target)
        if kind == "dm":
            key = crypto.pair_key(self.priv, self._contact_pub(target))
        else:
            key = crypto.group_message_key(self._group_key(target), target)
        return directory, key, aad

    def _legacy_key(self, kind: str, target: str) -> Optional[bytes]:
        """The key 0.x used for this conversation, or None if there is none
        (or this OpenSSL refuses the 0.x algorithms)."""
        try:
            if kind == "grp":
                return self._group_key(target)
            their = self.config["legacy_contacts"].get(target)
            if their is None or "legacy_priv" not in self.config:
                return None
            from . import legacy

            return legacy.pair_key(self._legacy_priv(), crypto.b64d(their))
        except (StoreError, ValueError, crypto.LegacyUnavailable):
            return None

    def send_message(self, kind: str, target: str, text: str) -> dict:
        directory, key, aad = self._conv(kind, target)
        if kind == "grp" and self.group_deleted_until(target) is not None:
            raise StoreError("that group has been deleted - it is read-only")
        _make_dir(directory, DROP_DIR_MODE)
        msg = {"from": self.name, "ts": time.time(), "text": text}
        blob = crypto.sym_encrypt(key, json.dumps(msg).encode(), aad)
        _atomic_write(
            os.path.join(directory, _msg_filename()),
            _wrap("blob", blob),
            PUBLIC_FILE_MODE,
        )
        return msg

    def list_message_files(self, kind: str, target: str) -> List[str]:
        names = set()
        for directory in self._message_dirs(kind, target):
            try:
                names.update(f for f in os.listdir(directory) if f.endswith(".json"))
            except FileNotFoundError:
                continue
        return sorted(names)

    def load_messages(
        self, kind: str, target: str, filenames: List[str]
    ) -> Dict[str, dict]:
        """Decrypt the given message files; undecryptable files are skipped.

        A message 0.x wrote is read with the 0.x key; if it is one of ours
        and the conversation has a current key, it is re-encrypted in place
        (only its owner may replace a file in the sticky directory)."""
        _, aad = self._conv_location(kind, target)
        dirs = self._message_dirs(kind, target)
        try:
            key: Optional[bytes] = self._conv(kind, target)[1]
        except StoreError:
            key = None  # e.g. a contact who has not upgraded yet
        legacy_key = None
        out: Dict[str, dict] = {}
        for fname in filenames:
            path = self._message_path(dirs, fname)
            try:
                with open(path, "rb") as fh:
                    wrapper = json.loads(fh.read())
                version = wrapper.get("v")
                blob = crypto.b64d(wrapper["blob"])
                if version == crypto.FORMAT and key is not None:
                    plain = crypto.sym_decrypt(key, blob, aad)
                elif version is None:
                    if legacy_key is None:
                        legacy_key = self._legacy_key(kind, target)
                    if legacy_key is None:
                        continue
                    from . import legacy

                    plain = legacy.sym_decrypt(legacy_key, blob, aad)
                else:
                    continue
                msg = json.loads(plain)
                if isinstance(msg, dict) and "text" in msg:
                    out[fname] = msg
                    if (
                        version is None
                        and key is not None
                        and msg.get("from") == self.name
                    ):
                        self._reencrypt(path, key, plain, aad)
            except (
                OSError, ValueError, KeyError, TypeError, AttributeError,
                crypto.DecryptError, crypto.LegacyUnavailable,
            ):
                continue
        return out

    @staticmethod
    def _reencrypt(path: str, key: bytes, plain: bytes, aad: bytes) -> None:
        blob = crypto.sym_encrypt(key, plain, aad)
        try:
            _atomic_write(path, _wrap("blob", blob), PUBLIC_FILE_MODE)
        except OSError:
            pass  # not our file after all; it stays as it was

    def migrate(self) -> List[Tuple[str, int]]:
        """Re-encrypt every 0.x message of ours that can be, now.

        Returns (conversation, files still in the 0.x format) for each
        conversation that has any left: messages from a contact who has not
        upgraded yet, or anything this OpenSSL cannot read."""
        left = []
        convs = [("dm", c, c) for c in sorted(self.config["contacts"])] + [
            ("grp", gid, "#" + g["name"])
            for gid, g in sorted(self.config["groups"].items())
        ]
        for kind, target, label in convs:
            dirs = self._message_dirs(kind, target)
            files = self.list_message_files(kind, target)
            self.load_messages(kind, target, files)
            remaining = 0
            for fname in files:
                try:
                    with open(self._message_path(dirs, fname), "rb") as fh:
                        wrapper = json.loads(fh.read())
                    remaining += isinstance(wrapper, dict) and "v" not in wrapper
                except (OSError, ValueError):
                    continue
            if remaining:
                left.append((label, remaining))
        return left

    # -------------------------------------------------------------- beacons
    # One small file per user and device, beacons/<name>.<device>.json. Its
    # mtime is the user's presence. Inside, one entry per conversation,
    # encrypted with a key derived from that conversation's key and filed
    # under a tag derived from it too, so only people in the conversation
    # can tell which conversation an entry is for or read it: the user's
    # read marker there, and when they last typed in it.

    @property
    def device(self) -> str:
        """This machine, as an opaque 8-character id (not its hostname)."""
        return hashlib.sha256(socket.gethostname().encode()).hexdigest()[:8]

    @property
    def shares_presence(self) -> bool:
        return self.config.get("share_presence", True)

    def set_share_presence(self, on: bool) -> None:
        """Turn beacons on or off. Off removes this user's beacon files."""
        self.config["share_presence"] = bool(on)
        self.save_config()
        if not on:
            prefix = self.name + "."
            try:
                names = os.listdir(self.shared.beacons_dir())
            except FileNotFoundError:
                names = []
            for fname in names:
                if fname.startswith(prefix) and fname.endswith(".json"):
                    self._discard(os.path.join(self.shared.beacons_dir(), fname))

    def _beacon_keys(self, kind: str, target: str, user: str) -> Tuple[str, bytes]:
        """(tag, key) of ``user``'s beacon entry for a conversation."""
        root = self._conv(kind, target)[1]
        tag = crypto.hkdf(root, b"paf-beacon-tag-v2|" + user.encode())[:16].hex()
        key = crypto.hkdf(root, b"paf-beacon-v2|" + user.encode())
        return tag, key

    def conversations(self) -> List[Tuple[str, str]]:
        """Every conversation we are in: ("dm", contact) and ("grp", gid)."""
        return [("dm", c) for c in sorted(self.config["contacts"])] + [
            ("grp", g) for g in sorted(self.config["groups"])
        ]

    def write_beacon(
        self, typing: Optional[Tuple[str, str]] = None, now: Optional[float] = None
    ) -> bool:
        """Write this device's beacon; False (writing nothing) when presence
        sharing is off. ``typing`` is the conversation being typed in."""
        if not self.shares_presence:
            return False
        now = time.time() if now is None else now
        entries = {}
        for kind, target in self.conversations():
            try:
                tag, key = self._beacon_keys(kind, target, self.name)
            except StoreError:
                continue  # e.g. a contact still on 0.x
            state = {
                "read": self.config["last_read"].get(self.conv_key(kind, target), 0.0)
            }
            if typing == (kind, target):
                state["typing_at"] = now
            entries[tag] = crypto.b64e(
                crypto.sym_encrypt(
                    key, json.dumps(state).encode(), b"paf-beacon|" + tag.encode()
                )
            )
        _make_dir(self.shared.beacons_dir(), DROP_DIR_MODE)
        body = {"v": crypto.FORMAT, "entries": entries}
        _atomic_write(
            os.path.join(self.shared.beacons_dir(), f"{self.name}.{self.device}.json"),
            json.dumps(body).encode(),
            PUBLIC_FILE_MODE,
        )
        return True

    def read_beacons(self, cache: Dict[str, Tuple[float, dict]]) -> Dict[str, dict]:
        """Everyone else's beacons, as {user: {"mtime": newest, "convs":
        {conv_key: {"read": t, "typing_at": t}}}}.

        One listdir and a stat per file; a file is read only when its mtime
        differs from the one in ``cache`` (which this updates), so a tick in
        which nothing changed reads nothing."""
        directory = self.shared.beacons_dir()
        try:
            names = os.listdir(directory)
        except FileNotFoundError:
            names = []
        seen = set()
        for fname in names:
            parts = fname.rsplit(".", 2)
            if len(parts) != 3 or parts[2] != "json" or parts[0] == self.name:
                continue
            path = os.path.join(directory, fname)
            try:
                mtime = os.stat(path).st_mtime
            except OSError:
                continue
            seen.add(fname)
            if fname in cache and cache[fname][0] == mtime:
                continue
            cache[fname] = (mtime, self._decode_beacon(parts[0], path))
        for fname in set(cache) - seen:
            del cache[fname]
        out: Dict[str, dict] = {}
        for fname, (mtime, convs) in cache.items():
            user = fname.rsplit(".", 2)[0]
            entry = out.setdefault(user, {"mtime": 0.0, "convs": {}})
            entry["mtime"] = max(entry["mtime"], mtime)
            for conv, state in convs.items():
                merged = entry["convs"].setdefault(conv, {})
                for field in ("read", "typing_at"):
                    if field in state:
                        merged[field] = max(merged.get(field, 0.0), state[field])
        return out

    def _decode_beacon(self, user: str, path: str) -> dict:
        try:
            with open(path, "rb") as fh:
                body = json.loads(fh.read())
            if body.get("v") != crypto.FORMAT:
                return {}
            entries = body["entries"]
        except (OSError, ValueError, KeyError, AttributeError):
            return {}
        convs = {}
        for kind, target in self.conversations():
            if kind == "dm" and target != user:
                continue
            try:
                tag, key = self._beacon_keys(kind, target, user)
                if tag not in entries:
                    continue
                state = json.loads(
                    crypto.sym_decrypt(
                        key, crypto.b64d(entries[tag]), b"paf-beacon|" + tag.encode()
                    )
                )
                convs[self.conv_key(kind, target)] = {
                    k: float(state[k]) for k in ("read", "typing_at") if k in state
                }
            except (StoreError, ValueError, TypeError, AttributeError,
                    crypto.DecryptError):
                continue
        return convs

    @staticmethod
    def presence(beacon: Optional[dict], now: Optional[float] = None) -> str:
        """"active", "idle" or "away" from a read_beacons() entry."""
        if not beacon:
            return "away"
        age = (time.time() if now is None else now) - beacon["mtime"]
        if age < PRESENCE_ACTIVE:
            return "active"
        if age < PRESENCE_IDLE:
            return "idle"
        return "away"

    # --------------------------------------------------------------- search
    @staticmethod
    def matches(text: str, terms: List[str]) -> bool:
        """True when ``text`` contains every term, ignoring case."""
        folded = text.casefold()
        return all(term.casefold() in folded for term in terms)

    def search_conversation(
        self,
        kind: str,
        target: str,
        terms: List[str],
        cache: Optional[Dict[str, Dict[str, dict]]] = None,
    ) -> List[Tuple[str, dict]]:
        """[(filename, message)] in one conversation containing every term,
        newest first. ``cache`` (conv key -> {filename: message}, the TUI's
        own) is filled as messages are decrypted, so a second search
        decrypts nothing. The plaintext stays in memory: nothing is
        written."""
        cache = {} if cache is None else cache
        seen = cache.setdefault(self.conv_key(kind, target), {})
        try:
            files = self.list_message_files(kind, target)
            new = [f for f in files if f not in seen]
            if new:
                seen.update(self.load_messages(kind, target, new))
        except StoreError:
            return []
        hits = [
            (fname, msg) for fname, msg in seen.items()
            if isinstance(msg.get("text"), str) and self.matches(msg["text"], terms)
        ]
        return sorted(hits, key=lambda h: h[0], reverse=True)

    def search(
        self, terms: List[str], cache: Optional[Dict[str, Dict[str, dict]]] = None
    ) -> List[Tuple[str, str, List[Tuple[str, dict]]]]:
        """Every conversation with hits, as (kind, target, hits), the one
        with the newest hit first."""
        found = []
        for kind, target in self.conversations():
            hits = self.search_conversation(kind, target, terms, cache)
            if hits:
                found.append((kind, target, hits))
        return sorted(found, key=lambda c: c[2][0][0], reverse=True)

    # ----------------------------------------------------------- read state
    @staticmethod
    def conv_key(kind: str, target: str) -> str:
        return f"{kind}:{target}"

    def unread_count(self, kind: str, target: str) -> int:
        try:
            files = self.list_message_files(kind, target)
        except StoreError:
            return 0
        last = self.config["last_read"].get(self.conv_key(kind, target), 0.0)
        return sum(1 for f in files if _ts_of(f) > last)

    def mark_read(self, kind: str, target: str) -> bool:
        """Advance the read marker to the newest message; True if changed."""
        try:
            files = self.list_message_files(kind, target)
        except StoreError:
            return False
        newest = max((_ts_of(f) for f in files), default=0.0)
        ck = self.conv_key(kind, target)
        if newest > self.config["last_read"].get(ck, 0.0):
            self.config["last_read"][ck] = newest
            self.save_config()
            return True
        return False
