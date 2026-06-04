"""Encrypted per-user credential store.

Each user record maps a Webex email to their GitHub PAT and username.
Tokens are encrypted at rest with Fernet (AES-128-CBC + HMAC-SHA256).
"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)


class UserRecord:
    __slots__ = ("email", "github_token", "github_user")

    def __init__(self, email: str, github_token: str, github_user: str = ""):
        self.email = email.lower()
        self.github_token = github_token
        self.github_user = github_user

    def to_dict(self) -> dict:
        return {
            "email": self.email,
            "github_token": self.github_token,
            "github_user": self.github_user,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "UserRecord":
        return cls(
            email=d["email"],
            github_token=d["github_token"],
            github_user=d.get("github_user", ""),
        )


class UserStore:
    """Load/save encrypted user credentials on disk."""

    def __init__(self, path: str, encryption_key: str):
        self._path = Path(path)
        self._fernet = Fernet(encryption_key.encode() if isinstance(encryption_key, str) else encryption_key)
        self._users: Dict[str, UserRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            logger.info("User store not found at %s — starting fresh", self._path)
            return
        try:
            encrypted = self._path.read_bytes()
            decrypted = self._fernet.decrypt(encrypted)
            data = json.loads(decrypted)
            for email, rec in data.items():
                self._users[email.lower()] = UserRecord.from_dict(rec)
            logger.info("Loaded %d user(s) from store", len(self._users))
        except Exception:
            logger.exception("Failed to load user store — starting fresh")
            self._users = {}

    def _save(self) -> None:
        data = {email: rec.to_dict() for email, rec in self._users.items()}
        plaintext = json.dumps(data, indent=2).encode()
        encrypted = self._fernet.encrypt(plaintext)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_bytes(encrypted)
        logger.info("Saved %d user(s) to store", len(self._users))

    def get(self, email: str) -> Optional[UserRecord]:
        return self._users.get(email.lower())

    def put(self, record: UserRecord) -> None:
        self._users[record.email] = record
        self._save()

    def remove(self, email: str) -> bool:
        email = email.lower()
        if email in self._users:
            del self._users[email]
            self._save()
            return True
        return False

    def all_emails(self) -> list:
        return list(self._users.keys())

    def count(self) -> int:
        return len(self._users)

    @classmethod
    def read_all(cls, path: str, encryption_key: str) -> Dict[str, UserRecord]:
        """Decrypt a store file without mutating it."""
        p = Path(path)
        if not p.exists():
            return {}
        fernet = Fernet(
            encryption_key.encode()
            if isinstance(encryption_key, str)
            else encryption_key
        )
        data = json.loads(fernet.decrypt(p.read_bytes()))
        return {
            email.lower(): UserRecord.from_dict(rec)
            for email, rec in data.items()
        }

    @classmethod
    def write_all(
        cls, path: str, encryption_key: str, users: Dict[str, UserRecord]
    ) -> None:
        """Write a full user map to an encrypted store file."""
        store = cls.__new__(cls)
        store._path = Path(path)
        store._fernet = Fernet(
            encryption_key.encode()
            if isinstance(encryption_key, str)
            else encryption_key
        )
        store._users = {email.lower(): rec for email, rec in users.items()}
        store._save()

    @staticmethod
    def _token_valid(token: str, github_base_url: str) -> bool:
        if not token or not github_base_url:
            return False
        try:
            r = requests.get(
                f"{github_base_url.rstrip('/')}/user",
                headers={"Authorization": f"token {token}"},
                timeout=20,
            )
            return r.status_code == 200
        except requests.RequestException:
            return False

    @classmethod
    def merge_maps(
        cls,
        *sources: Dict[str, UserRecord],
        github_base_url: str = "",
        prefer: Optional[str] = None,
    ) -> Tuple[Dict[str, UserRecord], List[str]]:
        """Union multiple user maps; on duplicate email pick a working PAT.

        prefer: label for logging only, e.g. '577' or '619' — used when both
        tokens validate (later source wins if prefer matches second source).
        """
        merged: Dict[str, UserRecord] = {}
        notes: List[str] = []
        for idx, src in enumerate(sources):
            label = str(idx)
            for email, rec in src.items():
                email = email.lower()
                if email not in merged:
                    merged[email] = rec
                    continue
                existing = merged[email]
                if existing.github_token == rec.github_token:
                    if rec.github_user and not existing.github_user:
                        merged[email] = rec
                    continue
                ex_ok = cls._token_valid(existing.github_token, github_base_url)
                new_ok = cls._token_valid(rec.github_token, github_base_url)
                if ex_ok and not new_ok:
                    notes.append(f"{email}: kept existing PAT ({label} invalid)")
                elif new_ok and not ex_ok:
                    merged[email] = rec
                    notes.append(f"{email}: took PAT from source {label}")
                elif new_ok and ex_ok:
                    # both work — keep second if prefer matches this source index
                    merged[email] = rec
                    notes.append(f"{email}: both PATs valid; kept source {label}")
                else:
                    merged[email] = rec
                    notes.append(
                        f"{email}: neither PAT verified; kept source {label}"
                    )
        return merged, notes
