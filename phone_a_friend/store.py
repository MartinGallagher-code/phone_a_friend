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
      dm/<a>__<b>/<ts>-<rand>.json   direct messages (pair-key encrypted)
      groups/<gid>/meta.json         public group metadata
      groups/<gid>/msgs/<...>.json   group messages (group-key encrypted)

Access control = filesystem permissions on the shared directory plus
encryption: a message can only be decrypted by holders of the right keys.

Every encrypted record (``config.enc``, invites, replies and messages) is a
JSON wrapper with a ``"v"`` field naming its format, currently
:data:`crypto.FORMAT`. Records written by 0.x have no ``"v"``; they are read
through :mod:`phone_a_friend.legacy` and migrated (see :meth:`Session.login`
and :meth:`Session.migrate`). A record with a ``"v"`` this client does not
know is never guessed at: it is skipped, or refused for ``config.enc``.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from typing import Dict, List, Optional, Tuple

from . import crypto

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")

# Directories other users must be able to drop files into. The sticky bit
# keeps writers from deleting each other's files while still letting the
# directory owner clean up.
DROP_DIR_MODE = 0o1777
USER_DIR_MODE = 0o755
PUBLIC_FILE_MODE = 0o644
PRIVATE_FILE_MODE = 0o600


# 0.x identity keys were raw X25519 public keys, 32 bytes long.
LEGACY_PUB_LEN = 32


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
        lo, hi = sorted([a, b])
        return os.path.join(self.root, "dm", f"{lo}__{hi}")

    def group_dir(self, gid: str) -> str:
        return os.path.join(self.root, "groups", gid)

    def group_msgs_dir(self, gid: str) -> str:
        return os.path.join(self.group_dir(gid), "msgs")

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
        self.priv = crypto.private_key_from_bytes(crypto.b64d(config["priv"]))
        self.pub = crypto.public_key_bytes(self.priv)
        self._legacy = None  # the 0.x X25519 key, loaded when first needed

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

    def _add_contact(self, name: str, pub_b64: str) -> None:
        try:
            raw = crypto.b64d(pub_b64)
        except ValueError as exc:
            raise StoreError(f"corrupt key from {name}") from exc
        if len(raw) == LEGACY_PUB_LEN:
            self.config["legacy_contacts"][name] = pub_b64
        self.config["contacts"][name] = pub_b64

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
        pub = self._identity_pub(other)
        payload = {
            "type": "contact",
            "from": self.name,
            "from_pub": crypto.b64e(self.pub),
            "ts": time.time(),
        }
        self._drop_sealed(self.shared.invites_dir(other), crypto.b64e(pub), payload)
        self.config["pending"].append({"type": "contact", "to": other})
        self.save_config()

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
        """Incoming invites we can decrypt: [(filename, payload)]."""
        out = []
        directory = self.shared.invites_dir(self.name)
        try:
            names = sorted(os.listdir(directory))
        except FileNotFoundError:
            return out
        for fname in names:
            if not fname.endswith(".json"):
                continue
            payload = self._read_sealed(os.path.join(directory, fname))
            if payload is None:
                continue
            if payload.get("type") == "contact" and payload.get("from") in self.config["contacts"]:
                # stale invite from someone who is already a contact
                self._discard(os.path.join(directory, fname))
                continue
            if payload.get("type") == "group" and payload.get("group_id") in self.config["groups"]:
                self._discard(os.path.join(directory, fname))
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
            self.shared.replies_dir(sender), crypto.b64e(reply_pub), reply
        )
        self._discard(os.path.join(self.shared.invites_dir(self.name), fname))
        return desc

    def decline_invite(self, fname: str, payload: dict) -> None:
        reply = {
            "type": f"{payload['type']}_decline",
            "from": self.name,
            "group_id": payload.get("group_id"),
        }
        try:
            pub = self._peer_pub(payload["from"], payload["from_pub"])
        except StoreError:
            pub = None  # a 0.x sender could not read the reply anyway
        if pub is not None:
            self._drop_sealed(
                self.shared.replies_dir(payload["from"]), crypto.b64e(pub), reply
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
        self.save_config()
        try:
            pub = self._peer_pub(other, pub_b64)
        except StoreError:
            return  # a 0.x client could not read the notice anyway
        self._drop_sealed(
            self.shared.replies_dir(other),
            crypto.b64e(pub),
            {"type": "contact_removed", "from": self.name},
        )

    def remove_group_member(self, gid: str, other: str) -> None:
        """Remove a user from a group; any member can, mirroring invites.

        Removing yourself leaves the group. Removal is cooperative: the
        removed user's client discards the group key when it sees the
        notice — the key itself is not rotated."""
        group = self.config["groups"].get(gid)
        if group is None:
            raise StoreError("you are not a member of that group")
        if other == self.name:
            del self.config["groups"][gid]
            self.save_config()
            return
        pub = self._identity_pub(other)
        self._drop_sealed(
            self.shared.replies_dir(other),
            crypto.b64e(pub),
            {
                "type": "group_removed",
                "from": self.name,
                "group_id": gid,
                "group_name": group["name"],
            },
        )

    def process_replies(self) -> List[str]:
        """Consume invite replies addressed to us; returns notice strings."""
        notices = []
        directory = self.shared.replies_dir(self.name)
        try:
            names = sorted(os.listdir(directory))
        except FileNotFoundError:
            return notices
        changed = False
        for fname in names:
            if not fname.endswith(".json"):
                continue
            path = os.path.join(directory, fname)
            payload = self._read_sealed(path)
            if payload is None:
                continue
            sender = payload.get("from", "?")
            kind = payload.get("type")
            if kind == "contact_accept":
                self._add_contact(sender, payload["from_pub"])
                self._clear_pending("contact", sender)
                notices.append(f"{sender} accepted your chat invite")
                changed = True
            elif kind == "contact_decline":
                self._clear_pending("contact", sender)
                notices.append(f"{sender} declined your chat invite")
                changed = True
            elif kind == "group_accept":
                self._clear_pending("group", sender, payload.get("group_id"))
                notices.append(
                    f"{sender} joined group '{payload.get('group_name', '?')}'"
                )
                changed = True
            elif kind == "group_decline":
                self._clear_pending("group", sender, payload.get("group_id"))
                notices.append(f"{sender} declined a group invite")
                changed = True
            elif kind == "contact_removed":
                if self.config["contacts"].pop(sender, None) is not None:
                    notices.append(
                        f"{sender} ended your chat - a new invite can restore it"
                    )
                    changed = True
            elif kind == "group_removed":
                group = self.config["groups"].pop(payload.get("group_id"), None)
                if group is not None:
                    notices.append(
                        f"{sender} removed you from group '{group['name']}'"
                    )
                    changed = True
            self._discard(path)
        if changed:
            self.save_config()
        return notices

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
        meta = {
            "id": gid,
            "name": group_name.strip(),
            "owner": self.name,
            "created": time.time(),
        }
        _atomic_write(
            os.path.join(self.shared.group_dir(gid), "meta.json"),
            json.dumps(meta, indent=2).encode(),
            PUBLIC_FILE_MODE,
        )
        self.config["groups"][gid] = {
            "name": group_name.strip(),
            "key": crypto.b64e(key),
        }
        self.save_config()
        return gid

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
            directory = self.shared.dm_dir(self.name, target)
            aad = os.path.basename(directory).encode()
            return directory, b"paf-dm|" + aad
        if kind == "grp":
            if target not in self.config["groups"]:
                raise StoreError("no key for that group - accept an invite first")
            return self.shared.group_msgs_dir(target), b"paf-grp|" + target.encode()
        raise StoreError(f"bad conversation kind: {kind}")

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
        directory, _ = self._conv_location(kind, target)
        try:
            return sorted(
                f for f in os.listdir(directory) if f.endswith(".json")
            )
        except FileNotFoundError:
            return []

    def load_messages(
        self, kind: str, target: str, filenames: List[str]
    ) -> Dict[str, dict]:
        """Decrypt the given message files; undecryptable files are skipped.

        A message 0.x wrote is read with the 0.x key; if it is one of ours
        and the conversation has a current key, it is re-encrypted in place
        (only its owner may replace a file in the sticky directory)."""
        directory, aad = self._conv_location(kind, target)
        try:
            key: Optional[bytes] = self._conv(kind, target)[1]
        except StoreError:
            key = None  # e.g. a contact who has not upgraded yet
        legacy_key = None
        out: Dict[str, dict] = {}
        for fname in filenames:
            path = os.path.join(directory, fname)
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
            directory, _ = self._conv_location(kind, target)
            files = self.list_message_files(kind, target)
            self.load_messages(kind, target, files)
            remaining = 0
            for fname in files:
                try:
                    with open(os.path.join(directory, fname), "rb") as fh:
                        wrapper = json.loads(fh.read())
                    remaining += isinstance(wrapper, dict) and "v" not in wrapper
                except (OSError, ValueError):
                    continue
            if remaining:
                left.append((label, remaining))
        return left

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
