"""PR Monitor Bot — multi-user orchestrator.

Features:
- Multi-user: each user registers their own GitHub token (via DM); `renew` rotates an expired PAT
- GitHub actions use the initiating user's account
- Per-user PR monitoring with isolated state
- Notifications routed via DM to the owning user
- Precommit queuing, auto-retry; `automerge on` enables GitHub native auto-merge immediately
- All commands are case-insensitive
"""

import atexit
import logging
import logging.handlers
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

from github_client import (
    GitHubEnterpriseClient,
    PRECOMMIT_FALLBACK_TEST_TAG,
    PRInfo,
    SubmoduleAmbiguousError,
    WHITEBOX_DEFAULT_PR_TEMPLATE_SUFFIX,
    format_checks_summary,
    parse_pr_url,
)  # SubmoduleAmbiguousError still imported \u2014 used by `raise_hash_update` to surface match list
from jenkins_client import JenkinsClient, parse_jenkins_job_url
from usage_logger import UsageLogger
from user_store import UserStore
from webex_client import WebexBotClient


def _raise_hash_webex_cicd_summary_footer() -> str:
    """Webex appendix: CICD block included in the GitHub PR body with the submodule summary."""
    block = WHITEBOX_DEFAULT_PR_TEMPLATE_SUFFIX.strip()
    return (
        "\n\n---\n\n**On GitHub** the PR description includes the **Submodule update** summary "
        "and this **CICD Ring 2 (Precommit)** block:\n\n```\n"
        f"{block}\n```"
    )


_bot_dir = Path(__file__).resolve().parent
for _candidate in [_bot_dir / ".env.jira", _bot_dir.parent / ".env.jira"]:
    if _candidate.exists():
        load_dotenv(_candidate)
        break

_log_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
_file_handler = logging.handlers.TimedRotatingFileHandler(
    _bot_dir / "bot.log", when="midnight", backupCount=5, utc=False,
)
_file_handler.setFormatter(_log_fmt)
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_log_fmt)
logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _console_handler])

logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
logging.getLogger("apscheduler.executors").setLevel(logging.WARNING)

logger = logging.getLogger("pr-monitor-bot")

UT_CHECK_PREFIX = "ut - "
FAILED_STATES = {"failure", "error"}
SUCCESS_STATES = {"success"}
PENDING_STATES = {"pending", "queued", "in_progress"}
MAX_RETRIES = 2

UT_STALL_SECONDS = 30 * 60
MAX_UT_WATCHDOG_REPOSTS = 2

PRECOMMIT_CHECK_KEY = "precommit"
PRECOMMIT_RETRY_WAIT_SEC = 60
PRECOMMIT_MONITOR_POLL_SEC = 30

@dataclass
class PrecommitMonitor:
    """Watch precommit checks after ``precommit`` sets TEST_TAG with auto-trigger enabled."""

    user_tag: str
    attempt: int = 1
    phase: str = "watch"  # watch | wait_restore
    restore_at: float = 0.0
    saw_post_restore_activity: bool = False


@dataclass
class RetryTracker:
    command: str
    attempts: int = 1
    triggered_at: float = field(default_factory=time.time)
    watchdog_reposts: int = 0


@dataclass
class PRState:
    info: PRInfo
    owner_email: str
    previous_checks: Dict[str, str] = field(default_factory=dict)
    auto_merge: bool = False  # GitHub PR auto-merge flag (from API), for display
    retries: Dict[str, RetryTracker] = field(default_factory=dict)
    notify_emails: Dict[str, str] = field(default_factory=dict)
    precommit_monitor: Optional[PrecommitMonitor] = None


# Unique key for per-user PR tracking
PRKey = Tuple[str, int]  # (email, pr_number)


