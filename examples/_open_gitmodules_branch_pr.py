"""One-shot: open a PR on whitebox/sonic-buildimage that flips 14 submodules'
.gitmodules branch tracking from c-master -> cisco/202505c (no gitlink/hash change).
"""

import base64
import os
import re
import sys
import time

from dotenv import load_dotenv

load_dotenv(".env.jira")
sys.path.insert(0, ".")
from github_client import GitHubEnterpriseClient
from user_store import UserStore

PARENT_OWNER = "whitebox"
PARENT_REPO = "sonic-buildimage"
TARGET_BRANCH = "cisco/202505c"
NEW_SUB_BRANCH = "cisco/202505c"
OLD_SUB_BRANCH = "c-master"
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
PATHS_SET = set(PATHS)


def _resolve_token() -> str:
    store = UserStore("users.db", os.environ["ENCRYPTION_KEY"])
    rec = store.get("vrajeshe@cisco.com")
    if not rec or not rec.github_token:
        raise SystemExit("No PAT registered for vrajeshe@cisco.com")
    return rec.github_token


def rewrite_gitmodules(content: str) -> tuple[str, list[str], list[str]]:
    """Return (new_content, edited_paths, missed_paths_already_set)."""
    lines = content.splitlines()
    section_idxs = [
        i for i, l in enumerate(lines) if re.match(r"^\s*\[submodule\s", l)
    ]
    section_idxs.append(len(lines))
    new_lines = list(lines)
    edited: list[str] = []
    already: list[str] = []

    for s, e in zip(section_idxs, section_idxs[1:]):
        block = lines[s:e]
        path = None
        branch_idx = None
        branch_val = None
        for j, l in enumerate(block):
            mp = re.match(r"^\s*path\s*=\s*(.+?)\s*$", l)
            if mp:
                path = mp.group(1).strip()
            mb = re.match(r"^\s*branch\s*=\s*(.+?)\s*$", l)
            if mb:
                branch_idx = j
                branch_val = mb.group(1).strip()
        if path in PATHS_SET:
            if branch_val == NEW_SUB_BRANCH:
                already.append(path)
                continue
            if branch_idx is None or branch_val != OLD_SUB_BRANCH:
                raise SystemExit(
                    f"Unexpected state for submodule '{path}': branch={branch_val!r}; "
                    f"refusing to touch."
                )
            indent = re.match(r"^\s*", block[branch_idx]).group(0)
            new_lines[s + branch_idx] = f"{indent}branch = {NEW_SUB_BRANCH}"
            edited.append(path)

    out = "\n".join(new_lines)
    if content.endswith("\n"):
        out += "\n"
    return out, edited, already


def main() -> None:
    base = os.environ.get("GITHUB_BASE_URL", "https://wwwin-github.cisco.com/api/v3")
    gh = GitHubEnterpriseClient(token=_resolve_token(), base_url=base)

    contents_url = f"/repos/{PARENT_OWNER}/{PARENT_REPO}/contents/.gitmodules"
    info = gh._get(contents_url, params={"ref": TARGET_BRANCH})
    file_sha = info["sha"]
    gm_old = base64.b64decode(info["content"].replace("\n", "")).decode("utf-8")

    gm_new, edited, already = rewrite_gitmodules(gm_old)
    print(f"edited={len(edited)} already={len(already)}")
    for p in edited:
        print(f"  edit: {p}")
    for p in already:
        print(f"  already: {p}")

    expected = sorted(PATHS_SET)
    actually_changed = sorted(set(edited) | set(already))
    if expected != actually_changed:
        missing = sorted(set(expected) - set(actually_changed))
        raise SystemExit(f"Did not match all expected paths. Missing: {missing}")

    if not edited:
        raise SystemExit("No edits to make.")

    ts = time.strftime("%Y%m%d-%H%M%S")
    new_branch = f"bot/gitmodules-cisco-202505c-{ts}"
    parent_tip = gh.get_branch_tip_sha(PARENT_OWNER, PARENT_REPO, TARGET_BRANCH)
    print(f"creating branch {new_branch} from {parent_tip[:12]}")
    gh._post(
        f"/repos/{PARENT_OWNER}/{PARENT_REPO}/git/refs",
        json_body={"ref": f"refs/heads/{new_branch}", "sha": parent_tip},
    )

    new_b64 = base64.b64encode(gm_new.encode("utf-8")).decode("ascii")
    commit_msg = (
        f"[{JIRA}] .gitmodules: track cisco/202505c instead of c-master "
        f"for {len(edited)} submodules\n\n"
        + "\n".join(f"  - {p}" for p in edited)
    )
    print("committing .gitmodules update on new branch")
    commit_resp = gh._put(
        contents_url,
        json_body={
            "message": commit_msg,
            "content": new_b64,
            "sha": file_sha,
            "branch": new_branch,
        },
    )
    commit_sha = (commit_resp.get("commit") or {}).get("sha", "")
    print(f"commit sha: {commit_sha[:12]}")

    bullets = "\n".join(f"- `{p}`" for p in edited)
    bump_block = (
        f"**JIRA**: {JIRA}\n\n"
        f"This PR updates `.gitmodules` to track **`{NEW_SUB_BRANCH}`** instead of "
        f"`{OLD_SUB_BRANCH}` for {len(edited)} submodules whose upstream repos already "
        f"have a `{NEW_SUB_BRANCH}` branch.\n\n"
        f"**Scope:** only the `branch = ...` field is edited. Submodule gitlinks "
        f"(pinned commit SHAs) are unchanged.\n\n"
        f"**Submodules updated:**\n{bullets}\n"
    )
    body = gh._compose_raise_hash_pr_description(
        PARENT_OWNER, PARENT_REPO, TARGET_BRANCH, bump_block
    )
    title = (
        f"[{JIRA}] .gitmodules: track cisco/202505c instead of c-master "
        f"for {len(edited)} submodules"
    )
    pr = gh._post(
        f"/repos/{PARENT_OWNER}/{PARENT_REPO}/pulls",
        json_body={
            "title": title,
            "head": new_branch,
            "base": TARGET_BRANCH,
            "body": body,
        },
    )
    print(f"PR opened: #{pr.get('number')} -> {pr.get('html_url')}")


if __name__ == "__main__":
    main()
