"""Append-only structured usage log for PR Monitor Bot commands.

Writes one JSON object per line (JSONL) per dispatched command, with a
hard cap on disk usage via Python's RotatingFileHandler.

Privacy:
    - Args for `register`, `renew`, and `renew_token` are **never** stored;
      they are replaced with the literal string ``<redacted>`` before write.
    - All free-form fields are sanitized (CR/LF stripped, truncated) to
      prevent log injection.

Disk cap:
    - Default 2.5 MB per file × 1 backup = **5 MB total** ceiling.
    - On rotation the oldest file is discarded, so the on-disk size never
      exceeds ``max_bytes * (backup_count + 1)``.

This module is deliberately self-contained (no project imports) so the
bot's main logger setup is unaffected.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

# Commands whose argument is a raw GitHub PAT — NEVER persist it.
_REDACT_ARG_COMMANDS = frozenset({"register", "renew", "renew_token"})

# Hard cap per logged free-form field to keep entries bounded and prevent
# log-injection / log-flooding via a long argument string.
_MAX_ARG_LEN = 200
_MAX_DETAIL_LEN = 200


def _sanitize(value: str, *, max_len: int = _MAX_ARG_LEN) -> str:
    """Strip CR/LF/TAB and truncate to keep log lines parseable as JSONL.

    Per the project's logging guidelines, all free-form input fields must
    be sanitized to prevent log-injection (CR/LF) before they hit disk.
    """
    if not value:
        return ""
    cleaned = (
        str(value)
        .replace("\r", " ")
        .replace("\n", " ")
        .replace("\t", " ")
    )
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len] + "..."
    return cleaned


class UsageLogger:
    """Per-command structured logger with rotating file backend."""

    def __init__(
        self,
        path,
        *,
        max_bytes: int = 2_500_000,
        backup_count: int = 1,
    ) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

        self._logger = logging.getLogger("pr-monitor-bot.usage")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        for handler in list(self._logger.handlers):
            self._logger.removeHandler(handler)

        rh = logging.handlers.RotatingFileHandler(
            self._path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        rh.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(rh)

    @property
    def path(self) -> Path:
        return self._path

    def log_command(
        self,
        email: str,
        cmd: str,
        args: str,
        source: str,
        outcome: str = "ok",
        detail: str = "",
        elapsed_ms: int = 0,
    ) -> None:
        cmd_l = (cmd or "").strip().lower()
        if cmd_l in _REDACT_ARG_COMMANDS:
            args_safe = "<redacted>"
        else:
            args_safe = _sanitize(args)

        record = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "user": _sanitize(email, max_len=120),
            "cmd": _sanitize(cmd_l, max_len=64),
            "args": args_safe,
            "source": _sanitize(source, max_len=8),
            "outcome": _sanitize(outcome, max_len=16),
            "detail": _sanitize(detail, max_len=_MAX_DETAIL_LEN),
            "elapsed_ms": int(elapsed_ms) if elapsed_ms else 0,
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            try:
                self._logger.info(line)
            except Exception:
                # Logging must never crash the bot.
                pass

    def file_paths(self) -> List[Path]:
        """All log files (active first, then rotated .1, .2, ...) that exist."""
        out: List[Path] = []
        if self._path.exists():
            out.append(self._path)
        for i in range(1, 10):
            candidate = self._path.parent / f"{self._path.name}.{i}"
            if candidate.exists():
                out.append(candidate)
            else:
                break
        return out

    def total_size_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.file_paths())

    def read_lines(
        self,
        max_lines: int = 100,
        user: Optional[str] = None,
    ) -> List[str]:
        """Return the *most recent* ``max_lines`` log lines, optionally filtered."""
        u = (user or "").strip().lower() or None
        with self._lock:
            files = self.file_paths()
            # Order: oldest rotated first, active last (chronological)
            files_chrono = list(reversed(files))
            collected: List[str] = []
            for p in files_chrono:
                try:
                    with open(p, "r", encoding="utf-8", errors="replace") as fh:
                        collected.extend(fh.readlines())
                except FileNotFoundError:
                    continue
        if u:
            filtered: List[str] = []
            for line in collected:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if (rec.get("user") or "").lower() == u:
                    filtered.append(line)
            collected = filtered
        return collected[-max_lines:] if max_lines > 0 else collected

    def export_combined(self, dest, user: Optional[str] = None):
        """Write all log lines (chronologically) into a single file at ``dest``."""
        dest_path = Path(dest)
        u = (user or "").strip().lower() or None
        with self._lock:
            files_chrono = list(reversed(self.file_paths()))
            with open(dest_path, "w", encoding="utf-8") as out:
                for p in files_chrono:
                    try:
                        with open(p, "r", encoding="utf-8", errors="replace") as fh:
                            for line in fh:
                                if u:
                                    try:
                                        rec = json.loads(line)
                                    except Exception:
                                        continue
                                    if (rec.get("user") or "").lower() != u:
                                        continue
                                out.write(line)
                    except FileNotFoundError:
                        continue
        return dest_path

    def summarize(self) -> dict:
        """Quick aggregate stats over the whole retained window."""
        with self._lock:
            files_chrono = list(reversed(self.file_paths()))
            entries = 0
            cmd_counts: dict = {}
            user_counts: dict = {}
            error_counts: dict = {}
            first_ts = ""
            last_ts = ""
            for p in files_chrono:
                try:
                    with open(p, "r", encoding="utf-8", errors="replace") as fh:
                        for line in fh:
                            try:
                                rec = json.loads(line)
                            except Exception:
                                continue
                            entries += 1
                            ts = rec.get("ts") or ""
                            if ts:
                                if not first_ts or ts < first_ts:
                                    first_ts = ts
                                if ts > last_ts:
                                    last_ts = ts
                            c = rec.get("cmd") or "?"
                            cmd_counts[c] = cmd_counts.get(c, 0) + 1
                            u = rec.get("user") or "?"
                            user_counts[u] = user_counts.get(u, 0) + 1
                            if rec.get("outcome") and rec["outcome"] != "ok":
                                error_counts[c] = error_counts.get(c, 0) + 1
                except FileNotFoundError:
                    continue
        return {
            "entries": entries,
            "first_ts": first_ts,
            "last_ts": last_ts,
            "size_bytes": self.total_size_bytes(),
            "by_cmd": cmd_counts,
            "by_user": user_counts,
            "errors_by_cmd": error_counts,
        }