class PRMonitorBot:

    def __init__(self):
        self._gh_base_url = os.environ.get(
            "GITHUB_BASE_URL", "https://github.example.com/api/v3"
        )
        self._admin_token = os.environ.get("GITHUB_TOKEN", "")

        encryption_key = os.environ.get("ENCRYPTION_KEY", "")
        if not encryption_key:
            from cryptography.fernet import Fernet
            encryption_key = Fernet.generate_key().decode()
            logger.warning(
                "ENCRYPTION_KEY not set — generated ephemeral key. "
                "User registrations will be lost on restart. "
                "Set ENCRYPTION_KEY in .env.jira for persistence."
            )
        store_path = os.environ.get(
            "USER_STORE_PATH",
            str(_bot_dir / "users.db"),
        )
        self._user_store = UserStore(store_path, encryption_key)

        self._gh_clients: Dict[str, GitHubEnterpriseClient] = {}

        admin_email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
        allowed_raw = os.environ.get("ALLOWED_USERS", "").strip()
        allowed_set = {e.strip().lower() for e in allowed_raw.split(",") if e.strip()} if allowed_raw else set()

        self._webex = WebexBotClient(
            bot_token=os.environ["WEBEX_BOT_TOKEN"],
            room_id=os.environ["WEBEX_ROOM_ID"],
            allowed_emails=allowed_set if allowed_set else None,
        )
        self._poll_interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "600"))
        _dr = os.environ.get("DEFAULT_REPO", "").strip()
        if _dr and "/" not in _dr:
            self._default_repo = f"whitebox/{_dr}"
        else:
            self._default_repo = _dr
        self._admin_email = admin_email

        self._prs: Dict[PRKey, PRState] = {}
        self._running = False
        self._scheduler = BackgroundScheduler(daemon=True)

        self._jenkins: Optional[JenkinsClient] = None
        j_base = os.environ.get("JENKINS_BASE_URL", "").strip().rstrip("/")
        j_user = os.environ.get("JENKINS_USER", "").strip()
        j_token = os.environ.get("JENKINS_API_TOKEN", "").strip()
        if j_base and j_token:
            self._jenkins = JenkinsClient(j_base, username=j_user or None, api_token=j_token)
            logger.info("Jenkins API client enabled (%s)", j_base)

        # Per-command usage telemetry (5MB on-disk ceiling: 2 files × 2.5MB).
        usage_log_path = os.environ.get(
            "USAGE_LOG_PATH",
            str(_bot_dir / "usage.log"),
        )
        self._usage_logger = UsageLogger(usage_log_path)
        self._webex.set_usage_log_callback(self._usage_logger.log_command)
        logger.info("Usage logger enabled at %s", usage_log_path)

        self._preload_gh_clients()
        self._register_commands()

    def _is_admin(self, email: str) -> bool:
        """True if ``email`` matches ``ADMIN_EMAIL`` (case-insensitive)."""
        return bool(
            self._admin_email
            and (email or "").strip().lower() == self._admin_email
        )

    def _preload_gh_clients(self) -> None:
        """Build GitHub clients for all registered users on startup."""
        for email in self._user_store.all_emails():
            rec = self._user_store.get(email)
            if rec:
                self._gh_clients[email] = GitHubEnterpriseClient(
                    token=rec.github_token, base_url=self._gh_base_url,
                )
                logger.info("Loaded GitHub client for %s (%s)", email, rec.github_user)

        if self._admin_token and self._admin_email:
            if self._admin_email not in self._gh_clients:
                self._gh_clients[self._admin_email] = GitHubEnterpriseClient(
                    token=self._admin_token, base_url=self._gh_base_url,
                )
                logger.info("Loaded admin GitHub client for %s", self._admin_email)

    def _get_gh(self, email: str) -> Optional[GitHubEnterpriseClient]:
        """Get the GitHub client for a user, or None if not registered."""
        return self._gh_clients.get(email)

    def _reply(self, email: str, text: str = "", markdown: str = "") -> None:
        """Send a private response to a user via DM."""
        if markdown:
            self._webex.send_dm(email, markdown=markdown)
        else:
            self._webex.send_dm(email, text=text)

    def _require_gh(self, email: str) -> Optional[GitHubEnterpriseClient]:
        """Get GitHub client or send an error message if not registered."""
        gh = self._get_gh(email)
        if not gh:
            self._reply(
                email,
                markdown=(
                    f"\U0001F512 **{email}** is not registered. "
                    f"Send me a **DM** with: `register <YOUR_GITHUB_TOKEN>` to get started."
                ),
            )
        return gh

    def _register_commands(self) -> None:
        all_cmds = {
            "help": self._cmd_help,
            "register": self._dm_register,
            "renew": self._dm_renew,
            "renew_token": self._dm_renew,
            "invite": self._cmd_invite,
            "monitor": self._cmd_monitor,
            "stop": self._cmd_stop,
            "status": self._cmd_status,
            "precommit": self._cmd_precommit,
            "comment": self._cmd_comment,
            "automerge": self._cmd_automerge,
            "auto-merge": self._cmd_automerge,
            "approve": self._cmd_approve,
            "unapprove": self._cmd_unapprove,
            "merge": self._cmd_merge,
            "update_branch_rebase": self._cmd_update_branch_rebase,
            "rebase": self._cmd_update_branch_rebase,
            "update_branch_merge": self._cmd_update_branch_merge,
            "update_branch": self._cmd_update_branch_merge,
            "get_summary": self._cmd_get_summary,
            "summary": self._cmd_get_summary,
            "get_full_summary": self._cmd_get_full_summary,
            "full_summary": self._cmd_get_full_summary,
            "get_diff": self._cmd_get_diff,
            "diff": self._cmd_get_diff,
            "notify": self._cmd_notify,
            "unnotify": self._cmd_unnotify,
            "queue": self._cmd_queue,
            "interval": self._cmd_interval,
            "whoami": self._cmd_whoami,
            "users": self._cmd_users,
            "unregister": self._dm_unregister,
            "raise_hash_update": self._cmd_raise_hash_update,
            "raise_hash_update_for_all_sub_repos": self._cmd_raise_hash_update_for_all_sub_repos,
            "raise_hash_update_all": self._cmd_raise_hash_update_for_all_sub_repos,
            "backport": self._cmd_backport,
            "usage_log": self._cmd_usage_log,
            "usage": self._cmd_usage_log,
        }
        if self._jenkins is not None:
            all_cmds["jenkins_last"] = self._cmd_jenkins_last
            all_cmds["jenkins_build"] = self._cmd_jenkins_build
        for name, handler in all_cmds.items():
            self._webex.register_command(name, handler)
            self._webex.register_dm_command(name, handler)

    # ── helpers ────────────────────────────────────────────────

    def _resolve_pr_number(self, text: str) -> Optional[int]:
        text = text.strip()
        if text.isdigit():
            return int(text)
        parsed = parse_pr_url(text)
        if parsed:
            return parsed[2]
        return None

    def _resolve_pr_full(self, text: str) -> Optional[tuple]:
        text = text.strip()
        if text.isdigit():
            if not self._default_repo:
                return None
            owner, repo = self._default_repo.split("/", 1)
            return owner, repo, int(text)
        parsed = parse_pr_url(text)
        return parsed

    def _resolve_parent_owner_repo(self, spec: Optional[str]) -> Optional[Tuple[str, str]]:
        """Resolve parent repo as (owner, repo).

        Bare `repo` (no slash) is always **whitebox/repo**. Full `owner/repo` is used as-is.
        """
        if spec:
            s = spec.strip()
            if "/" in s:
                owner, repo = s.split("/", 1)
                return owner.strip(), repo.strip()
            return "whitebox", s
        if self._default_repo:
            o, r = self._default_repo.split("/", 1)
            return o, r
        return None

    def _pr_key(self, email: str, num: int) -> PRKey:
        return (email.lower(), num)

    def _get_or_auto_monitor(self, email: str, num: int) -> Optional[PRState]:
        key = self._pr_key(email, num)
        ps = self._prs.get(key)
        if ps:
            return ps
        gh = self._require_gh(email)
        if not gh:
            return None
        self._add_pr(email, num, quiet=True)
        return self._prs.get(key)

    def _user_prs(self, email: str) -> Dict[int, PRState]:
        """Return all PRs monitored by a specific user."""
        return {
            num: ps for (e, num), ps in self._prs.items()
            if e == email.lower()
        }

    def _ensure_monitoring_pr(
        self, email: str, owner: str, repo: str, number: int, *, quiet: bool = True
    ) -> Optional[PRState]:
        """Ensure ``owner/repo#number`` is in the poll list (uses PR key = email + number)."""
        gh = self._get_gh(email)
        if not gh:
            return None
        try:
            pr = gh.get_pr(owner, repo, number)
        except Exception:
            logger.exception(
                "Failed to fetch PR %s/%s#%d for monitoring", owner, repo, number
            )
            return None
        key = self._pr_key(email, number)
        ps = self._prs.get(key)
        if ps:
            ps.info = pr
            ps.auto_merge = pr.auto_merge_enabled
            return ps
        self._prs[key] = PRState(
            info=pr,
            owner_email=email,
            auto_merge=pr.auto_merge_enabled,
        )
        if not quiet:
            self._webex.send_room_message(
                markdown=(
                    f"\U0001F50D Now monitoring **[PR #{number}]({pr.html_url})** "
                    f"on `{owner}/{repo}`."
                ),
            )
        return self._prs[key]

    @staticmethod
    def _precommit_related_checks(
        check_states: Dict[str, str],
    ) -> List[Tuple[str, str]]:
        """Checks whose name contains ``precommit`` (excludes ``ut -`` UT jobs)."""
        out = []
        for name, state in check_states.items():
            lower = name.lower()
            if UT_CHECK_PREFIX in lower:
                continue
            if PRECOMMIT_CHECK_KEY in lower:
                out.append((name, state))
        return out

    @staticmethod
    def _clear_precommit_previous_checks(ps: PRState) -> None:
        ps.previous_checks = {
            k: v
            for k, v in ps.previous_checks.items()
            if PRECOMMIT_CHECK_KEY not in k.lower()
        }

    def _process_precommit_monitor(
        self, ps: PRState, check_states_lower: Dict[str, str]
    ) -> None:
        pm = ps.precommit_monitor
        if not pm:
            return
        gh = self._get_gh(ps.owner_email)
        if not gh:
            return
        pr = ps.info

        if pm.phase == "wait_restore":
            if time.time() < pm.restore_at:
                return
            try:
                gh.update_pr_precommit_test_tag(
                    pr.owner, pr.repo, pr.number, pm.user_tag
                )
            except Exception:
                logger.exception(
                    "precommit restore tag failed PR #%d", pr.number
                )
                self._webex.send_dm(
                    ps.owner_email,
                    markdown=(
                        f"\u274C Failed to restore **`TEST_TAG`: {pm.user_tag}** "
                        f"on PR #{pr.number} after Default retry wait."
                    ),
                )
                ps.precommit_monitor = None
                return
            pm.attempt = 2
            pm.phase = "watch"
            pm.saw_post_restore_activity = False
            self._clear_precommit_previous_checks(ps)
            self._webex.send_dm(
                ps.owner_email,
                markdown=(
                    f"\U0001F504 **Precommit retry (2/2)** on "
                    f"[PR #{pr.number}]({pr.html_url}): restored **`TEST_TAG`: {pm.user_tag}** "
                    f"after **{PRECOMMIT_RETRY_WAIT_SEC}s** with **`Default`**."
                ),
            )
            return

        related = self._precommit_related_checks(check_states_lower)
        if not related:
            return

        any_failed = any(s in FAILED_STATES for _, s in related)
        any_pending = any(s in PENDING_STATES for _, s in related)
        all_done = all(s not in PENDING_STATES for _, s in related)
        all_passed = all(s in SUCCESS_STATES for _, s in related)

        if any_pending or not all_done:
            if pm.attempt >= 2:
                pm.saw_post_restore_activity = True
            return

        if all_passed:
            ps.precommit_monitor = None
            self._webex.send_dm(
                ps.owner_email,
                markdown=(
                    f"\U0001F7E2 **Precommit passed** on [PR #{pr.number}]({pr.html_url}) "
                    f"with **`TEST_TAG`: {pm.user_tag}**."
                ),
            )
            return

        if not any_failed:
            return

        if pm.attempt >= 2 and not pm.saw_post_restore_activity:
            return

        if pm.attempt == 1:
            try:
                gh.update_pr_precommit_test_tag(
                    pr.owner, pr.repo, pr.number, PRECOMMIT_FALLBACK_TEST_TAG
                )
            except Exception:
                logger.exception(
                    "precommit Default tag failed PR #%d", pr.number
                )
                self._webex.send_dm(
                    ps.owner_email,
                    markdown=f"\u274C Precommit failed; could not set **TEST_TAG** to **Default** on PR #{pr.number}.",
                )
                ps.precommit_monitor = None
                return
            pm.phase = "wait_restore"
            pm.restore_at = time.time() + PRECOMMIT_RETRY_WAIT_SEC
            failed = [n for n, s in related if s in FAILED_STATES]
            self._webex.send_dm(
                ps.owner_email,
                markdown=(
                    f"\u26A0\uFE0F **Precommit failed** (1/2) on [PR #{pr.number}]({pr.html_url}) "
                    f"with **`TEST_TAG`: {pm.user_tag}**.\n"
                    f"Failed: {', '.join(f'`{n}`' for n in failed)}\n\n"
                    f"Set **`TEST_TAG`: {PRECOMMIT_FALLBACK_TEST_TAG}**, waiting "
                    f"**{PRECOMMIT_RETRY_WAIT_SEC}s**, then restoring your tag for **attempt 2**."
                ),
            )
            return

        ps.precommit_monitor = None
        failed = [n for n, s in related if s in FAILED_STATES]
        self._webex.send_dm(
            ps.owner_email,
            markdown=(
                f"\u274C **Precommit failed** after 2 attempts on [PR #{pr.number}]({pr.html_url}).\n"
                f"Failed: {', '.join(f'`{n}`' for n in failed)}\n"
                f"Last **`TEST_TAG`**: `{pm.user_tag}` — manual action needed."
            ),
        )

    def _poll_precommit_monitors(self) -> None:
        """Faster poll loop for PRs with active precommit monitoring."""
        for (email, num), ps in list(self._prs.items()):
            if not ps.precommit_monitor:
                continue
            pr = ps.info
            gh = self._get_gh(email)
            if not gh:
                continue
            try:
                fresh_pr = gh.get_pr(pr.owner, pr.repo, pr.number)
                ps.info = fresh_pr
                if fresh_pr.state == "closed" or fresh_pr.merged:
                    ps.precommit_monitor = None
                    continue
                checks = gh.get_all_checks(pr.owner, pr.repo, fresh_pr.head_sha)
            except Exception:
                logger.exception("precommit monitor poll failed PR #%d", num)
                continue
            current_states_lower = {c.name.lower(): c.state for c in checks}
            self._process_precommit_monitor(ps, current_states_lower)

    # ── DM commands (registration) ─────────────────────────────

    def _validate_and_store_github_pat(self, sender_email: str, token: str) -> Optional[str]:
        """Call GitHub with ``token``; on success persist and refresh the in-memory client.

        Returns the GitHub login, or ``None`` if validation failed (caller sends error DM).
        """
        try:
            test_gh = GitHubEnterpriseClient(token=token, base_url=self._gh_base_url)
            user_resp = test_gh._get("/user")
            gh_user = user_resp.get("login", "") or ""
        except Exception:
            logger.exception("GitHub PAT validation failed for %s", sender_email)
            return None

        from user_store import UserRecord

        self._user_store.put(
            UserRecord(
                email=sender_email,
                github_token=token,
                github_user=gh_user,
            )
        )
        self._gh_clients[sender_email] = test_gh
        return gh_user

    def _warn_room_pat_leak(self, sender_email: str, cmd_name: str) -> None:
        """Post a room-visible warning that ``sender_email`` just pasted a PAT in the room.

        Called whenever ``register`` / ``renew`` is invoked in the shared room *with*
        a token argument. Silently redirecting to DM at this point does not un-expose
        the token — it just leaves the user stuck and repeatedly re-pasting it, which
        makes the leak worse. This warning surfaces the exposure to the user (and to
        anyone else in the room) so the token can be rotated immediately.
        """
        self._webex.send_broadcast(
            markdown=(
                f"\u26A0\uFE0F **{sender_email}** \u2014 you just pasted a "
                f"**GitHub Personal Access Token** in this shared room via "
                f"`{cmd_name}`. It is now visible in this room's history to "
                f"**everyone**.\n\n"
                f"**Please rotate this token immediately** on "
                f"[Tokens (classic)](https://github.example.com/settings/tokens) "
                f"and, next time, **send `{cmd_name} <TOKEN>` to me via DM** \u2014 "
                f"never in the room.\n\n"
                f"_(The bot has captured the token so your registration is not "
                f"stuck, but treat it as compromised until you rotate.)_"
            )
        )

    def _dm_register(self, sender_email: str, args: str) -> None:
        token = args.strip()
        in_room = (self._webex.reply_source == "room")

        # Empty command: just guidance. Send to whichever channel the user
        # is actually looking at so they don't miss it.
        if not token:
            guidance = (
                "\U0001F44B To register, DM me:\n\n"
                "`register <YOUR_GITHUB_PERSONAL_ACCESS_TOKEN>`\n\n"
                "\U0001F512 Your token is encrypted with AES-256 at rest and is "
                "only used for GitHub API calls on your behalf.\n\n"
                "Need a token? Go to "
                "[Tokens (classic)](https://github.example.com/settings/tokens)"
            )
            if in_room:
                self._webex.send_broadcast(
                    markdown=(
                        f"\U0001F44B **{sender_email}** \u2014 please **DM me** "
                        f"with `register <YOUR_GITHUB_TOKEN>`. Do **not** paste "
                        f"tokens in this public room."
                    )
                )
            self._webex.send_dm(sender_email, markdown=guidance)
            return

        # Token was pasted in the room \u2014 it is already leaked. Warn loudly
        # in the room, then fall through and process the registration so the
        # user isn't stuck retrying (which just leaks the same token again).
        if in_room:
            self._warn_room_pat_leak(sender_email, "register")

        prior = self._user_store.get(sender_email)
        gh_user = self._validate_and_store_github_pat(sender_email, token)
        if gh_user is None:
            err = (
                "\u274C **Invalid token.** GitHub rejected this PAT \u2014 "
                "nothing was changed.\n\n"
                "Common causes:\n"
                "- Token **expired** \u2014 generate a new one at "
                "[Tokens (classic)](https://github.example.com/settings/tokens)\n"
                "- Missing **SSO authorization** for the enterprise org (click "
                "\u201CAuthorize\u201D next to the token on GitHub Enterprise)\n"
                "- Wrong host (this bot talks to **GitHub Enterprise**, not github.com)"
            )
            self._webex.send_dm(sender_email, markdown=err)
            if in_room:
                self._webex.send_broadcast(
                    markdown=(
                        f"\u274C **{sender_email}** \u2014 that token was "
                        f"rejected by GitHub. See your DM for details. "
                        f"**Still rotate it** since it was leaked in the room."
                    )
                )
            return

        if prior is None:
            logger.info("User registered: %s -> GitHub %s", sender_email, gh_user)
            self._webex.send_dm(
                sender_email,
                markdown=(
                    f"\u2705 **Registered!** GitHub user: **{gh_user}**\n\n"
                    f"You can now use all commands in the **room** or via **DM**.\n"
                    f"All responses are sent privately to you via DM.\n"
                    f"If your PAT **expires** later, DM: `renew <NEW_TOKEN>` "
                    f"(same as re-sending `register`).\n"
                    f"Type `unregister` here to remove your credentials."
                ),
            )
            self._webex.send_broadcast(
                markdown=f"\U0001F44B **{gh_user}** ({sender_email}) has joined the bot!"
            )
        else:
            logger.info("GitHub PAT renewed for %s (%s)", sender_email, gh_user)
            self._webex.send_dm(
                sender_email,
                markdown=(
                    f"\u2705 **GitHub token updated** for **{gh_user}**.\n\n"
                    f"Your previous PAT was replaced. Retry any command that "
                    f"returned **401** / **Unauthorized**.\n"
                    f"If org repos still fail, authorize **SSO** for your token "
                    f"on GitHub Enterprise."
                ),
            )

    def _dm_renew(self, sender_email: str, args: str) -> None:
        """Replace stored PAT (e.g. after expiry) without unregistering.

        Accepts both DM and room invocations. When invoked in the room *with*
        a token, the token is treated as leaked \u2014 the bot issues a loud
        room-visible warning telling the user to rotate immediately, and
        still processes the renewal so the user isn't stuck retrying.
        """
        token = args.strip()
        in_room = (self._webex.reply_source == "room")

        if not token:
            usage = (
                "**Usage:** `renew <NEW_GITHUB_PERSONAL_ACCESS_TOKEN>`\n\n"
                "Use this when your PAT **expired** or you need a **new scope** "
                "(e.g. SSO). Your Webex identity stays the same; only the "
                "GitHub token is replaced.\n\n"
                "You can also send `register <NEW_TOKEN>` \u2014 it does the "
                "same update if you are already registered.\n\n"
                "\u26A0\uFE0F Send this command via **DM**, not in a room."
            )
            if in_room:
                self._webex.send_broadcast(
                    markdown=(
                        f"\U0001F44B **{sender_email}** \u2014 please **DM me** "
                        f"with `renew <NEW_TOKEN>`. Do **not** paste tokens in "
                        f"the room."
                    )
                )
            self._webex.send_dm(sender_email, markdown=usage)
            return

        if in_room:
            self._warn_room_pat_leak(sender_email, "renew")

        if not self._user_store.get(sender_email):
            self._webex.send_dm(
                sender_email,
                markdown=(
                    "You are **not** registered yet. Send:\n\n"
                    "`register <YOUR_GITHUB_TOKEN>` (via DM)\n\n"
                    "After that, use `renew <NEW_TOKEN>` whenever you need to "
                    "rotate the PAT."
                ),
            )
            return

        gh_user = self._validate_and_store_github_pat(sender_email, token)
        if gh_user is None:
            self._webex.send_dm(
                sender_email,
                markdown=(
                    "\u274C **Invalid token.** GitHub rejected this PAT \u2014 "
                    "nothing was changed.\n\n"
                    "Check that the token is **not expired**, has **SSO "
                    "authorized** for the org, and was copied in full."
                ),
            )
            if in_room:
                self._webex.send_broadcast(
                    markdown=(
                        f"\u274C **{sender_email}** \u2014 that token was "
                        f"rejected by GitHub. See your DM for details. "
                        f"**Still rotate it** since it was leaked in the room."
                    )
                )
            return

        logger.info("GitHub PAT renewed (renew cmd) for %s (%s)", sender_email, gh_user)
        self._webex.send_dm(
            sender_email,
            markdown=(
                f"\u2705 **GitHub token renewed** for **{gh_user}**.\n\n"
                f"Retry your PR commands. If you still see **401**, check "
                f"**SSO authorization** for the org on "
                f"[token settings](https://github.example.com/settings/tokens)."
            ),
        )

    def _dm_unregister(self, sender_email: str, _args: str) -> None:
        removed = self._user_store.remove(sender_email)
        self._gh_clients.pop(sender_email, None)

        keys_to_remove = [k for k in self._prs if k[0] == sender_email]
        for k in keys_to_remove:
            del self._prs[k]

        if removed:
            self._webex.send_dm(
                sender_email,
                markdown="\u2705 **Unregistered.** Your GitHub token has been deleted and all monitored PRs stopped.",
            )
        else:
            self._webex.send_dm(sender_email, text="You are not registered.")

    def _dm_whoami(self, sender_email: str, _args: str) -> None:
        rec = self._user_store.get(sender_email)
        if rec:
            user_prs = self._user_prs(sender_email)
            pr_list = ", ".join(f"#{n}" for n in sorted(user_prs.keys())) or "none"
            self._webex.send_dm(
                sender_email,
                markdown=(
                    f"\U0001F464 **{sender_email}**\n"
                    f"GitHub: **{rec.github_user}**\n"
                    f"Monitoring: {pr_list}"
                ),
            )
        else:
            self._webex.send_dm(
                sender_email,
                markdown="You are not registered. Send `register <GITHUB_TOKEN>` to get started.",
            )

    def _dm_help(self, sender_email: str, _args: str) -> None:
        self._cmd_help(sender_email, _args)
        self._webex.send_dm(
            sender_email,
            markdown=(
                "**DM-only commands:**\n\n"
                "| Command | Description |\n"
                "|---------|-------------|\n"
                "| `register <TOKEN>` | Register your GitHub PAT (encrypted storage) |\n"
                "| `renew <TOKEN>` | Replace an **expired** or rotated PAT (same as `register` if already registered) |\n"
                "| `unregister` | Remove your credentials and stop all monitoring |\n"
            ),
        )

    # ── room commands ──────────────────────────────────────────

    def _send_help(self, sender_email: str = "") -> None:
        help_lines = [
            "**PR Monitor Bot** \U0001F916 (Multi-User)",
            "",
            "\U0001F512 **All responses are sent privately via DM.** No user sees another user's output.",
            "",
            "**Setup:** DM me `register <GITHUB_TOKEN>` to link your GitHub account, or ask someone to `invite <you>`.",
            "",
            "| Command | Example | Description |",
            "|---------|---------|-------------|",
            "| **monitor** `<PR_ID>` | `monitor 3473` or full URL | Start monitoring a PR |",
            "| **stop** `<PR_ID / all>` | `stop 3473` or full URL or `stop all` | Stop monitoring |",
            "| **status** `<PR_ID / all>` | `status 3473` or full URL or `status all` | Check status |",
            "| **precommit** `<PR_ID> <TEST_TAG>` | `precommit 3473 ROUTING` or full URL | Set **`TEST_TAG`** in PR description (CICD block) |",
            "| **comment** `<PR_ID> <text>` | `comment 3473 LGTM` or full URL | Post a comment |",
            "| **approve** `<PR_ID>` | `approve 3473` or full URL | Approve a PR |",
            "| **unapprove** `<PR_ID>` | `unapprove 3473` or full URL | Withdraw approval |",
            "| **merge** `<PR_ID>` | `merge 3473` or full URL | Merge a PR |",
            "| **backport** | `backport <PR> <target_branch>` | Squash cherry-pick the PR's diff onto **target_branch** and open a new PR (3-way merge; reports conflicts) |",
            "| **rebase** `<PR_ID>` | `rebase 3473` or full URL | Update branch via rebase |",
            "| **update_branch** `<PR_ID>` | `update_branch 3473` or full URL | Update branch via merge |",
            "| **summary** `<PR_ID>` | `summary 3473` or full URL | PR summary |",
            "| **full_summary** `<PR_ID>` | `full_summary 3473` or full URL | Full details + comments |",
            "| **diff** `<PR_ID>` | `diff 3473` or full URL | View PR code diff |",
            "| **automerge** `<PR_ID> <on/off>` | `automerge 3473 on` or full URL | Turns **GitHub** auto-merge on/off immediately |",
            "| **notify** `<PR_ID> <user>` | `notify 3473 johndoe` or full URL | Add status subscriber |",
            "| **unnotify** `<PR_ID> <user>` | `unnotify 3473 johndoe` or full URL | Remove subscriber |",
            "| **raise_hash_update** | `raise_hash_update c-master sonic-mgmt-common,sonic-swss JIRA-1` | **parent_ref**; **`sub`** = `.gitmodules` hint(s); **commas** = **one PR** updating each gitlink; **FRR** sets **`rules/frr.mk`** |",
            "| **raise_hash_update_for_all_sub_repos** | `raise_hash_update_for_all_sub_repos c-master JIRA-1` | Bumps **every stale** submodule on the parent in **one PR** (skips up-to-date and unreachable ones) |",
        ]
        if self._jenkins is not None:
            help_lines.extend([
                "| **jenkins_last** | `jenkins_last <job URL or path>` | Last build + result (server **Jenkins** API) |",
                "| **jenkins_build** | `jenkins_build <job URL or path> [KEY=VAL ...]` | Queue build; any **KEY=VAL** → **buildWithParameters** |",
            ])
        help_lines.extend([
            "| **queue** | `queue` | Show your pending items |",
            "| **whoami** | `whoami` | Show your registration status |",
            "| **invite** `<username>` | `invite johndoe` | Invite a user via DM |",
            "| **users** | `users` | List registered users |",
            "| **usage_log** | `usage_log` / `usage_log tail 50` / `usage_log all` / `usage_log <user@example.com>` | _(admin)_ Recent usage entries inline; `all` exports the full log as a file (5MB ring-buffer; PATs redacted) |",
            "| **interval** `<seconds>` | `interval 300` | Change polling interval |",
            "| **help** | `help` | Show this message |",
            "",
            "**Smart features:**",
            "- Each user's GitHub actions use **their own account**",
            "- **All responses are private** — sent via DM, never in the room",
            "- Use `notify <PR_ID> <user>` to explicitly share PR status with others",
            "- **precommit** sets **`TEST_TAG`** in the PR CICD block; if **Enable Precommit Auto-Trigger** is checked, monitors precommit (on fail: **Default** \u2192 wait 1 min \u2192 restore tag, retry once)",
            "- Failed UT auto-retries once (max 2 attempts) when a UT trigger was posted earlier",
            "- **UT watchdog**: if no UT build progress in **30 min**, or a PR comment says **Build did not succeed** / **ABORTED** after your trigger, the bot re-posts the UT trigger (up to 2 times)",
            "- Merged/closed PRs are auto-stopped",
            "- Status reports every 3 hours (only if something changed)",
            "- **raise_hash_update** — **parent_ref** reads **.gitmodules** on the parent; **sub** is a hint per submodule (comma-separated \u2192 **one PR** updating every listed gitlink). Each hint must resolve uniquely (refine with `path/` or `owner/repo` if ambiguous). **FRR** also updates **`rules/frr.mk`**.\n",
            "- Examples: `raise_hash_update 202405c sonic-utilities MIGSOFTWAR-39128`; `raise_hash_update c-master frr MIGSOFTWAR-39128`; `raise_hash_update c-master sonic-mgmt-common,sonic-swss MIGSOFTWAR-35315`.",
            "",
            "All commands work in the **room** or **DM**. Commands are **case insensitive**.",
        ])
        self._webex.send_room_message(markdown="\n".join(help_lines))

    def _cmd_help(self, sender_email: str, _args: str) -> None:
        self._send_help(sender_email)

    def _cmd_whoami(self, sender_email: str, _args: str) -> None:
        rec = self._user_store.get(sender_email)
        if rec:
            user_prs = self._user_prs(sender_email)
            pr_list = ", ".join(f"#{n}" for n in sorted(user_prs.keys())) or "none"
            self._webex.send_room_message(
                markdown=(
                    f"\U0001F464 **{sender_email}** \u2192 GitHub: **{rec.github_user}**\n"
                    f"Monitoring: {pr_list}"
                ),
            )
        else:
            self._webex.send_room_message(
                markdown=f"\U0001F512 **{sender_email}** is not registered. DM me `register <TOKEN>` to start.",
            )

    def _cmd_users(self, sender_email: str, _args: str) -> None:
        emails = self._user_store.all_emails()
        if not emails:
            self._webex.send_room_message("No users registered yet.")
            return
        lines = ["**Registered users:**"]
        for e in sorted(emails):
            rec = self._user_store.get(e)
            gh = rec.github_user if rec else "?"
            count = len(self._user_prs(e))
            lines.append(f"  \u2022 **{gh}** ({e}) — {count} PR(s) monitored")
        self._webex.send_room_message(markdown="\n".join(lines))

    def _cmd_usage_log(self, sender_email: str, args: str) -> None:
        """Admin-only: dump or summarize the per-command usage log.

        Usage:
            usage_log                       — last 100 entries inline
            usage_log tail [N]              — last N entries (max 1000) inline
            usage_log all                   — DM the full log as a file attachment
            usage_log stats                 — summary (entries, bytes, by-cmd, by-user)
            usage_log <user@example.com>    — filter to that user (full file)
            usage_log user <user@example.com>  — same, explicit form
        """
        if not self._is_admin(sender_email):
            self._webex.send_room_message(
                markdown="\U0001F6AB `usage_log` is **admin-only**.",
            )
            return

        raw = (args or "").strip()
        lower = raw.lower()
        user_filter: Optional[str] = None
        mode = "tail"
        n = 100

        if not raw:
            mode = "tail"
        elif lower == "all":
            mode = "all"
        elif lower == "stats":
            mode = "stats"
        elif lower.startswith("tail"):
            bits = raw.split()
            if len(bits) > 1 and bits[1].isdigit():
                n = max(1, min(1000, int(bits[1])))
            mode = "tail"
        elif lower.startswith("user "):
            user_filter = raw.split(maxsplit=1)[1].strip().lower()
            mode = "all"
        elif "@" in raw and " " not in raw:
            # Shorthand: `usage_log <email>` → filter that user, full export.
            user_filter = raw.lower()
            mode = "all"
        else:
            self._webex.send_room_message(
                markdown=(
                    "**Usage:**\n"
                    "- `usage_log` — last 100 entries inline\n"
                    "- `usage_log tail <N>` — last N entries inline (max 1000)\n"
                    "- `usage_log all` — full log as a file attachment\n"
                    "- `usage_log stats` — aggregate counts\n"
                    "- `usage_log <user@example.com>` — full log filtered by user"
                ),
            )
            return

        if mode == "stats":
            stats = self._usage_logger.summarize()
            top_cmds = sorted(
                stats["by_cmd"].items(), key=lambda kv: kv[1], reverse=True,
            )[:15]
            top_users = sorted(
                stats["by_user"].items(), key=lambda kv: kv[1], reverse=True,
            )[:15]
            lines = [
                "\U0001F4CA **Usage log stats**",
                "",
                f"- Entries: **{stats['entries']:,}**",
                f"- Window: `{stats['first_ts'] or 'n/a'}` \u2192 `{stats['last_ts'] or 'n/a'}`",
                f"- On-disk size: **{stats['size_bytes']:,} bytes**",
                "",
                "**Top commands:**",
            ]
            if top_cmds:
                lines.append("| Command | Count | Errors |")
                lines.append("|---------|------:|-------:|")
                for cmd, count in top_cmds:
                    errs = stats["errors_by_cmd"].get(cmd, 0)
                    lines.append(f"| `{cmd}` | {count} | {errs} |")
            else:
                lines.append("_(no entries yet)_")
            lines.append("")
            lines.append("**Top users:**")
            if top_users:
                lines.append("| User | Count |")
                lines.append("|------|------:|")
                for user, count in top_users:
                    lines.append(f"| `{user}` | {count} |")
            else:
                lines.append("_(none)_")
            self._webex.send_dm(sender_email, markdown="\n".join(lines))
            return

        if mode == "tail":
            lines = self._usage_logger.read_lines(max_lines=n, user=user_filter)
            if not lines:
                self._webex.send_dm(
                    sender_email,
                    markdown="_(no usage entries match)_",
                )
                return
            body = "".join(lines)
            limit = 6500  # leave headroom under Webex's ~7K markdown ceiling
            if len(body) > limit:
                body = body[-limit:]
                # Trim partial first line if any
                nl = body.find("\n")
                if 0 < nl < 200:
                    body = body[nl + 1:]
                note = (
                    f"_(truncated tail of last **{len(lines)}** entries; use "
                    f"`usage_log all` for the full file)_"
                )
            else:
                note = f"_(last **{len(lines)}** entries)_"
            self._webex.send_dm(
                sender_email,
                markdown=note + "\n\n```\n" + body + "```",
            )
            return

        # mode == "all" — export and DM as attachment
        import tempfile
        suffix = "_" + user_filter.split("@")[0] if user_filter else ""
        fname = f"usage_log{suffix}.jsonl"
        tmp_dir = tempfile.gettempdir()
        tmp_path = Path(tmp_dir) / fname
        try:
            self._usage_logger.export_combined(tmp_path, user=user_filter)
            try:
                size = tmp_path.stat().st_size
            except FileNotFoundError:
                size = 0
            if size == 0:
                self._webex.send_dm(
                    sender_email,
                    markdown="_(no usage entries match)_",
                )
                return
            md = (
                "\U0001F4CA **Usage log export**"
                + (f" (filtered by `{user_filter}`)" if user_filter else "")
                + f" \u2014 {size:,} bytes"
            )
            ok = self._webex.send_dm_with_file(
                sender_email,
                file_path=tmp_path,
                markdown=md,
                file_name=fname,
            )
            if not ok:
                self._webex.send_dm(
                    sender_email,
                    markdown="\u26A0\uFE0F Failed to send usage-log file. Check bot.log.",
                )
        finally:
            try:
                tmp_path.unlink()
            except Exception:
                pass


    def _cmd_invite(self, sender_email: str, args: str) -> None:
        username = args.strip().lower()
        if not username:
            self._webex.send_room_message(
                "Usage: `invite <username>`  Example: `invite johndoe`"
            )
            return

        email = f"{username}@cisco.com"
        rec = self._user_store.get(email)
        if rec:
            self._webex.send_room_message(
                f"**{username}** is already registered (GitHub: **{rec.github_user}**)."
            )
            return

        display_name = self._webex.lookup_person(email)
        if not display_name:
            self._webex.send_room_message(f"Could not find Webex user `{email}`. Check the username.")
            return

        self._webex.send_dm(
            email,
            markdown=(
                f"\U0001F44B Hi **{display_name}**!\n\n"
                f"You've been invited to use the **PR Monitor Bot** by **{sender_email}**.\n\n"
                f"To get started, reply to me here with:\n"
                f"`register <YOUR_GITHUB_PERSONAL_ACCESS_TOKEN>`\n\n"
                f"\U0001F512 **Your token is safe** \u2014 it is encrypted with AES-256 "
                f"and stored securely. It is only used for GitHub API calls on your behalf "
                f"and is never visible to anyone.\n\n"
                f"Need a token? Go to "
                f"[Tokens (classic)](https://github.example.com/settings/tokens)"
            ),
        )
        self._webex.send_room_message(
            markdown=(
                f"\U0001F4E8 Invitation sent to **{display_name}** (`{username}`) via DM.\n"
                f"They'll need to DM me `register <TOKEN>` to complete setup."
            )
        )

    def _cmd_monitor(self, sender_email: str, args: str) -> None:
        text = args.strip()
        if not text:
            self._webex.send_room_message("Usage: `monitor <PR_ID>`  Example: `monitor 3473` or full URL")
            return
        gh = self._require_gh(sender_email)
        if not gh:
            return

        num = self._resolve_pr_number(text)
        if num is None:
            self._webex.send_room_message(f"Could not parse: `{text}`. Use a PR number or full URL.")
            return

        key = self._pr_key(sender_email, num)
        if key in self._prs:
            self._webex.send_room_message(f"You are already monitoring PR #{num}.")
            return
        self._add_pr(sender_email, num)

    def _cmd_stop(self, sender_email: str, args: str) -> None:
        text = args.strip().lower()
        if not text:
            self._webex.send_room_message("Usage: `stop <PR_ID>` or `stop all`  (PR_ID can be a number or full URL)")
            return

        if text == "all":
            user_keys = [k for k in self._prs if k[0] == sender_email]
            if not user_keys:
                self._webex.send_room_message("You are not monitoring any PRs.")
                return
            for k in user_keys:
                del self._prs[k]
            self._webex.send_room_message(
                f"\u23F9\uFE0F Stopped monitoring all **{len(user_keys)}** of your PRs."
            )
            return

        num = self._resolve_pr_number(text)
        if num is None:
            self._webex.send_room_message(f"Could not parse: `{text}`. Use a PR number or full URL.")
            return
        key = self._pr_key(sender_email, num)
        if key in self._prs:
            del self._prs[key]
            self._webex.send_room_message(f"\u23F9\uFE0F Stopped monitoring PR #{num}.")
        else:
            self._webex.send_room_message(f"You are not monitoring PR #{num}.")

    def _cmd_status(self, sender_email: str, args: str) -> None:
        text = args.strip().lower()
        if not text:
            self._webex.send_room_message("Usage: `status <PR_ID>` or `status all`  (PR_ID can be a number or full URL)")
            return

        if text == "all":
            user_prs = self._user_prs(sender_email)
            if not user_prs:
                self._webex.send_room_message("You are not monitoring any PRs.")
                return
            for num in sorted(user_prs.keys()):
                self._fetch_and_report(sender_email, num, force=True)
            return

        num = self._resolve_pr_number(text)
        if num is None:
            self._webex.send_room_message(f"Could not parse: `{text}`. Use a PR number or full URL.")
            return
        key = self._pr_key(sender_email, num)
        if key not in self._prs:
            self._webex.send_room_message(f"You are not monitoring PR #{num}.")
            return
        self._fetch_and_report(sender_email, num, force=True)

    def _cmd_precommit(self, sender_email: str, args: str) -> None:
        parts = args.strip().split(maxsplit=1)
        if len(parts) < 2:
            self._webex.send_room_message(
                "Usage: `precommit <PR_ID> <TEST_TAG>`\n"
                "Examples: `precommit 3473 ROUTING` or `precommit https://...pull/530 L2`\n\n"
                "Updates the PR **description** (CICD precommit section):\n"
                "`**TEST_TAG**: <TEST_TAG>` (e.g. `routing` \u2192 `ROUTING`)."
            )
            return
        gh = self._require_gh(sender_email)
        if not gh:
            return

        resolved = self._resolve_pr_full(parts[0])
        if not resolved:
            self._webex.send_room_message(
                f"Could not parse PR: `{parts[0]}`. Use a number or full URL."
            )
            return
        owner, repo, num = resolved
        test_tag = parts[1].strip()
        if not test_tag:
            self._webex.send_room_message("**TEST_TAG** cannot be empty.")
            return

        try:
            out = gh.update_pr_precommit_test_tag(owner, repo, num, test_tag)
            lines = [
                f"\u2705 Set **`TEST_TAG`: {out['test_tag']}** in the PR description for "
                f"[PR #{num}]({out['html_url']}).",
            ]
            if out.get("auto_trigger_enabled"):
                ps = self._ensure_monitoring_pr(sender_email, owner, repo, num)
                if ps:
                    ps.precommit_monitor = PrecommitMonitor(user_tag=out["test_tag"])
                    lines.append(
                        "\n\n\U0001F50D **Enable Precommit Auto-Trigger** is checked — "
                        f"monitoring **precommit** checks (poll every **{PRECOMMIT_MONITOR_POLL_SEC}s**).\n"
                        "On failure: **`TEST_TAG`** \u2192 **Default**, wait **1 min**, "
                        f"restore **`{out['test_tag']}`** and retry once."
                    )
                    self._poll_precommit_monitors()
            else:
                lines.append(
                    "\n\n\u26A0\uFE0F **Enable Precommit Auto-Trigger** is not checked — "
                    "check the box in the CICD section to auto-run precommit; not monitoring."
                )
            self._webex.send_room_message(markdown="".join(lines))
        except requests.exceptions.HTTPError as exc:
            detail = GitHubEnterpriseClient.format_http_error(exc).replace("`", "'")
            logger.exception(
                "precommit HTTP error %s/%s#%d tag=%s: %s",
                owner, repo, num, test_tag, detail,
            )
            self._webex.send_room_message(
                markdown=(
                    f"\u274C **Failed to update TEST_TAG** on PR #{num}:\n```\n{detail}\n```"
                ),
            )
        except Exception:
            logger.exception(
                "precommit failed %s/%s#%d tag=%s", owner, repo, num, test_tag
            )
            self._webex.send_room_message(
                f"\u274C Failed to update **TEST_TAG** on PR #{num}. See **bot.log**."
            )

    def _cmd_comment(self, sender_email: str, args: str) -> None:
        parts = args.strip().split(maxsplit=1)
        if len(parts) < 2:
            self._webex.send_room_message(
                "Usage: `comment <PR_ID> <text>`\n"
                "Examples: `comment 3473 LGTM` or `comment https://...pull/530 LGTM`"
            )
            return
        gh = self._require_gh(sender_email)
        if not gh:
            return

        num = self._resolve_pr_number(parts[0])
        if num is None:
            self._webex.send_room_message(f"Could not parse PR: `{parts[0]}`. Use a number or full URL.")
            return
        text = parts[1].strip()
        ps = self._get_or_auto_monitor(sender_email, num)
        if not ps:
            return

        pr = ps.info
        url = gh.post_comment(pr.owner, pr.repo, pr.number, text)
        self._webex.send_room_message(
            markdown=f"\u2705 Comment posted on PR #{pr.number}: [view]({url})"
        )

    def _cmd_automerge(self, sender_email: str, args: str) -> None:
        parts = args.strip().split(maxsplit=1)
        if len(parts) < 2:
            self._webex.send_room_message(
                "Usage: `automerge <PR_ID> <on/off>`  Example: `automerge 3473 on` or full URL"
            )
            return
        num = self._resolve_pr_number(parts[0])
        if num is None:
            self._webex.send_room_message(f"Could not parse PR: `{parts[0]}`. Use a number or full URL.")
            return
        gh = self._require_gh(sender_email)
        if not gh:
            return
        toggle = parts[1].strip().lower()
        ps = self._get_or_auto_monitor(sender_email, num)
        if not ps:
            self._webex.send_room_message(f"Could not fetch PR #{num}.")
            return

        if toggle in ("on", "enable", "yes"):
            pr = ps.info
            if not pr.node_id:
                fresh = gh.get_pr(pr.owner, pr.repo, pr.number)
                ps.info = fresh
                pr = fresh
            if not pr.node_id:
                self._webex.send_room_message(
                    f"\u274C Could not read PR **node id** for #{num}; cannot enable auto-merge."
                )
                return
            ok = gh.enable_auto_merge(pr.node_id)
            if ok:
                ps.auto_merge = True
                self._webex.send_room_message(
                    markdown=(
                        f"\U0001F504 **GitHub auto-merge enabled** for "
                        f"**[PR #{num}: {pr.title}]({pr.html_url})** in `{pr.owner}/{pr.repo}`.\n\n"
                        "GitHub will merge when **required checks** for the branch pass "
                        "(same behavior as **Enable auto-merge** in the web UI). "
                        "Use `automerge off` to cancel."
                    )
                )
            else:
                self._webex.send_room_message(
                    markdown=(
                        f"\u274C **Could not enable GitHub auto-merge** on PR #{num}. "
                        "Typical causes: PR not mergeable, missing **merge** permission, "
                        "branch protection disallows auto-merge, or pending reviews."
                    )
                )
        elif toggle in ("off", "disable", "no"):
            if ps.info.node_id:
                gh.disable_auto_merge(ps.info.node_id)
            ps.auto_merge = False
            self._webex.send_room_message(
                f"\U0001F534 **GitHub auto-merge disabled** for PR #{num}."
            )
        else:
            self._webex.send_room_message("Use `on` or `off`. Example: `automerge 3473 on`")

    def _cmd_approve(self, sender_email: str, args: str) -> None:
        text = args.strip()
        if not text:
            self._webex.send_room_message("Usage: `approve <PR_ID>`  Example: `approve 3473` or full URL")
            return
        gh = self._require_gh(sender_email)
        if not gh:
            return

        resolved = self._resolve_pr_full(text)
        if not resolved:
            self._webex.send_room_message(f"Could not parse: `{text}`. Use a PR number or full URL.")
            return

        owner, repo, number = resolved
        try:
            ok = gh.approve_pr(owner, repo, number)
        except Exception:
            logger.exception("Failed to approve PR %s/%s#%d", owner, repo, number)
            ok = False

        if ok:
            rec = self._user_store.get(sender_email)
            gh_user = rec.github_user if rec else sender_email
            self._webex.send_room_message(
                markdown=f"\U0001F7E2 **{gh_user}** approved PR #{number} in `{owner}/{repo}`."
            )
        else:
            self._webex.send_room_message(
                f"Failed to approve PR #{number} in `{owner}/{repo}`. "
                f"Check permissions or if you've already approved."
            )

    def _cmd_unapprove(self, sender_email: str, args: str) -> None:
        text = args.strip()
        if not text:
            self._webex.send_room_message("Usage: `unapprove <PR_ID>`  Example: `unapprove 3473` or full URL")
            return
        gh = self._require_gh(sender_email)
        if not gh:
            return

        resolved = self._resolve_pr_full(text)
        if not resolved:
            self._webex.send_room_message(f"Could not parse: `{text}`. Use a PR number or full URL.")
            return

        owner, repo, number = resolved
        try:
            ok = gh.unapprove_pr(owner, repo, number)
        except Exception:
            logger.exception("Failed to unapprove PR %s/%s#%d", owner, repo, number)
            ok = False

        if ok:
            self._webex.send_room_message(
                markdown=f"\U0001F534 **Approval withdrawn** for PR #{number} in `{owner}/{repo}`."
            )
        else:
            self._webex.send_room_message(
                f"Failed to unapprove PR #{number} in `{owner}/{repo}`. Check permissions."
            )

    def _cmd_merge(self, sender_email: str, args: str) -> None:
        text = args.strip()
        if not text:
            self._webex.send_room_message("Usage: `merge <PR_ID>`  Example: `merge 3473` or full URL")
            return
        gh = self._require_gh(sender_email)
        if not gh:
            return

        resolved = self._resolve_pr_full(text)
        if not resolved:
            self._webex.send_room_message(f"Could not parse: `{text}`. Use a PR number or full URL.")
            return

        owner, repo, number = resolved
        ok = gh.merge_pr(owner, repo, number)
        if ok:
            self._webex.send_room_message(
                markdown=f"\U0001F389 **Merged** PR #{number} in `{owner}/{repo}`!"
            )
        else:
            self._webex.send_room_message(
                f"Failed to merge PR #{number} in `{owner}/{repo}`. "
                f"Check if all required checks have passed and branch protection allows it."
            )

    def _cmd_backport(self, sender_email: str, args: str) -> None:
        """Squash cherry-pick a PR's diff onto target_branch and open a new backport PR."""
        parts = args.strip().split()
        if len(parts) != 2:
            self._reply(
                sender_email,
                markdown=(
                    "**Usage:** `backport <pull_request> <target_branch>`\n\n"
                    "Cherry-picks the PR's full diff onto **target_branch** as a single squashed "
                    "commit and opens a new PR `backport/pr-<n>-<target>-<ts>` \u2192 **target_branch**.\n\n"
                    "Conflicts are reported back so you can resolve manually \u2014 the bot won't "
                    "guess. The original PR's `base` does not need to equal **target_branch**.\n\n"
                    "**Example:**\n"
                    "`backport https://github.example.com/whitebox/sonic-dhcp-relay/pull/40 c-master`"
                ),
            )
            return

        pr_spec, target_branch = parts[0], parts[1]
        resolved = self._resolve_pr_full(pr_spec)
        if not resolved:
            self._reply(
                sender_email,
                markdown=(
                    f"Could not parse PR from `{pr_spec}`. "
                    "Use a full PR URL or numeric ID (with **DEFAULT_REPO** set)."
                ),
            )
            return

        owner, repo, number = resolved
        gh = self._require_gh(sender_email)
        if not gh:
            return

        try:
            out = gh.backport_pr(owner, repo, number, target_branch)
            new_pr_url = out.get("new_pr_url") or ""
            new_pr_num = out.get("new_pr_number")
            commit_sha = str(out.get("commit_sha") or "")[:7]
            self._reply(
                sender_email,
                markdown=(
                    f"\u2705 **Backport** of [PR #{number}]({out.get('original_html_url') or '#'}) "
                    f"opened on `{owner}/{repo}`\n\n"
                    f"- **New PR:** [#{new_pr_num}]({new_pr_url})\n"
                    f"- **Branch:** `{out.get('new_branch', '')}` \u2192 `{out.get('target_branch', '')}`\n"
                    f"- **Backport commit:** `{commit_sha}`"
                ),
            )
        except ValueError as exc:
            self._reply(sender_email, markdown=f"\u274C **Backport not done** — {exc}")
        except requests.exceptions.HTTPError as exc:
            detail = GitHubEnterpriseClient.format_http_error(exc)
            self._reply(
                sender_email,
                markdown=(
                    "\u274C **Backport failed** \u2014 GitHub error:\n\n"
                    f"```\n{detail}\n```\n\n"
                    "**Typical causes:** missing **push** permission on the repo, **branch "
                    "protection** rules on the target, **422** if a backport branch with the "
                    "same name already exists, or **404** if `target_branch` was deleted."
                ),
            )

    @staticmethod
    def _resolve_jenkins_job_path(spec: str) -> Optional[str]:
        """Return Jenkins job path (e.g. ``Update_Golden_Code`` or ``folder/job``) from URL or path."""
        s = spec.strip().strip('"').strip("'")
        if not s:
            return None
        if s.lower().startswith(("http://", "https://")) and "/job/" in s.lower():
            parsed = parse_jenkins_job_url(s)
            return parsed[1] if parsed else None
        return s.strip().strip("/") or None

    def _cmd_jenkins_last(self, sender_email: str, args: str) -> None:
        """Show last build for a Jenkins job via configured server API."""
        if not self._jenkins:
            self._reply(
                sender_email,
                markdown=(
                    "Jenkins API is **not** configured on this bot. Set **JENKINS_BASE_URL** and "
                    "**JENKINS_API_TOKEN** (and optional **JENKINS_USER**) on the server."
                ),
            )
            return
        spec = args.strip()
        if not spec:
            self._reply(
                sender_email,
                markdown=(
                    "**Usage:** `jenkins_last <job URL or job/path>`\n\n"
                    "**Examples:**\n"
                    "`jenkins_last https://jenkins-sonic.cisco.com/job/Update_Golden_Code/`\n"
                    "`jenkins_last Update_Golden_Code`"
                ),
            )
            return
        job_path = self._resolve_jenkins_job_path(spec)
        if not job_path:
            self._reply(
                sender_email,
                markdown=f"Could not parse Jenkins job from `{spec[:200]}`.",
            )
            return
        try:
            job = self._jenkins.get_job_json(
                job_path,
                tree="name,url,lastBuild[number,url,result,timestamp,duration,inProgress]",
            )
            lb = job.get("lastBuild")
            if not lb:
                self._reply(
                    sender_email,
                    markdown=f"No builds yet for **{job.get('name', job_path)}**.",
                )
                return
            n = lb.get("number")
            url = lb.get("url", "")
            res = lb.get("result") or ("in progress" if lb.get("inProgress") else "unknown")
            dur_ms = int(lb.get("duration") or 0)
            dur_s = dur_ms // 1000 if dur_ms else 0
            self._reply(
                sender_email,
                markdown=(
                    f"**Jenkins** `{job.get('name', job_path)}` — last build **#{n}**\n\n"
                    f"- **Result:** `{res}`\n"
                    f"- **Duration:** {dur_s}s\n"
                    f"- **URL:** {url}"
                ),
            )
        except requests.exceptions.HTTPError as exc:
            self._reply(
                sender_email,
                markdown=f"\u274C Jenkins API error:\n```\n{JenkinsClient.format_http_error(exc)}\n```",
            )

    def _cmd_jenkins_build(self, sender_email: str, args: str) -> None:
        """Queue a Jenkins build (optionally with parameters)."""
        if not self._jenkins:
            self._reply(
                sender_email,
                markdown=(
                    "Jenkins API is **not** configured on this bot. Set **JENKINS_BASE_URL** and "
                    "**JENKINS_API_TOKEN** (and optional **JENKINS_USER**) on the server."
                ),
            )
            return
        parts = args.strip().split()
        if not parts:
            self._reply(
                sender_email,
                markdown=(
                    "**Usage:** `jenkins_build <job URL or job/path> [KEY=VAL ...]`\n\n"
                    "With no **KEY=VAL** pairs, queues a plain **build**. With parameters, uses "
                    "**buildWithParameters**.\n\n"
                    "**Examples:**\n"
                    "`jenkins_build https://jenkins-sonic.cisco.com/job/Update_Golden_Code/`\n"
                    "`jenkins_build Update_Golden_Code BRANCH=202405c`"
                ),
            )
            return
        job_path = self._resolve_jenkins_job_path(parts[0])
        if not job_path:
            self._reply(
                sender_email,
                markdown=f"Could not parse Jenkins job from `{parts[0][:200]}`.",
            )
            return
        params: Dict[str, str] = {}
        for raw in parts[1:]:
            if "=" not in raw:
                self._reply(
                    sender_email,
                    markdown=f"\u274C Invalid parameter `{raw}` — use **KEY=VALUE** (no spaces around `=`).",
                )
                return
            k, v = raw.split("=", 1)
            params[k.strip()] = v.strip()
        try:
            loc = self._jenkins.build(job_path, params if params else None)
            extra = f"\n**Queue:** `{loc}`" if loc else ""
            self._reply(
                sender_email,
                markdown=(
                    f"\u2705 **Build queued** for Jenkins job `{job_path}`."
                    f"{extra}"
                ),
            )
        except requests.exceptions.HTTPError as exc:
            self._reply(
                sender_email,
                markdown=f"\u274C Jenkins **build** failed:\n```\n{JenkinsClient.format_http_error(exc)}\n```",
            )

    def _cmd_update_branch(self, sender_email: str, args: str, method: str = "MERGE") -> None:
        text = args.strip()
        label = "rebase" if method == "REBASE" else "merge"
        if not text:
            self._webex.send_room_message(
                f"Usage: `update_branch_{label} <PR_ID>`  "
                f"Example: `update_branch_{label} 3473` or full URL"
            )
            return
        gh = self._require_gh(sender_email)
        if not gh:
            return

        resolved = self._resolve_pr_full(text)
        if not resolved:
            self._webex.send_room_message(f"Could not parse: `{text}`. Use a PR number or full URL.")
            return

        owner, repo, number = resolved
        try:
            pr_data = gh.get_pr(owner, repo, number)
        except Exception:
            self._webex.send_room_message(f"Failed to fetch PR #{number} in `{owner}/{repo}`.")
            return

        ok = gh.update_branch(pr_data.node_id, method=method)
        if ok:
            self._webex.send_room_message(
                markdown=(
                    f"\U0001F504 **Branch updated** ({label}) for "
                    f"**[PR #{number}]({pr_data.html_url})** in `{owner}/{repo}`"
                )
            )
        else:
            self._webex.send_room_message(
                f"Failed to update branch ({label}) for PR #{number}. "
                f"The branch may already be up to date or conflicts may need manual resolution."
            )

    def _cmd_update_branch_rebase(self, sender_email: str, args: str) -> None:
        self._cmd_update_branch(sender_email, args, method="REBASE")

    def _cmd_update_branch_merge(self, sender_email: str, args: str) -> None:
        self._cmd_update_branch(sender_email, args, method="MERGE")

    @staticmethod
    def _raise_hash_github_error_markdown(detail: str) -> str:
        """Webex markdown for a failed submodule bump after GitHub HTTPError."""
        return (
            "\u274C **Submodule bump failed** — GitHub returned an error:\n\n"
            f"```\n{detail}\n```\n\n"
            "**How to read this**\n"
            "- **401 / 403** — PAT cannot access the repo, **SSO** not authorized for the org, "
            "or missing **write** (push branch + open PR) on the **parent** repo.\n"
            "- **404** — wrong parent or **target_branch**; `.gitmodules` or submodule path missing "
            "on that ref; or submodule remote/branch not found.\n"
            "- **409** — merge conflict or transient ref update issue.\n"
            "- **422** — branch protection, invalid **base**/**head**, or other validation. "
            "(If an open PR already used this **head**→**base**, the bot **reuses** it after updating the branch. "
            "The bot also **resets** the bump **head** ref if GitHub says it already exists.)\n\n"
            "Confirm **parent**, **target_branch**, and **sub** hint; ensure your token matches the "
            "GitHub user that is allowed to push to **whitebox/sonic-buildimage** (or whichever parent you passed)."
        )

    def _cmd_raise_hash_update(self, sender_email: str, args: str) -> None:
        """Bump a submodule in a parent repo to latest on an inferred branch; open PR to target_branch."""
        parts = args.strip().split()
        if len(parts) < 3:
            self._reply(
                sender_email,
                markdown=(
                    "**Usage:** `raise_hash_update <parent_ref> <sub> <Jira_No> [parent_owner/parent_repo]`\n\n"
                    "**Examples:**\n"
                    "`raise_hash_update 202405c sonic-utilities MIGSOFTWAR-39128`  _(release ref **202405c** on parent)_\n"
                    "`raise_hash_update c-master frr MIGSOFTWAR-39128`  _(FRR on **c-master**; updates **rules/frr.mk**)_\n"
                    "`raise_hash_update c-master sonic-mgmt-common,sonic-swss MIGSOFTWAR-35315`  "
                    "_(comma-separated **sub** hints \u2192 **one PR** with each submodule gitlink updated)_\n"
                    "`raise_hash_update master sonic-utilities CSCwj12345`\n\n"
                    "- **parent_ref** — branch or tag on **sonic-buildimage** (first reads `.gitmodules` / gitlinks there; PR **base**).\n"
                    "- **sub** — hint from **`.gitmodules`**: `frr`, path suffix, `owner/repo`, etc. "
                    "Use **commas** to bump **several** submodules in **one** commit/PR (each hint must resolve uniquely).\n"
                    "- Submodule **tip** — uses **`.gitmodules` `branch =`** for that entry **first** (all submodules), "
                    "then default branch, parent ref name, `master`/`main`, then other branches.\n"
                    "- **FRR only** — also updates **`rules/frr.mk`** (`FRR_TAG` = new full submodule SHA).\n"
                    "- If several entries match, refine the hint (e.g. include `path/` or `owner/repo`).\n"
                    "- **parent** defaults to `DEFAULT_REPO` if omitted."
                ),
            )
            return

        target_branch = parts[0].strip()
        repo_hint = parts[1].strip()
        jira_no = parts[2].strip()
        parent_spec = parts[3].strip() if len(parts) > 3 else None

        gh = self._require_gh(sender_email)
        if not gh:
            return

        resolved = self._resolve_parent_owner_repo(parent_spec)
        if not resolved:
            self._reply(
                sender_email,
                markdown=(
                    "Set **DEFAULT_REPO** in the environment or pass **parent_owner/parent_repo** "
                    "(fourth argument)."
                ),
            )
            return

        parent_owner, parent_repo = resolved
        try:
            out = gh.raise_submodule_hash_pr(
                parent_owner,
                parent_repo,
                target_branch,
                repo_hint,
                jira_no,
            )
            sub_detail = (out.get("webex_submodules_md") or "").strip()
            frr_line = (
                "\n- **`rules/frr.mk`:** `FRR_TAG` updated to match the new FRR gitlink."
                if (out.get("frr_mk_updated") or "").strip()
                else ""
            )
            self._reply(
                sender_email,
                markdown=(
                    f"\u2705 **Opened submodule bump PR** into `{target_branch}` on "
                    f"`{parent_owner}/{parent_repo}`\n\n"
                    f"- **PR:** [PR #{out['number']}]({out['html_url']})\n"
                    f"- **Head branch:** `{out['head_branch']}`\n"
                    f"{sub_detail}{frr_line}"
                    + _raise_hash_webex_cicd_summary_footer()
                ),
            )
        except SubmoduleAmbiguousError as exc:
            lines = [
                f"\u26A0\uFE0F **Multiple** `.gitmodules` entries match `{repo_hint}` on "
                f"`{parent_owner}/{parent_repo}` @ `{target_branch}`.",
                "",
                "Re-run **`raise_hash_update`** with a more specific hint "
                "(e.g. include `path/` or `owner/repo`):",
            ]
            for i, (p, so, sr, br) in enumerate(exc.matches, 1):
                br_s = f" (`.gitmodules` branch=`{br}`)" if (br or "").strip() else ""
                lines.append(f"{i}. `{p}` \u2192 `{so}/{sr}`{br_s}")
            self._reply(sender_email, markdown="\n".join(lines))
        except ValueError as exc:
            self._reply(sender_email, markdown=f"\u26A0\uFE0F {exc}")
        except requests.exceptions.HTTPError as exc:
            detail = GitHubEnterpriseClient.format_http_error(exc).replace("`", "'")
            logger.exception(
                "raise_hash_update HTTP error parent=%s/%s branch=%s repo=%s: %s",
                parent_owner, parent_repo, target_branch, repo_hint, detail,
            )
            self._reply(
                sender_email,
                markdown=self._raise_hash_github_error_markdown(detail),
            )
        except requests.exceptions.RequestException as exc:
            logger.exception(
                "raise_hash_update network error parent=%s/%s",
                parent_owner, parent_repo,
            )
            self._reply(
                sender_email,
                markdown=(
                    f"\u274C **Submodule bump failed** — could not reach GitHub "
                    f"(`{type(exc).__name__}`). Check VPN/network and **GITHUB_BASE_URL**."
                ),
            )
        except Exception as exc:
            logger.exception(
                "raise_hash_update failed parent=%s/%s branch=%s repo=%s",
                parent_owner, parent_repo, target_branch, repo_hint,
            )
            hint = str(exc).strip().replace("`", "'")[:400]
            extra = f"\n\n`{hint}`" if hint else ""
            self._reply(
                sender_email,
                markdown=(
                    "\u274C **Submodule bump failed** — unexpected error."
                    f"{extra}\n\nSee server **bot.log** for the full traceback."
                ),
            )

    def _cmd_raise_hash_update_for_all_sub_repos(self, sender_email: str, args: str) -> None:
        """Bump every stale submodule on the parent in one PR (skips up-to-date / unreachable)."""
        parts = args.strip().split()
        if len(parts) < 2:
            self._reply(
                sender_email,
                markdown=(
                    "**Usage:** `raise_hash_update_for_all_sub_repos <parent_ref> <Jira_No> [parent_owner/parent_repo]`\n\n"
                    "Walks **`.gitmodules`** on the parent, identifies every submodule whose "
                    "pinned gitlink is **behind** its tracked branch tip, and opens **one** PR "
                    "bumping all of them.\n\n"
                    "- Submodules already at tip are **skipped silently**.\n"
                    "- Submodules whose remote can't be resolved are **skipped** and reported.\n"
                    "- For **FRR**, **`rules/frr.mk`** is also updated (same as `raise_hash_update`).\n"
                    "- **parent** defaults to `DEFAULT_REPO` if omitted.\n\n"
                    "**Examples:**\n"
                    "`raise_hash_update_for_all_sub_repos c-master MIGSOFTWAR-39128`\n"
                    "`raise_hash_update_for_all_sub_repos 202405c MIGSOFTWAR-39128 whitebox/sonic-buildimage`"
                ),
            )
            return

        target_branch = parts[0].strip()
        jira_no = parts[1].strip()
        parent_spec = parts[2].strip() if len(parts) > 2 else None

        gh = self._require_gh(sender_email)
        if not gh:
            return

        resolved = self._resolve_parent_owner_repo(parent_spec)
        if not resolved:
            self._reply(
                sender_email,
                markdown=(
                    "Set **DEFAULT_REPO** in the environment or pass **parent_owner/parent_repo** "
                    "(third argument)."
                ),
            )
            return

        parent_owner, parent_repo = resolved
        try:
            out = gh.raise_all_stale_submodules_pr(
                parent_owner,
                parent_repo,
                target_branch,
                jira_no,
            )
            sub_detail = (out.get("webex_submodules_md") or "").strip()
            frr_line = (
                "\n- **`rules/frr.mk`:** `FRR_TAG` updated to match the new FRR gitlink."
                if str(out.get("frr_mk_updated") or "").strip()
                else ""
            )
            total = out.get("all_total") or "?"
            stale = out.get("all_stale") or "?"
            up_to_date = (out.get("all_up_to_date") or "").strip()
            skipped = (out.get("all_skipped") or "").strip()
            extras = []
            if up_to_date:
                already = up_to_date.split(",")
                extras.append(
                    f"\n- **Already at tip:** {len(already)} submodule(s) "
                    f"(skipped)"
                )
            if skipped:
                extras.append(f"\n- **Skipped (resolve failure):** {skipped}")
            self._reply(
                sender_email,
                markdown=(
                    f"\u2705 **Opened bulk submodule bump PR** into `{target_branch}` on "
                    f"`{parent_owner}/{parent_repo}`\n\n"
                    f"- **PR:** [PR #{out['number']}]({out['html_url']})\n"
                    f"- **Head branch:** `{out['head_branch']}`\n"
                    f"- **Stale / total submodules:** **{stale}** / **{total}**\n"
                    f"{sub_detail}{frr_line}"
                    + "".join(extras)
                    + _raise_hash_webex_cicd_summary_footer()
                ),
            )
        except ValueError as exc:
            self._reply(sender_email, markdown=f"\u26A0\uFE0F {exc}")
        except requests.exceptions.HTTPError as exc:
            detail = GitHubEnterpriseClient.format_http_error(exc).replace("`", "'")
            logger.exception(
                "raise_hash_update_for_all_sub_repos HTTP error parent=%s/%s branch=%s: %s",
                parent_owner, parent_repo, target_branch, detail,
            )
            self._reply(
                sender_email,
                markdown=self._raise_hash_github_error_markdown(detail),
            )
        except requests.exceptions.RequestException as exc:
            logger.exception(
                "raise_hash_update_for_all_sub_repos network error parent=%s/%s",
                parent_owner, parent_repo,
            )
            self._reply(
                sender_email,
                markdown=(
                    f"\u274C **Bulk submodule bump failed** \u2014 could not reach GitHub "
                    f"(`{type(exc).__name__}`). Check VPN/network and **GITHUB_BASE_URL**."
                ),
            )
        except Exception as exc:
            logger.exception(
                "raise_hash_update_for_all_sub_repos failed parent=%s/%s branch=%s",
                parent_owner, parent_repo, target_branch,
            )
            hint = str(exc).strip().replace("`", "'")[:400]
            extra = f"\n\n`{hint}`" if hint else ""
            self._reply(
                sender_email,
                markdown=(
                    "\u274C **Bulk submodule bump failed** \u2014 unexpected error."
                    f"{extra}\n\nSee server **bot.log** for the full traceback."
                ),
            )

    def _cmd_get_summary(self, sender_email: str, args: str) -> None:
        text = args.strip()
        if not text:
            self._webex.send_room_message("Usage: `summary <PR_ID>`  Example: `summary 3473` or full URL")
            return
        gh = self._require_gh(sender_email)
        if not gh:
            return

        resolved = self._resolve_pr_full(text)
        if not resolved:
            self._webex.send_room_message(f"Could not parse: `{text}`. Use a PR number or full URL.")
            return

        owner, repo, number = resolved
        try:
            s = gh.get_pr_summary(owner, repo, number)
        except Exception:
            logger.exception("Failed to get summary for %s/%s#%d", owner, repo, number)
            self._webex.send_room_message(f"Failed to fetch PR #{number} in `{owner}/{repo}`.")
            return

        review_lines = []
        for user, state in sorted(s["reviews"].items()):
            icon = "\U0001F7E2" if state == "APPROVED" else "\U0001F534"
            review_lines.append(f"  {icon} **{user}** — {state}")

        file_lines = []
        for f in s["files"][:15]:
            file_lines.append(
                f"  `{f['status'][:1].upper()}` `{f['name']}` "
                f"(+{f['additions']} -{f['deletions']})"
            )
        if len(s["files"]) > 15 or s["file_count_truncated"]:
            file_lines.append("  _...and more_")

        merge_icon = "\U0001F7E2" if s["mergeable"] else "\U0001F534"
        auto_merge_str = " | \U0001F504 auto-merge ON" if s["auto_merge"] else ""

        lines = [
            f"**[PR #{number}: {s['title']}]({s['html_url']})** ({owner}/{repo})",
            f"by **{s['user']}** | `{s['head_branch']}` \u2192 `{s['base_branch']}`",
            f"State: **{s['state']}** | {merge_icon} mergeable: "
            f"**{s['mergeable_state']}**{auto_merge_str}",
            "",
            f"\U0001F4CA **{s['commits']}** commits | "
            f"**{s['changed_files']}** files | "
            f"+{s['additions']} -{s['deletions']}",
            "",
        ]

        if s["body"]:
            body_preview = s["body"].replace("\r\n", "\n").split("\n")
            preview = "\n".join(body_preview[:5])
            if len(body_preview) > 5:
                preview += "\n..."
            lines.append(f"**Description:**\n{preview}")
            lines.append("")

        if review_lines:
            lines.append("**Reviews:**")
            lines.extend(review_lines)
            lines.append("")

        lines.append("**Files changed:**")
        lines.extend(file_lines)

        self._webex.send_room_message(markdown="\n".join(lines))

    def _cmd_get_full_summary(self, sender_email: str, args: str) -> None:
        text = args.strip()
        if not text:
            self._webex.send_room_message("Usage: `full_summary <PR_ID>`  Example: `full_summary 3473` or full URL")
            return
        gh = self._require_gh(sender_email)
        if not gh:
            return

        resolved = self._resolve_pr_full(text)
        if not resolved:
            self._webex.send_room_message(f"Could not parse: `{text}`. Use a PR number or full URL.")
            return

        owner, repo, number = resolved
        try:
            s = gh.get_full_pr_summary(owner, repo, number)
        except Exception:
            logger.exception("Failed to get full summary for %s/%s#%d", owner, repo, number)
            self._webex.send_room_message(f"Failed to fetch PR #{number} in `{owner}/{repo}`.")
            return

        merge_status = ""
        if s["merged"]:
            merge_status = f" | \U0001F389 Merged by **{s['merged_by']}** at {s['merged_at']}"
        auto_merge_str = " | \U0001F504 auto-merge ON" if s["auto_merge"] else ""

        lines = [
            f"**[PR #{number}: {s['title']}]({s['html_url']})** ({owner}/{repo})",
            f"by **{s['user']}** | `{s['head_branch']}` \u2192 `{s['base_branch']}`",
            f"State: **{s['state']}**{merge_status}{auto_merge_str}",
            f"Created: {s['created_at']} | Updated: {s['updated_at']}",
        ]

        if s["labels"]:
            lines.append(f"Labels: {', '.join(f'`{l}`' for l in s['labels'])}")
        if s["assignees"]:
            lines.append(f"Assignees: {', '.join(f'**{a}**' for a in s['assignees'])}")

        lines.append("")
        lines.append(
            f"\U0001F4CA **{s['commits']}** commits | "
            f"**{s['changed_files']}** files | "
            f"+{s['additions']} -{s['deletions']}"
        )

        if s["body"]:
            body_lines = s["body"].replace("\r\n", "\n").split("\n")
            preview = "\n".join(body_lines[:10])
            if len(body_lines) > 10:
                preview += "\n..."
            lines.append("")
            lines.append(f"**Description:**\n{preview}")

        if s["reviews"]:
            lines.append("")
            lines.append("**Reviews:**")
            for r in s["reviews"]:
                if not r["state"] or r["state"] == "COMMENTED":
                    continue
                icon = {
                    "APPROVED": "\U0001F7E2",
                    "CHANGES_REQUESTED": "\U0001F534",
                    "DISMISSED": "\u26AA",
                }.get(r["state"], "\u2753")
                body_snippet = ""
                if r["body"]:
                    body_snippet = f" — _{r['body'][:100]}_"
                lines.append(f"  {icon} **{r['user']}** {r['state']}{body_snippet}")

        if s["comments"]:
            lines.append("")
            lines.append(f"**Comments ({len(s['comments'])}):**")
            for c in s["comments"][-20:]:
                body_preview = c["body"].replace("\n", " ")[:150]
                lines.append(f"  \U0001F4AC **{c['user']}** ({c['created_at'][:10]}):")
                lines.append(f"  {body_preview}")

        if s["inline_comments"]:
            lines.append("")
            lines.append(f"**Inline review comments ({len(s['inline_comments'])}):**")
            for c in s["inline_comments"][-15:]:
                body_preview = c["body"].replace("\n", " ")[:120]
                line_ref = f"L{c['line']}" if c["line"] else ""
                lines.append(f"  \U0001F4DD **{c['user']}** on `{c['path']}`{line_ref}:")
                lines.append(f"  {body_preview}")

        lines.append("")
        lines.append("**Files changed:**")
        for f in s["files"][:20]:
            lines.append(
                f"  `{f['status'][:1].upper()}` `{f['name']}` "
                f"(+{f['additions']} -{f['deletions']})"
            )
        if len(s["files"]) > 20:
            lines.append(f"  _...and {len(s['files']) - 20} more files_")

        self._webex.send_room_message(markdown="\n".join(lines))

    def _cmd_get_diff(self, sender_email: str, args: str) -> None:
        text = args.strip()
        if not text:
            self._webex.send_room_message("Usage: `diff <PR_ID>`  Example: `diff 3473` or full URL")
            return
        gh = self._require_gh(sender_email)
        if not gh:
            return

        resolved = self._resolve_pr_full(text)
        if not resolved:
            self._webex.send_room_message(f"Could not parse: `{text}`. Use a PR number or full URL.")
            return

        owner, repo, number = resolved
        try:
            raw_diff = gh.get_pr_diff(owner, repo, number)
        except Exception:
            logger.exception("Failed to get diff for %s/%s#%d", owner, repo, number)
            self._webex.send_room_message(f"Failed to fetch diff for PR #{number} in `{owner}/{repo}`.")
            return

        if not raw_diff.strip():
            self._webex.send_room_message(f"PR #{number} in `{owner}/{repo}` has no code changes.")
            return

        MAX_MSG_LEN = 7000
        header = f"**Diff for [PR #{number}](https://github.example.com/{owner}/{repo}/pull/{number})** (`{owner}/{repo}`):\n\n"
        diff_block = f"```diff\n{raw_diff}\n```"

        if len(header) + len(diff_block) <= MAX_MSG_LEN:
            self._webex.send_room_message(markdown=header + diff_block)
        else:
            budget = MAX_MSG_LEN - len(header) - len("```diff\n\n```") - 60
            truncated = raw_diff[:budget]
            last_nl = truncated.rfind("\n")
            if last_nl > 0:
                truncated = truncated[:last_nl]
            footer = (
                f"\n\n_... diff truncated ({len(raw_diff):,} chars total). "
                f"[View full diff on GitHub]"
                f"(https://github.example.com/{owner}/{repo}/pull/{number}/files)_"
            )
            self._webex.send_room_message(
                markdown=header + f"```diff\n{truncated}\n```" + footer
            )

    def _cmd_notify(self, sender_email: str, args: str) -> None:
        parts = args.strip().split(maxsplit=1)
        if len(parts) < 2:
            self._webex.send_room_message(
                "Usage: `notify <PR_ID> <username>`  Example: `notify 3473 johndoe` or full URL"
            )
            return
        num = self._resolve_pr_number(parts[0])
        if num is None:
            self._webex.send_room_message(f"Could not parse PR: `{parts[0]}`. Use a number or full URL.")
            return
        username = parts[1].strip().lower()
        ps = self._get_or_auto_monitor(sender_email, num)
        if not ps:
            return

        email = f"{username}@cisco.com"
        if email in ps.notify_emails:
            self._webex.send_room_message(
                f"`{username}` is already subscribed to PR #{num} status updates."
            )
            return

        display_name = self._webex.lookup_person(email)
        if not display_name:
            self._webex.send_room_message(f"Could not find Webex user `{email}`. Check the username.")
            return

        ps.notify_emails[email] = display_name
        self._webex.send_dm(
            email,
            markdown=(
                f"\U0001F514 You have been subscribed to status updates for "
                f"**[PR #{num}: {ps.info.title}]({ps.info.html_url})**.\n\n"
                f"You will receive read-only notifications when check statuses change."
            ),
        )
        self._webex.send_room_message(
            markdown=(
                f"\U0001F514 **{display_name}** (`{username}`) will now receive "
                f"status updates for PR #{num} via DM."
            )
        )

    def _cmd_unnotify(self, sender_email: str, args: str) -> None:
        parts = args.strip().split(maxsplit=1)
        if len(parts) < 2:
            self._webex.send_room_message(
                "Usage: `unnotify <PR_ID> <username>`  Example: `unnotify 3473 johndoe` or full URL"
            )
            return
        num = self._resolve_pr_number(parts[0])
        if num is None:
            self._webex.send_room_message(f"Could not parse PR: `{parts[0]}`. Use a number or full URL.")
            return
        username = parts[1].strip().lower()
        email = f"{username}@cisco.com"

        key = self._pr_key(sender_email, num)
        ps = self._prs.get(key)
        if not ps or email not in ps.notify_emails:
            self._webex.send_room_message(f"`{username}` is not subscribed to PR #{num}.")
            return

        name = ps.notify_emails.pop(email)
        self._webex.send_dm(
            email,
            markdown=f"\U0001F515 You have been unsubscribed from status updates for **PR #{num}**.",
        )
        self._webex.send_room_message(
            f"\U0001F515 **{name}** (`{username}`) removed from PR #{num} notifications."
        )

    def _cmd_queue(self, sender_email: str, _args: str) -> None:
        user_prs = self._user_prs(sender_email)
        lines = []
        for num, ps in sorted(user_prs.items()):
            if ps.precommit_monitor:
                pm = ps.precommit_monitor
                if pm.phase == "wait_restore":
                    lines.append(
                        f"- PR #{num}: precommit **{pm.user_tag}** — "
                        f"**Default** retry, restore in "
                        f"{max(0, int(pm.restore_at - time.time()))}s"
                    )
                else:
                    lines.append(
                        f"- PR #{num}: monitoring precommit **`TEST_TAG`: {pm.user_tag}** "
                        f"(attempt {pm.attempt}/2)"
                    )
            for key, rt in ps.retries.items():
                if rt.attempts < MAX_RETRIES:
                    lines.append(
                        f"- PR #{num}: `{rt.command}` "
                        f"(attempt {rt.attempts}/{MAX_RETRIES}, will retry on failure)"
                    )
            if ps.auto_merge:
                lines.append(f"- PR #{num}: **GitHub auto-merge** (on)")
            if ps.notify_emails:
                names = ", ".join(ps.notify_emails.values())
                lines.append(f"- PR #{num}: **notify** \u2192 {names}")

        if not lines:
            self._webex.send_room_message("No pending items in your queue.")
            return
        self._webex.send_room_message(markdown="**Your pending queue:**\n\n" + "\n".join(lines))

    def _cmd_interval(self, sender_email: str, args: str) -> None:
        try:
            seconds = int(args.strip())
            if seconds < 30:
                self._webex.send_room_message("Minimum interval is 30 seconds.")
                return
            self._poll_interval = seconds
            self._scheduler.reschedule_job(
                "check_pr", trigger="interval", seconds=self._poll_interval
            )
            self._webex.send_room_message(
                f"\u23F1\uFE0F Polling interval: **{seconds}s** (**{seconds // 60} min**)."
            )
        except (ValueError, TypeError):
            self._webex.send_room_message("Usage: `interval <seconds>`")

    # ── internal logic ────────────────────────────────────────

    def _add_pr(self, email: str, number: int, quiet: bool = False) -> None:
        gh = self._get_gh(email)
        if not gh:
            self._webex.send_room_message(
                f"\U0001F512 Not registered. DM me `register <TOKEN>` first."
            )
            return
        if not self._default_repo:
            self._webex.send_room_message("DEFAULT_REPO not configured.")
            return
        owner, repo = self._default_repo.split("/", 1)
        try:
            pr = gh.get_pr(owner, repo, number)
        except Exception:
            logger.exception("Failed to fetch PR #%d for %s", number, email)
            self._webex.send_room_message(f"Failed to fetch PR #{number}.")
            return

        key = self._pr_key(email, number)
        self._prs[key] = PRState(
            info=pr,
            owner_email=email,
            auto_merge=pr.auto_merge_enabled,
        )
        rec = self._user_store.get(email)
        gh_user = rec.github_user if rec else email
        logger.info(
            "%s now monitoring PR %s/%s#%d (auto_merge=%s)",
            gh_user, owner, repo, number, pr.auto_merge_enabled,
        )

        if quiet:
            self._webex.send_room_message(
                markdown=f"\U0001F50D Auto-monitoring **[PR #{number}: {pr.title}]({pr.html_url})** for **{gh_user}**"
            )
            return

        user_prs = self._user_prs(email)
        mins = self._poll_interval // 60
        monitored = ", ".join(f"#{n}" for n in sorted(user_prs.keys()))
        self._webex.send_room_message(
            markdown=(
                f"\U0001F50D **{gh_user}** now monitoring "
                f"**[PR #{number}: {pr.title}]({pr.html_url})**\n"
                f"Polling every **{mins} min**.\n\n"
                f"**Your monitored PRs:** {monitored}\n\n"
                f"Available actions:\n"
                f"- `status {number}` — check status now\n"
                f"- `precommit {number} <TEST_TAG>` — set **TEST_TAG** in PR description (e.g. ROUTING, L2)\n"
                f"- `comment {number} <text>` — post a comment\n"
                f"- `automerge {number} on` — enable **GitHub** auto-merge (same as the web UI)\n"
                f"- `stop {number}` — stop monitoring"
            )
        )
        self._fetch_and_report(email, number, force=True)

    def _fetch_and_report(self, email: str, pr_number: int, force: bool = False) -> None:
        key = self._pr_key(email, pr_number)
        ps = self._prs.get(key)
        if not ps:
            return
        gh = self._get_gh(email)
        if not gh:
            return
        pr = ps.info
        try:
            fresh_pr = gh.get_pr(pr.owner, pr.repo, pr.number)
            ps.info = fresh_pr
            ps.auto_merge = fresh_pr.auto_merge_enabled
            checks = gh.get_all_checks(pr.owner, pr.repo, fresh_pr.head_sha)
        except Exception:
            logger.exception("Error fetching checks for PR #%d (%s)", pr.number, email)
            return

        if fresh_pr.state == "closed" or fresh_pr.merged:
            summary = format_checks_summary(checks, fresh_pr)
            if fresh_pr.merged:
                msg = f"\U0001F389 **PR #{pr_number} has been merged!**\n\n"
            else:
                msg = f"\u274C **PR #{pr_number} was closed without merging.**\n\n"
            msg += summary + "\n\n\u23F9\uFE0F Auto-stopped monitoring."
            self._webex.send_dm(email, markdown=msg)
            self._notify_subscribers(ps, msg)
            self._prs.pop(key, None)
            logger.info("PR #%d closed/merged — stopped monitoring for %s", pr_number, email)
            return

        fresh_pr.checks = checks
        current_states = {c.name: c.state for c in checks}
        current_states_lower = {c.name.lower(): c.state for c in checks}
        prev = ps.previous_checks

        changed = []
        for c in checks:
            prev_state = prev.get(c.name)
            if prev_state is not None and prev_state != c.state:
                changed.append((c, prev_state))

        new_checks = [c for c in checks if c.name not in prev]
        ps.previous_checks = current_states

        if force or changed or new_checks:
            summary = format_checks_summary(checks, fresh_pr)
            if changed and not force:
                change_lines = []
                for c, prev_state in changed:
                    change_lines.append(
                        f"  \u2022 **{c.name}**: `{prev_state}` \u2192 `{c.state}`"
                    )
                header = (
                    "\U0001F514 **Check status changed!**\n"
                    + "\n".join(change_lines)
                    + "\n\n"
                )
                summary = header + summary
            self._webex.send_dm(email, markdown=summary)
            self._notify_subscribers(ps, summary)

        self._process_precommit_monitor(ps, current_states_lower)
        self._process_ut_watchdog(ps, current_states_lower)
        self._process_retries(ps, current_states_lower)

    def _notify_subscribers(self, ps: PRState, summary: str) -> None:
        if not ps.notify_emails:
            return
        for sub_email in ps.notify_emails:
            self._webex.send_dm(sub_email, markdown=summary)

    @staticmethod
    def _github_comment_ts(iso: str) -> float:
        if not iso:
            return 0.0
        s = iso.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(s).timestamp()
        except ValueError:
            return 0.0

    @staticmethod
    def _is_build_aborted_comment(body: str) -> bool:
        b = (body or "").lower()
        return "build did not succeed" in b and "aborted" in b

    def _ut_related_checks(
        self, check_states: Dict[str, str], key: str = "ut"
    ) -> List[Tuple[str, str]]:
        return [(n, s) for n, s in check_states.items() if key in n]

    def _ut_stalled_no_progress(self, related: List[Tuple[str, str]]) -> bool:
        """True if no UT-related checks, or every UT check is still only queued/pending."""
        if not related:
            return True
        for _, state in related:
            st = str(state).lower()
            if st in ("in_progress", "success", "failure", "error", "cancelled"):
                return False
        return all(str(s).lower() in ("pending", "queued") for _, s in related)

    def _process_ut_watchdog(
        self, ps: PRState, check_states_lower: Dict[str, str]
    ) -> None:
        """Re-post UT trigger if Jenkins never picked up the build (30+ min) or PR has ABORTED bot comment."""
        if "ut" not in ps.retries:
            return
        rt = ps.retries["ut"]
        if rt.watchdog_reposts >= MAX_UT_WATCHDOG_REPOSTS:
            ps.retries.pop("ut", None)
            return

        gh = self._get_gh(ps.owner_email)
        if not gh:
            return
        pr = ps.info
        now = time.time()
        trigger_ts = rt.triggered_at
        related = self._ut_related_checks(check_states_lower, "ut")

        abort_hit = False
        try:
            comments = gh.list_issue_comments(pr.owner, pr.repo, pr.number, per_page=80)
            abort_hit = any(
                self._is_build_aborted_comment(c.get("body", ""))
                and self._github_comment_ts(c.get("created_at", "")) > trigger_ts
                for c in comments
            )
        except Exception:
            logger.exception("UT watchdog: failed to list comments for PR #%d", pr.number)

        stall_hit = (
            (now - trigger_ts) >= UT_STALL_SECONDS
            and self._ut_stalled_no_progress(related)
        )

        if not abort_hit and not stall_hit:
            return

        reason = (
            "PR comment indicates **ABORTED** build after your UT trigger"
            if abort_hit
            else f"no UT build progress after **{UT_STALL_SECONDS // 60} minutes**"
        )
        try:
            url = gh.post_comment(pr.owner, pr.repo, pr.number, rt.command)
            rt.watchdog_reposts += 1
            rt.triggered_at = time.time()
            self._webex.send_dm(
                ps.owner_email,
                markdown=(
                    f"\U0001F504 **UT re-triggered** (watchdog {rt.watchdog_reposts}/"
                    f"{MAX_UT_WATCHDOG_REPOSTS}): `{rt.command}` on PR #{pr.number}\n"
                    f"_Reason: {reason}_\n[view]({url})"
                ),
            )
            if rt.watchdog_reposts >= MAX_UT_WATCHDOG_REPOSTS:
                ps.retries.pop("ut", None)
                self._webex.send_dm(
                    ps.owner_email,
                    markdown=(
                        f"\u26A0\uFE0F Watchdog will not re-post again for this UT run "
                        f"({MAX_UT_WATCHDOG_REPOSTS} re-triggers used). "
                        "Post a new UT trigger comment on the PR if you still need another kick."
                    ),
                )
        except Exception:
            logger.exception("UT watchdog: failed to re-post UT on PR #%d", pr.number)

    def _process_retries(
        self, ps: PRState, check_states: Dict[str, str]
    ) -> None:
        gh = self._get_gh(ps.owner_email)
        if not gh:
            return
        pr = ps.info
        to_remove = []

        for key, rt in list(ps.retries.items()):
            related_checks = [
                (name, state) for name, state in check_states.items()
                if key in name
            ]
            if not related_checks:
                continue

            any_failed = any(state in FAILED_STATES for _, state in related_checks)
            all_done = all(state not in PENDING_STATES for _, state in related_checks)

            if all_done and any_failed:
                if rt.attempts < MAX_RETRIES:
                    rt.attempts += 1
                    try:
                        url = gh.post_comment(pr.owner, pr.repo, pr.number, rt.command)
                        if key == "ut":
                            rt.triggered_at = time.time()
                            rt.watchdog_reposts = 0
                        self._webex.send_dm(
                            ps.owner_email,
                            markdown=(
                                f"\U0001F504 **Auto-retry** ({rt.attempts}/{MAX_RETRIES}): "
                                f"`{rt.command}` on PR #{pr.number}: [view]({url})"
                            ),
                        )
                    except Exception:
                        logger.exception("Retry failed for PR #%d", pr.number)
                        to_remove.append(key)
                else:
                    self._webex.send_dm(
                        ps.owner_email,
                        markdown=(
                            f"\u274C **{key.upper()} failed** after {MAX_RETRIES} attempts "
                            f"on PR #{pr.number}. `{rt.command}` — manual action needed."
                        ),
                    )
                    to_remove.append(key)
            elif all_done and not any_failed:
                to_remove.append(key)

        for key in to_remove:
            ps.retries.pop(key, None)

    def _poll_all_prs(self) -> None:
        for (email, num) in list(self._prs.keys()):
            self._fetch_and_report(email, num, force=False)

    def _poll_webex_messages(self) -> None:
        self._webex.poll_and_dispatch()

    def _periodic_report(self) -> None:
        if not self._prs:
            return

        users_with_prs: Dict[str, List[int]] = {}
        for (email, num) in self._prs:
            users_with_prs.setdefault(email, []).append(num)

        for email, nums in users_with_prs.items():
            for num in sorted(nums):
                self._fetch_and_report(email, num, force=False)

            queue_lines = []
            for num in sorted(nums):
                key = self._pr_key(email, num)
                ps = self._prs.get(key)
                if not ps:
                    continue
                if ps.precommit_monitor:
                    pm = ps.precommit_monitor
                    if pm.phase == "wait_restore":
                        queue_lines.append(
                            f"  \u2022 PR #{num}: precommit — **Default** wait "
                            f"({max(0, int(pm.restore_at - time.time()))}s until restore "
                            f"`{pm.user_tag}`)"
                        )
                    else:
                        queue_lines.append(
                            f"  \u2022 PR #{num}: precommit monitor "
                            f"`{pm.user_tag}` (attempt {pm.attempt}/2)"
                        )
                for k, rt in ps.retries.items():
                    if rt.attempts < MAX_RETRIES:
                        queue_lines.append(
                            f"  \u2022 PR #{num}: `{rt.command}` — attempt {rt.attempts}/{MAX_RETRIES}"
                        )
                if ps.auto_merge:
                    queue_lines.append(
                        f"  \u2022 PR #{num}: **GitHub auto-merge** enabled"
                    )

            if queue_lines:
                self._webex.send_dm(
                    email,
                    markdown="\U0001F4CB **Your queue:**\n" + "\n".join(queue_lines),
                )

    # ── lifecycle ─────────────────────────────────────────────

    def run(self) -> None:
        logger.info(
            "PR Monitor Bot starting (poll interval: %ds / %d min)",
            self._poll_interval,
            self._poll_interval // 60,
        )
        self._running = True

        self._webex.skip_existing_messages()

        self._scheduler.add_job(
            self._poll_all_prs,
            "interval",
            seconds=self._poll_interval,
            id="check_pr",
            max_instances=1,
        )
        self._scheduler.add_job(
            self._poll_webex_messages,
            "interval",
            seconds=5,
            id="poll_webex",
            max_instances=1,
        )
        self._scheduler.add_job(
            self._periodic_report,
            "interval",
            seconds=10800,
            id="periodic_report",
            max_instances=1,
        )
        self._scheduler.add_job(
            self._poll_precommit_monitors,
            "interval",
            seconds=PRECOMMIT_MONITOR_POLL_SEC,
            id="precommit_monitor",
            max_instances=1,
        )
        self._scheduler.start()

        # Welcome only people who are not yet in the user store (no blast to registered users).
        _welcome_md = (
            "\U0001F916 **Welcome to PR Monitor Bot**\n\n"
            "Thanks for using this bot. It helps you track pull requests, run checks, "
            "and manage GitHub workflows from Webex \u2014 with **private replies** so your "
            "updates stay between you and the bot.\n\n"
            "---\n\n"
            "**Getting started**\n\n"
            "\U0001F44B Send me a **direct message** (DM) with:\n\n"
            "`register <YOUR_GITHUB_TOKEN>`\n\n"
            "\U0001F512 **Your token is safe** \u2014 it is encrypted with AES-256 "
            "and stored securely. It is only used for GitHub API calls on your behalf "
            "and is never visible to anyone.\n\n"
            "Need a token? Go to "
            "[Tokens (classic)](https://github.example.com/settings/tokens)\n\n"
            "---\n\n"
            "**Suggestions or feedback**\n\n"
            "Please **unicast** (send a direct message on Webex) to "
            "**Venkata Gouri Rajesh Etla** (`vrajeshe`, **vrajeshe@cisco.com**). "
            "Type `help` in a DM or in this space after you register for command details."
        )
        registered = {e.lower() for e in self._user_store.all_emails()}
        try:
            room_emails = self._webex.list_room_member_emails()
        except Exception:
            logger.exception("Startup: could not list room memberships; skipping welcome DMs")
            room_emails = []

        welcome_sent = 0
        for email in room_emails:
            if not email or email == self._webex.bot_email:
                continue
            if email in registered:
                continue
            if not self._webex.is_email_allowed(email):
                continue
            if self._webex.send_dm(email, markdown=_welcome_md):
                welcome_sent += 1
        if welcome_sent:
            logger.info("Startup welcome DM sent to %d unregistered room member(s)", welcome_sent)

        def _shutdown(signum, frame):
            logger.info("Shutting down...")
            self._running = False

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            self._scheduler.shutdown(wait=False)
            logger.info("Bot stopped.")


LOCK_FILE = Path(__file__).parent / ".bot.pid"


def _acquire_lock() -> None:
    if LOCK_FILE.exists():
        old_pid = LOCK_FILE.read_text().strip()
        try:
            os.kill(int(old_pid), 0)
            logger.error(
                "Another bot instance is already running (PID %s). "
                "Kill it first or delete %s",
                old_pid, LOCK_FILE,
            )
            sys.exit(1)
        except (OSError, ValueError):
            pass
    LOCK_FILE.write_text(str(os.getpid()))
    atexit.register(_release_lock)


def _release_lock() -> None:
    try:
        if LOCK_FILE.exists() and LOCK_FILE.read_text().strip() == str(os.getpid()):
            LOCK_FILE.unlink()
    except OSError:
        pass


def main() -> None:
    _acquire_lock()

    required_vars = ["WEBEX_BOT_TOKEN", "WEBEX_ROOM_ID"]
    missing = [v for v in required_vars if not os.environ.get(v)]
    if missing:
        logger.error(
            "Missing required environment variables: %s\n"
            "Copy .env.example to .env and fill in the values.",
            ", ".join(missing),
        )
        sys.exit(1)

    bot = PRMonitorBot()
    bot.run()


if __name__ == "__main__":
    main()
