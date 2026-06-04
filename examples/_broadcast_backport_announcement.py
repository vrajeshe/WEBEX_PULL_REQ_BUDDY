"""One-shot: DM every registered user about the new `backport` command."""

import os
import sys
import time

from dotenv import load_dotenv

load_dotenv(".env.jira")
sys.path.insert(0, ".")
from user_store import UserStore
from webex_client import WebexBotClient

MARKDOWN = (
    "\U0001F4E2 **Bot update** \u2014 new command added (replacing `double_commit`):\n\n"
    "**`backport <pull-request-url> <target-branch>`**\n\n"
    "Squash-cherry-picks the PR's diff onto `<target-branch>` (3-way merge via the "
    "GitHub Merges API, conflict-aware) and opens a brand-new backport PR.\n\n"
    "**Example:**\n"
    "```\n"
    "backport https://wwwin-github.cisco.com/whitebox/sonic-dhcp-relay/pull/40 c-master\n"
    "```\n\n"
    "For `sonic-buildimage` PRs the new PR body uses the same template as "
    "`raise_hash_update` (PR template + CICD trigger block).\n\n"
    "Type `help` to see the full command list."
)


def main() -> None:
    store = UserStore("users.db", os.environ["ENCRYPTION_KEY"])
    emails = sorted(store.list_emails()) if hasattr(store, "list_emails") else sorted(store._users.keys())
    print(f"Registered users: {len(emails)}")

    wx = WebexBotClient(
        bot_token=os.environ["WEBEX_BOT_TOKEN"],
        room_id=os.environ["WEBEX_ROOM_ID"],
    )

    sent = 0
    failed = []
    for email in emails:
        ok = wx.send_dm(email, markdown=MARKDOWN)
        if ok:
            sent += 1
            print(f"  sent: {email}")
        else:
            failed.append(email)
            print(f"  FAILED: {email}")
        time.sleep(0.3)

    print(f"\nDone. sent={sent} failed={len(failed)}")
    if failed:
        for e in failed:
            print(f"  failed: {e}")


if __name__ == "__main__":
    main()
