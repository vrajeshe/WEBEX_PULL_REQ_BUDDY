"""One-shot audit: which submodules have `cisco/202505c` but are pinned to another branch in .gitmodules?"""

import os
import sys
from urllib.parse import quote

import requests
from dotenv import load_dotenv

load_dotenv(".env.jira")
sys.path.insert(0, ".")
from github_client import GitHubEnterpriseClient
from user_store import UserStore

PARENT_OWNER = "whitebox"
PARENT_REPO = "sonic-buildimage"
PARENT_BRANCH = "cisco/202505c"
TARGET_SUB_BRANCH = "cisco/202505c"

base = os.environ.get("GITHUB_BASE_URL", "https://wwwin-github.cisco.com/api/v3")
token = os.environ.get("GITHUB_TOKEN", "")

store = UserStore("users.db", os.environ["ENCRYPTION_KEY"])
for email in ("vrajeshe@cisco.com",):
    rec = store.get(email)
    if rec and rec.github_token:
        token = rec.github_token
        print(f"using stored PAT for {email}")
        break

gh = GitHubEnterpriseClient(token=token, base_url=base)

gm = gh.get_file_text(PARENT_OWNER, PARENT_REPO, ".gitmodules", PARENT_BRANCH)
mods = gh.parse_gitmodules_submodules(gm)
print(f"submodules in .gitmodules: {len(mods)}")
print(f"target sub-branch to look for: {TARGET_SUB_BRANCH}")
print("=" * 100)

mismatches = []
matches = []
no_target = []
errors = []

for m in mods:
    path = (m.get("path") or "").strip()
    url = (m.get("url") or "").strip()
    declared = (m.get("branch") or "").strip()
    parsed = gh.parse_github_owner_repo_from_url(url)
    if not parsed:
        errors.append((path, "unparseable url: " + url))
        continue
    sub_owner, sub_repo = parsed
    enc = quote(TARGET_SUB_BRANCH, safe="")
    try:
        r = gh._session.get(
            f"{base}/repos/{sub_owner}/{sub_repo}/branches/{enc}", timeout=20
        )
    except requests.RequestException as e:
        errors.append((path, str(e)))
        continue
    if r.status_code == 200:
        if declared == TARGET_SUB_BRANCH:
            matches.append((path, sub_owner, sub_repo))
        else:
            mismatches.append((path, sub_owner, sub_repo, declared))
    elif r.status_code == 404:
        no_target.append((path, sub_owner, sub_repo, declared))
    else:
        errors.append((path, f"HTTP {r.status_code}"))

NONE = "(none)"

print(
    f"\n--- MISMATCH: sub repo HAS '{TARGET_SUB_BRANCH}' but .gitmodules declares another branch "
    f"({len(mismatches)}) ---"
)
for p, o, r, d in mismatches:
    decl = d if d else NONE
    print(f"  {p:<55} repo={o}/{r:<30} .gitmodules branch = {decl}")

print(f"\n--- ALREADY ON {TARGET_SUB_BRANCH} in .gitmodules ({len(matches)}) ---")
for p, o, r in matches:
    print(f"  {p:<55} repo={o}/{r}")

print(f"\n--- sub repo does NOT have {TARGET_SUB_BRANCH} ({len(no_target)}) ---")
for p, o, r, d in no_target:
    decl = d if d else NONE
    print(f"  {p:<55} repo={o}/{r:<30} .gitmodules branch = {decl}")

if errors:
    print(f"\n--- ERRORS ({len(errors)}) ---")
    for p, e in errors:
        print(f"  {p}: {e}")

cm_no_target = [(p, o, r) for (p, o, r, d) in no_target if d == "c-master"]
cm_has_target = [(p, o, r) for (p, o, r, d) in mismatches if d == "c-master"]

print("\n" + "=" * 100)
print(f"submodules pinned to 'c-master' WITHOUT cisco/202505c upstream ({len(cm_no_target)}):")
for p, o, r in cm_no_target:
    print(f"  {p:<55} repo={o}/{r}")

print(f"\nsubmodules pinned to 'c-master' that DO have cisco/202505c upstream ({len(cm_has_target)}):")
for p, o, r in cm_has_target:
    print(f"  {p:<55} repo={o}/{r}")
