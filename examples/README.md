# `examples/` — real one-shot operational scripts

These are **kept as-is** (not scrubbed) from the production environment in
which the bot was originally developed. They are intentionally **not** part
of the bot's runtime and ship here only as worked examples of how to drive
the `GitHubEnterpriseClient` and `WebexBotClient` from a Python script.

Each script reads `.env.jira` (or `.env`) for credentials and uses
`UserStore` to look up an admin's encrypted GitHub PAT.

| Script | What it does |
|---|---|
| [`_audit_gitmodules_branch.py`](_audit_gitmodules_branch.py) | For a parent repo + branch, walks `.gitmodules` and reports which submodules are pinned to a different branch than a target (e.g. `cisco/202505c`). |
| [`_open_gitmodules_branch_pr.py`](_open_gitmodules_branch_pr.py) | Opens a PR that flips selected submodules' `branch =` field in `.gitmodules` from `c-master` to `cisco/202505c`. |
| [`_bump_gitlinks_on_existing_branch.py`](_bump_gitlinks_on_existing_branch.py) | Appends a commit on an existing PR branch that bumps gitlinks (commit SHAs) of selected submodules to the tip of their branch. |
| [`_broadcast_backport_announcement.py`](_broadcast_backport_announcement.py) | Sends a one-time DM to every registered user announcing a feature change. |

> ⚠️ **They contain real org names, branch names, and Jira IDs from the
> original deployment.** Adapt before running anywhere else, or treat them
> only as documentation.

## Running one

```bash
cd <repo root>
source venv/bin/activate
# Make sure .env is set with GITHUB_BASE_URL, GITHUB_TOKEN, ENCRYPTION_KEY, etc.
python examples/_audit_gitmodules_branch.py
```

If you do adapt one, please **don't** commit the adapted copy with hard-coded
values back to this directory — open a PR with the values templatised
behind environment variables instead.
