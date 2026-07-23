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


class StoreError(Exception):
    pass


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
        self.config = config
        self.priv = crypto.private_key_from_bytes(crypto.b64d(config["priv"]))
        self.pub = crypto.public_key_bytes(self.priv)

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

        identity = {"name": name, "pub": crypto.b64e(crypto.public_key_bytes(priv))}
        _atomic_write(
            shared.identity_path(name),
            json.dumps(identity, indent=2).encode(),
            PUBLIC_FILE_MODE,
        )

        salt = os.urandom(crypto.SALT_LEN)
        key = crypto.derive_passphrase_key(passphrase, salt)
        session = Session(shared, name, key, config)
        session._salt = salt
        session.save_config()
        return session

    @staticmethod
    def login(shared: SharedDir, name: str, passphrase: str) -> "Session":
        shared.ensure_root()
        if not shared.user_exists(name):
            raise StoreError(f"no such user: {name}")
        try:
            with open(shared.config_path(name), "rb") as fh:
                wrapper = json.loads(fh.read())
        except (OSError, ValueError) as exc:
            raise StoreError("could not read config file") from exc
        salt = crypto.b64d(wrapper["salt"])
        key = crypto.derive_passphrase_key(passphrase, salt)
        try:
            config = json.loads(
                crypto.sym_decrypt(key, crypto.b64d(wrapper["blob"]), b"paf-config")
            )
        except crypto.DecryptError as exc:
            raise StoreError("wrong passphrase") from exc
        session = Session(shared, name, key, config)
        session._salt = salt
        return session

    def save_config(self) -> None:
        blob = crypto.sym_encrypt(
            self._cfg_key,
            json.dumps(self.config).encode(),
            b"paf-config",
        )
        wrapper = {
            "salt": crypto.b64e(self._salt),
            "kdf": {"n": crypto.SCRYPT_N, "r": crypto.SCRYPT_R, "p": crypto.SCRYPT_P},
            "blob": crypto.b64e(blob),
        }
        _atomic_write(
            self.shared.config_path(self.name),
            json.dumps(wrapper).encode(),
            PRIVATE_FILE_MODE,
        )

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
        ident = self.shared.read_identity(other)
        payload = {
            "type": "contact",
            "from": self.name,
            "from_pub": crypto.b64e(self.pub),
            "ts": time.time(),
        }
        self._drop_sealed(self.shared.invites_dir(other), ident["pub"], payload)
        self.config["pending"].append({"type": "contact", "to": other})
        self.save_config()

    def invite_group(self, gid: str, other: str) -> None:
        """Push a group invite (containing the group key) to another user."""
        group = self.config["groups"].get(gid)
        if group is None:
            raise StoreError("you are not a member of that group")
        if other == self.name:
            raise StoreError("cannot invite yourself")
        ident = self.shared.read_identity(other)
        payload = {
            "type": "group",
            "from": self.name,
            "from_pub": crypto.b64e(self.pub),
            "group_id": gid,
            "group_name": group["name"],
            "group_key": group["key"],
            "ts": time.time(),
        }
        self._drop_sealed(self.shared.invites_dir(other), ident["pub"], payload)
        self.config["pending"].append({"type": "group", "to": other, "group_id": gid})
        self.save_config()

    def _drop_sealed(self, directory: str, pub_b64: str, payload: dict) -> None:
        _make_dir(directory, DROP_DIR_MODE)
        blob = crypto.seal(crypto.b64d(pub_b64), json.dumps(payload).encode())
        wrapper = json.dumps({"sealed": crypto.b64e(blob)}).encode()
        _atomic_write(
            os.path.join(directory, _msg_filename()), wrapper, PUBLIC_FILE_MODE
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
        try:
            with open(path, "rb") as fh:
                wrapper = json.loads(fh.read())
            return json.loads(
                crypto.unseal(self.priv, crypto.b64d(wrapper["sealed"]))
            )
        except (OSError, ValueError, KeyError, crypto.DecryptError):
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
        if payload["type"] == "contact":
            self.config["contacts"][sender] = payload["from_pub"]
            desc = f"chat with {sender}"
            reply = {
                "type": "contact_accept",
                "from": self.name,
                "from_pub": crypto.b64e(self.pub),
            }
        elif payload["type"] == "group":
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
        else:
            raise StoreError(f"unknown invite type: {payload['type']}")

        self.save_config()
        self._drop_sealed(
            self.shared.replies_dir(sender), payload["from_pub"], reply
        )
        self._discard(os.path.join(self.shared.invites_dir(self.name), fname))
        return desc

    def decline_invite(self, fname: str, payload: dict) -> None:
        reply = {
            "type": f"{payload['type']}_decline",
            "from": self.name,
            "group_id": payload.get("group_id"),
        }
        self._drop_sealed(
            self.shared.replies_dir(payload["from"]), payload["from_pub"], reply
        )
        self._discard(os.path.join(self.shared.invites_dir(self.name), fname))

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
                self.config["contacts"][sender] = payload["from_pub"]
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
        gid = secrets.token_hex(8)
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
    def _conv(self, kind: str, target: str) -> Tuple[str, bytes, bytes]:
        """Resolve (directory, key, aad) for a conversation."""
        if kind == "dm":
            pub_b64 = self.config["contacts"].get(target)
            if pub_b64 is None:
                raise StoreError(
                    f"no key exchanged with {target} - send or accept an invite first"
                )
            directory = self.shared.dm_dir(self.name, target)
            key = crypto.pair_key(self.priv, crypto.b64d(pub_b64))
            aad = os.path.basename(directory).encode()
            return directory, key, b"paf-dm|" + aad
        if kind == "grp":
            group = self.config["groups"].get(target)
            if group is None:
                raise StoreError("no key for that group - accept an invite first")
            directory = self.shared.group_msgs_dir(target)
            return directory, crypto.b64d(group["key"]), b"paf-grp|" + target.encode()
        raise StoreError(f"bad conversation kind: {kind}")

    def send_message(self, kind: str, target: str, text: str) -> dict:
        directory, key, aad = self._conv(kind, target)
        _make_dir(directory, DROP_DIR_MODE)
        msg = {"from": self.name, "ts": time.time(), "text": text}
        blob = crypto.sym_encrypt(key, json.dumps(msg).encode(), aad)
        wrapper = json.dumps({"blob": crypto.b64e(blob)}).encode()
        _atomic_write(
            os.path.join(directory, _msg_filename()), wrapper, PUBLIC_FILE_MODE
        )
        return msg

    def list_message_files(self, kind: str, target: str) -> List[str]:
        directory, _, _ = self._conv(kind, target)
        try:
            return sorted(
                f for f in os.listdir(directory) if f.endswith(".json")
            )
        except FileNotFoundError:
            return []

    def load_messages(
        self, kind: str, target: str, filenames: List[str]
    ) -> Dict[str, dict]:
        """Decrypt the given message files; undecryptable files are skipped."""
        directory, key, aad = self._conv(kind, target)
        out: Dict[str, dict] = {}
        for fname in filenames:
            try:
                with open(os.path.join(directory, fname), "rb") as fh:
                    wrapper = json.loads(fh.read())
                msg = json.loads(
                    crypto.sym_decrypt(key, crypto.b64d(wrapper["blob"]), aad)
                )
                if isinstance(msg, dict) and "text" in msg:
                    out[fname] = msg
            except (OSError, ValueError, KeyError, crypto.DecryptError):
                continue
        return out

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
