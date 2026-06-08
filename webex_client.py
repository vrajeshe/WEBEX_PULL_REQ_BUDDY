"""Webex Teams client using plain requests (no SDK dependency).

All commands are case-insensitive.
Handlers receive (sender_email, args) for multi-user support.
"""

import logging
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set

import requests

logger = logging.getLogger(__name__)

WEBEX_API = "https://webexapis.com/v1"

# Callback invoked once per dispatched command.
# Signature: (sender_email, cmd, args, source, outcome, detail, elapsed_ms) -> None
UsageLogCallback = Callable[[str, str, str, str, str, str, int], None]


class WebexBotClient:
    """Manages Webex messaging for the PR monitor bot."""

    def __init__(self, bot_token: str, room_id: str, allowed_emails: Optional[Set[str]] = None):
        self._room_id = room_id
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {bot_token}",
            "Content-Type": "application/json",
        })
        self._bot_id = None  # type: Optional[str]
        self._bot_name = ""
        self._commands: Dict[str, Callable] = {}
        self._dm_commands: Dict[str, Callable] = {}
        self._last_seen_id = None  # type: Optional[str]
        self._last_seen_ts = None  # type: Optional[str]
        self._dm_last_seen_ts = None  # type: Optional[str]
        self._dm_rooms: list = []
        self._dispatch_lock = threading.Lock()
        self._ready = False
        self._allowed_emails = {e.lower() for e in (allowed_emails or set())}
        self._reply_target = threading.local()
        self._reply_context = threading.local()
        self._seen_msg_ids: set = set()
        self._seen_max = 200
        self._usage_log_cb: Optional[UsageLogCallback] = None

        me = self._session.get(f"{WEBEX_API}/people/me", timeout=15).json()
        self._bot_id = me.get("id")
        self._bot_name = me.get("displayName", "")
        self._bot_email = me.get("emails", [""])[0].lower()
        logger.info("Webex bot authenticated as: %s (%s)", self._bot_name, self._bot_email)

    @property
    def room_id(self) -> str:
        return self._room_id

    @property
    def bot_email(self) -> str:
        return self._bot_email

    def is_email_allowed(self, email: str) -> bool:
        """True if this email may use the bot (no allowlist => everyone)."""
        e = (email or "").strip().lower()
        if not e:
            return False
        if not self._allowed_emails:
            return True
        return e in self._allowed_emails

    def list_room_member_emails(self) -> List[str]:
        """Lowercased personEmail values for memberships in the bot room (paginated)."""
        emails: List[str] = []
        seen: Set[str] = set()
        base = f"{WEBEX_API}/memberships"
        params: Dict[str, str] = {"roomId": self._room_id, "max": "100"}
        next_url: Optional[str] = None

        while True:
            if next_url:
                resp = self._session.get(next_url, timeout=30)
            else:
                resp = self._session.get(base, params=params, timeout=30)
            resp.raise_for_status()
            for item in resp.json().get("items", []):
                em = (item.get("personEmail") or "").strip().lower()
                if em and em not in seen:
                    seen.add(em)
                    emails.append(em)
            link = resp.headers.get("Link") or ""
            next_url = None
            for chunk in link.split(","):
                chunk = chunk.strip()
                if 'rel="next"' not in chunk and "rel=next" not in chunk:
                    continue
                found = re.search(r"<([^>]+)>", chunk)
                if found:
                    next_url = found.group(1)
                break
            if not next_url:
                break

        return emails

    @property
    def bot_id(self) -> Optional[str]:
        return self._bot_id

    def register_command(self, name: str, handler: Callable) -> None:
        """Register a room command. Handler signature: handler(sender_email, args)."""
        self._commands[name.lower()] = handler

    def register_dm_command(self, name: str, handler: Callable) -> None:
        """Register a DM-only command. Handler signature: handler(sender_email, args)."""
        self._dm_commands[name.lower()] = handler

    def set_usage_log_callback(self, cb: Optional[UsageLogCallback]) -> None:
        """Register a callback fired once per dispatched command for usage telemetry."""
        self._usage_log_cb = cb

    @property
    def reply_source(self) -> Optional[str]:
        """Return 'room' or 'dm' depending on where the current command originated, or None."""
        return getattr(self._reply_context, "source", None)

    def send_room_message(self, text: str = "", markdown: Optional[str] = None) -> None:
        """Send response — routed to the current user's DM if a reply target is set."""
        target = getattr(self._reply_target, "email", None)
        if target:
            self.send_dm(target, text=text, markdown=markdown)
            return
        try:
            payload = {"roomId": self._room_id}
            if markdown:
                payload["markdown"] = markdown
            else:
                payload["text"] = text
            resp = self._session.post(
                f"{WEBEX_API}/messages", json=payload, timeout=15
            )
            resp.raise_for_status()
        except Exception:
            logger.exception("Failed to send room message")

    def send_broadcast(self, text: str = "", markdown: Optional[str] = None) -> None:
        """Always send to the shared room, ignoring reply target."""
        try:
            payload = {"roomId": self._room_id}
            if markdown:
                payload["markdown"] = markdown
            else:
                payload["text"] = text
            resp = self._session.post(
                f"{WEBEX_API}/messages", json=payload, timeout=15
            )
            resp.raise_for_status()
        except Exception:
            logger.exception("Failed to send broadcast")

    def send_message(self, text: str = "", markdown: Optional[str] = None) -> None:
        """Alias — routes via reply target (DM) or falls back to room."""
        self.send_room_message(text=text, markdown=markdown)

    def send_dm(self, email: str, text: str = "", markdown: Optional[str] = None) -> bool:
        """Send a direct message to a user by email. Returns True on success."""
        try:
            payload = {"toPersonEmail": email}
            if markdown:
                payload["markdown"] = markdown
            else:
                payload["text"] = text
            resp = self._session.post(
                f"{WEBEX_API}/messages", json=payload, timeout=15
            )
            resp.raise_for_status()
            return True
        except Exception:
            logger.exception("Failed to send DM to %s", email)
            return False

    def send_dm_with_file(
        self,
        email: str,
        file_path: Any,
        markdown: str = "",
        text: str = "",
        file_name: Optional[str] = None,
        content_type: str = "application/octet-stream",
        timeout: int = 60,
    ) -> bool:
        """Send a DM with a file attachment via multipart/form-data.

        The shared session has a ``Content-Type: application/json`` header
        baked in (used by all the JSON endpoints), which would break
        multipart uploads — so this method issues a fresh request with only
        the ``Authorization`` header, letting ``requests`` set the boundary.
        """
        from pathlib import Path as _Path
        path = _Path(file_path)
        name = file_name or path.name
        auth = self._session.headers.get("Authorization", "")
        if not auth:
            logger.error("send_dm_with_file: missing Authorization header")
            return False
        try:
            data: Dict[str, str] = {"toPersonEmail": email}
            if markdown:
                data["markdown"] = markdown
            elif text:
                data["text"] = text
            with open(path, "rb") as fh:
                files = {"files": (name, fh, content_type)}
                resp = requests.post(
                    f"{WEBEX_API}/messages",
                    headers={"Authorization": auth},
                    data=data,
                    files=files,
                    timeout=timeout,
                )
            resp.raise_for_status()
            return True
        except Exception:
            logger.exception("Failed to send DM file to %s (path=%s)", email, path)
            return False

    def lookup_person(self, email: str) -> Optional[str]:
        """Look up a Webex user by email. Returns display name or None."""
        try:
            resp = self._session.get(
                f"{WEBEX_API}/people",
                params={"email": email, "max": 1},
                timeout=15,
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])
            if items:
                return items[0].get("displayName", email)
            return None
        except Exception:
            logger.exception("Failed to look up Webex user %s", email)
            return None

    def skip_existing_messages(self) -> None:
        """Record the newest message timestamp so we only process future messages."""
        try:
            resp = self._session.get(
                f"{WEBEX_API}/messages",
                params={"roomId": self._room_id, "max": 1},
                timeout=15,
            )
            resp.raise_for_status()
            messages = resp.json().get("items", [])
            if messages:
                self._last_seen_id = messages[0]["id"]
                self._last_seen_ts = messages[0]["created"]
                logger.info(
                    "Room watermark set to message %s at %s",
                    self._last_seen_id, self._last_seen_ts,
                )
        except Exception:
            logger.exception("Failed to skip existing room messages")

        try:
            resp = self._session.get(
                f"{WEBEX_API}/rooms",
                params={"type": "direct", "max": 20, "sortBy": "lastactivity"},
                timeout=15,
            )
            resp.raise_for_status()
            self._dm_rooms = [r["id"] for r in resp.json().get("items", [])]
            latest_ts = self._dm_last_seen_ts or ""
            for room_id in self._dm_rooms:
                try:
                    msg_resp = self._session.get(
                        f"{WEBEX_API}/messages",
                        params={"roomId": room_id, "max": 1},
                        timeout=15,
                    )
                    msg_resp.raise_for_status()
                    msgs = msg_resp.json().get("items", [])
                    if msgs and msgs[0]["created"] > latest_ts:
                        latest_ts = msgs[0]["created"]
                except Exception:
                    logger.debug("Skipped DM room %s during watermark init", room_id)
            if latest_ts:
                self._dm_last_seen_ts = latest_ts
            logger.info("DM watermark set to %s (%d direct rooms)", self._dm_last_seen_ts, len(self._dm_rooms))
        except Exception:
            logger.exception("Failed to skip existing DM messages")
            self._dm_rooms = []

        self._ready = True

    def poll_and_dispatch(self) -> None:
        """Poll for new messages and dispatch commands (one at a time)."""
        if not self._ready:
            return

        if not self._dispatch_lock.acquire(blocking=False):
            return
        try:
            self._poll_room_messages()
            self._poll_dm_messages()
        finally:
            self._dispatch_lock.release()

    def _poll_room_messages(self) -> None:
        try:
            resp = self._session.get(
                f"{WEBEX_API}/messages",
                params={"roomId": self._room_id, "max": 10},
                timeout=15,
            )
            resp.raise_for_status()
            messages = resp.json().get("items", [])
        except Exception:
            logger.exception("Failed to fetch room messages")
            return

        new_messages = []
        for msg in messages:
            created = msg.get("created", "")
            if self._last_seen_ts and created <= self._last_seen_ts:
                break
            if msg.get("personId") == self._bot_id:
                continue
            if msg["id"] in self._seen_msg_ids:
                continue
            new_messages.append(msg)

        if not new_messages:
            return

        newest = new_messages[0]
        self._last_seen_id = newest["id"]
        self._last_seen_ts = newest["created"]

        for msg in reversed(new_messages):
            self._mark_seen(msg["id"])
            sender_email = (msg.get("personEmail") or "").lower()
            if self._allowed_emails and sender_email not in self._allowed_emails:
                logger.warning(
                    "Unauthorized command from %s: %s",
                    sender_email, (msg.get("text") or "")[:60],
                )
                self.send_dm(
                    sender_email,
                    markdown="\U0001F6AB Access denied. "
                    "Send me a **DM** with `register <YOUR_GITHUB_TOKEN>` to get started, "
                    "or ask the admin to invite you.",
                )
                continue
            logger.info("Room msg from %s: %s", sender_email, (msg.get("text") or "")[:80])
            self._handle_room_message(sender_email, msg)

    def _poll_dm_messages(self) -> None:
        """Poll direct messages sent to the bot by checking direct rooms."""
        if not self._dm_commands:
            return

        try:
            resp = self._session.get(
                f"{WEBEX_API}/rooms",
                params={"type": "direct", "max": 20, "sortBy": "lastactivity"},
                timeout=15,
            )
            resp.raise_for_status()
            dm_rooms = resp.json().get("items", [])
        except Exception:
            logger.exception("Failed to list direct rooms")
            return

        all_new = []
        for room in dm_rooms:
            room_id = room["id"]
            room_last = room.get("lastActivity", "")
            if self._dm_last_seen_ts and room_last <= self._dm_last_seen_ts:
                continue
            try:
                msg_resp = self._session.get(
                    f"{WEBEX_API}/messages",
                    params={"roomId": room_id, "max": 5},
                    timeout=15,
                )
                msg_resp.raise_for_status()
                for msg in msg_resp.json().get("items", []):
                    created = msg.get("created", "")
                    if self._dm_last_seen_ts and created <= self._dm_last_seen_ts:
                        break
                    if msg.get("personId") == self._bot_id:
                        continue
                    if msg["id"] in self._seen_msg_ids:
                        continue
                    all_new.append(msg)
            except Exception:
                logger.exception("Failed to fetch DMs from room %s", room_id)

        if not all_new:
            return

        all_new.sort(key=lambda m: m.get("created", ""))
        self._dm_last_seen_ts = all_new[-1]["created"]

        for msg in all_new:
            self._mark_seen(msg["id"])
            sender_email = (msg.get("personEmail") or "").lower()
            logger.info("DM from %s: %s", sender_email, (msg.get("text") or "")[:80])
            self._handle_dm_message(sender_email, msg)

    def _mark_seen(self, msg_id: str) -> None:
        self._seen_msg_ids.add(msg_id)
        if len(self._seen_msg_ids) > self._seen_max:
            to_drop = len(self._seen_msg_ids) - self._seen_max
            it = iter(self._seen_msg_ids)
            for _ in range(to_drop):
                self._seen_msg_ids.discard(next(it))

    def _strip_bot_name(self, text: str) -> str:
        if self._bot_name:
            lower_text = text.lower()
            lower_name = self._bot_name.lower()
            if lower_text.startswith(lower_name):
                text = text[len(self._bot_name):].strip()
        return text

    def _dispatch_one(self, sender_email: str, text: str, commands: dict) -> None:
        """Dispatch a single command string against a command table.

        Captures outcome + elapsed time and invokes the usage-log callback
        (if registered). Per-segment exceptions are caught here so one bad
        segment does not abort sibling segments in a multi-segment message.
        """
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower().lstrip("/")
        args = parts[1] if len(parts) > 1 else ""
        source = getattr(self._reply_context, "source", None) or "?"

        handler = commands.get(cmd)
        if handler is None:
            logger.warning("No handler for cmd=%r (text=%r)", cmd, text)
            self.send_room_message(
                markdown="Unknown command: **{}**. Type **help** for available commands.".format(cmd)
            )
            self._emit_usage(sender_email, cmd, args, source, "unknown", "", 0)
            return

        # Args are deliberately NOT echoed in the dispatch log line for
        # commands like `register`/`renew` — the bot's general logger has
        # always logged args; usage_logger redacts on its own.
        log_args = "<redacted>" if cmd in ("register", "renew", "renew_token") else args
        logger.info("Dispatching cmd=%r sender=%s args=%r", cmd, sender_email, log_args)

        started = time.monotonic()
        outcome = "ok"
        detail = ""
        try:
            handler(sender_email, args)
        except Exception as exc:
            outcome = "error"
            detail = "{}: {}".format(type(exc).__name__, str(exc)[:120])
            logger.exception("Handler error for cmd=%r", cmd)
            try:
                self.send_room_message(
                    text="Error executing command **{}**.".format(cmd)
                )
            except Exception:
                pass
        elapsed_ms = int((time.monotonic() - started) * 1000)
        self._emit_usage(sender_email, cmd, args, source, outcome, detail, elapsed_ms)

    def _emit_usage(
        self,
        sender_email: str,
        cmd: str,
        args: str,
        source: str,
        outcome: str,
        detail: str,
        elapsed_ms: int,
    ) -> None:
        cb = self._usage_log_cb
        if cb is None:
            return
        try:
            cb(sender_email, cmd, args, source, outcome, detail, elapsed_ms)
        except Exception:
            logger.exception("usage log callback raised; ignoring")

    def _handle_room_message(self, sender_email: str, message: dict) -> None:
        text = (message.get("text") or "").strip()
        text = self._strip_bot_name(text)
        if not text:
            return

        self._reply_target.email = sender_email
        self._reply_context.source = "room"
        try:
            for segment in text.split(";"):
                segment = segment.strip()
                if segment:
                    self._dispatch_one(sender_email, segment, self._commands)
        except Exception:
            logger.exception("Command handler error for message: %s", text[:80])
            self.send_room_message(text="Error executing command.")
        finally:
            self._reply_target.email = None
            self._reply_context.source = None

    def _handle_dm_message(self, sender_email: str, message: dict) -> None:
        text = (message.get("text") or "").strip()
        text = self._strip_bot_name(text)
        if not text:
            return

        self._reply_target.email = sender_email
        self._reply_context.source = "dm"
        try:
            for segment in text.split(";"):
                segment = segment.strip()
                if segment:
                    self._dispatch_one(sender_email, segment, self._dm_commands)
        except Exception:
            logger.exception("DM command handler error for message: %s", text[:80])
            self.send_room_message(text="Error executing command.")
        finally:
            self._reply_target.email = None
            self._reply_context.source = None
