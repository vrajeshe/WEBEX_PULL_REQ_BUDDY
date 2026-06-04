"""One-shot: append a commit on `bot/gitmodules-cisco-202505c-20260526-225136`
that bumps the 14 submodule gitlinks to the tip of their `cisco/202505c` branch.
Then update PR #3765's body with the new hash table.
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv(".env.jira")
sys.path.insert(0, ".")
from github_client import GitHubEnterpriseClient
from user_store import UserStore

PARENT_OWNER = "whitebox"
PARENT_REPO = "sonic-buildimage"
TARGET_BRANCH = "cisco/202505c"
PR_BRANCH = "bot/gitmodules-cisco-202505c-20260526-225136"
PR_NUMBER = 3765
SUB_BRANCH = "cisco/202505c"
JIRA = "MIGSOFTWAR-42441"

PATHS = [
    "src/sonic-swss-common",
    "src/sonic-linux-kernel",
    "src/sonic-sairedis",
    "src/sonic-dbsyncd",
    "src/sonic-snmpagent",
    "src/sonic-utilities",
    "src/sonic-platform-common",
    "src/sonic-mgmt-framework",
    "src/sonic-ztp",
    "src/sonic-mgmt-common",
    "src/dhcprelay",
    "src/sonic-host-services",
    "src/sonic-gnmi",
    "src/sonic-stp",
]


def _resolve_token() -> str:
    store = UserStore("users.db", os.environ["ENCRYPTION_KEY"])
    rec = store.get("vrajeshe@cisco.com")
    if not rec or not rec.github_token:
        raise SystemExit("No PAT registered for vrajeshe@cisco.com")
    return rec.github_token


def main() -> None:
    base = os.environ.get("GITHUB_BASE_URL", "https://wwwin-github.cisco.com/api/v3")
    gh = GitHubEnterpriseClient(token=_resolve_token(), base_url=base)

    gm_text = gh.get_file_text(PARENT_OWNER, PARENT_REPO, ".gitmodules", PR_BRANCH)
    mods = gh.parse_gitmodules_submodules(gm_text)
    by_path = {m["path"]: m for m in mods}

    rows = []
    for p in PATHS:
        m = by_path.get(p)
        if not m:
            raise SystemExit(f"path {p} missing from .gitmodules on {PR_BRANCH}")
        if (m.get("branch") or "").strip() != SUB_BRANCH:
            raise SystemExit(
                f"path {p}: .gitmodules branch is "
                f"{m.get('branch')!r}, expected {SUB_BRANCH!r}"
            )
        sub_owner, sub_repo = gh.parse_github_owner_repo_from_url(m["url"])
        tip = gh.get_branch_tip_sha(sub_owner, sub_repo, SUB_BRANCH)
        current = gh.get_submodule_gitlink_sha(PARENT_OWNER, PARENT_REPO, p, PR_BRANCH)
        rows.append((p, sub_owner, sub_repo, current, tip))

    print(f"{'path':<35} {'sub repo':<40} {'current':<10} -> {'tip':<10}  changed")
    print("-" * 110)
    changed_rows = []
    for p, so, sr, cur, tip in rows:
        chg = "yes" if cur != tip else "no"
        if cur != tip:
            changed_rows.append((p, so, sr, cur, tip))
        print(f"{p:<35} {so + '/' + sr:<40} {cur[:10]:<10} -> {tip[:10]:<10}  {chg}")

    if not changed_rows:
        print("\nNothing to bump — all 14 already at tip on that branch.")
        return

    head_sha = gh.get_branch_tip_sha(PARENT_OWNER, PARENT_REPO, PR_BRANCH)
    head_commit = gh._get(f"/repos/{PARENT_OWNER}/{PARENT_REPO}/git/commits/{head_sha}")
    base_tree = head_commit["tree"]["sha"]
    print(f"\nbranch tip {head_sha[:12]} tree {base_tree[:12]}")

    tree_entries = [
        {"path": p, "mode": "160000", "type": "commit", "sha": tip}
        for (p, _so, _sr, _cur, tip) in changed_rows
    ]
    new_tree = gh._post(
        f"/repos/{PARENT_OWNER}/{PARENT_REPO}/git/trees",
        json_body={"base_tree": base_tree, "tree": tree_entries},
    )
    new_tree_sha = new_tree["sha"]
    print(f"new tree: {new_tree_sha[:12]}")

    bump_lines = [f"  - {p}: {cur[:10]} -> {tip[:10]}" for (p, _, _, cur, tip) in changed_rows]
    commit_msg = (
        f"[{JIRA}] Bump {len(changed_rows)} submodule gitlinks to tip of {SUB_BRANCH}\n\n"
        + "\n".join(bump_lines)
    )
    new_commit = gh._post(
        f"/repos/{PARENT_OWNER}/{PARENT_REPO}/git/commits",
        json_body={
            "message": commit_msg,
            "tree": new_tree_sha,
            "parents": [head_sha],
        },
    )
    new_commit_sha = new_commit["sha"]
    print(f"new commit: {new_commit_sha[:12]}")

    gh._patch(
        f"/repos/{PARENT_OWNER}/{PARENT_REPO}/git/refs/heads/{PR_BRANCH}",
        json_body={"sha": new_commit_sha, "force": False},
    )
    print(f"branch {PR_BRANCH} -> {new_commit_sha[:12]}")

    pr = gh._get(f"/repos/{PARENT_OWNER}/{PARENT_REPO}/pulls/{PR_NUMBER}")
    body = pr.get("body") or ""

    table_lines = [
        "",
        "**Submodule hash bumps (tip of `cisco/202505c`):**",
        "",
        "| Submodule | Repo | from | to |",
        "|---|---|---|---|",
    ]
    for p, so, sr, cur, tip in changed_rows:
        table_lines.append(
            f"| `{p}` | `{so}/{sr}` | `{cur[:10]}` | `{tip[:10]}` |"
        )

    sentinel = "<!-- bot:hash-bumps -->"
    if sentinel not in body:
        addition = "\n".join(table_lines) + f"\n\n{sentinel}\n"
        if "**Submodules updated:**" in body:
            body = body.replace(
                "**Submodules updated:**",
                addition + "\n**Submodules updated:**",
                1,
            )
        else:
            body = addition + "\n" + body

    gh._patch(
        f"/repos/{PARENT_OWNER}/{PARENT_REPO}/pulls/{PR_NUMBER}",
        json_body={"body": body},
    )
    print(f"updated PR #{PR_NUMBER} body")


if __name__ == "__main__":
    main()
