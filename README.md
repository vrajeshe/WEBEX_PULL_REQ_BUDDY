# WEBEX_PULL_REQ_BUDDY

A Webex Teams chat-bot that **drives GitHub Enterprise pull-request
workflows** end to end: monitoring check status, posting CI triggers,
opening submodule-bump PRs, backporting changes between branches, and
running scheduled Jenkins jobs.

Originally built to keep a SONiC build-image team unblocked while their
hosts went up and down for maintenance. It runs as a long-lived Python
process, talks to one or more GitHub Enterprise instances via a per-user
PAT model, and uses Webex DMs as the primary UX.

> 🛈 **Heads-up before you adopt this**
> The bot was developed against an internal GitHub Enterprise + Jenkins
> deployment. The shipped code uses placeholder hosts (`github.example.com`,
> `jenkins.example.com`, org `myorg`). Search-and-replace those for your
> environment, or set the matching environment variables. See
> [§ Configuration](#configuration) and [§ Adapting to your org](#adapting-to-your-org).

---

## Table of contents

- [Highlights](#highlights)
- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [User registration model](#user-registration-model)
- [Bot commands](#bot-commands)
- [Submodule workflows](#submodule-workflows)
- [Usage logging](#usage-logging)
- [Backports](#backports)
- [Jenkins integration](#jenkins-integration)
- [Scheduled jobs](#scheduled-jobs)
- [Failover deployment](#failover-deployment)
- [Security model](#security-model)
- [Adapting to your org](#adapting-to-your-org)
- [Layout](#layout)
- [Development](#development)
- [License](#license)

---

## Highlights

- **Per-user GitHub identity** — every command uses the *invoking user's* PAT,
  encrypted at rest with Fernet. No shared service accounts.
- **Webex-native UX** — works in a shared room or 1:1 DM. All replies are
  routed back to DM by default to keep the room quiet.
- **PR check monitoring** — polls a watchlist of PRs, surfaces only **changes**
  in check state, debounces noise, and supports per-PR subscriber lists.
- **Submodule bumps** — single command (`raise_hash_update`) opens a
  ready-to-merge PR that bumps one or many submodules in a parent repo's
  `.gitmodules` to the tip of their tracked branch, with `FRR_TAG` updated
  in `rules/frr.mk` if applicable.
- **Bulk submodule sweep** — `raise_hash_update_for_all_sub_repos` finds
  every stale submodule and bumps them all in one PR.
- **Backports** — `backport <pr_url> <target_branch>` performs a 3-way
  cherry-pick via the GitHub Merges API (conflict-aware) and opens a new
  PR; build-image PR bodies pick up your CICD trigger template
  automatically.
- **Jenkins triggers** — bot commands `jenkins_last`, `jenkins_build`, plus
  cron-based parameterised job triggering with a branch list file.
- **Failover-friendly** — runs on a standby host using the same Webex bot
  token and rsyncs its encrypted user store nightly between hosts.
- **Usage telemetry** — every command is recorded in a rotating
  `usage.log` (5 MB ring buffer, PATs redacted) and inspectable via the
  admin-only `usage_log` command.

---

## Architecture

```
                          ┌──────────────────────────┐
                          │       Webex Cloud        │
                          └────────────┬─────────────┘
                       polls / DMs     │     room messages
                                       │
                                ┌──────▼──────┐
                                │  bot.py     │  long-lived process
                                │             │  (APScheduler + threading)
                                │  - dispatch │
                                │  - monitor  │
                                │  - schedule │
                                └─┬─────────┬─┘
                       reads/writes│         │ uses per-user PAT
                                   │         │
                  ┌────────────────▼──┐   ┌──▼──────────────────┐
                  │  user_store.py    │   │ github_client.py    │
                  │  Fernet-encrypted │   │ REST client + rich  │
                  │  users.db         │   │ submodule helpers   │
                  └───────────────────┘   └──┬──────────────────┘
                                              │
                                       ┌──────▼─────────────┐
                                       │ GitHub Enterprise  │
                                       │ /api/v3            │
                                       └────────────────────┘
                                              ▲
                                              │ optional
                                       ┌──────┴─────────────┐
                                       │ jenkins_client.py  │
                                       │ user-token auth    │
                                       └────────────────────┘
```

Concurrency model: APScheduler runs three independent jobs (Webex
poll, PR-check poll, periodic report) on a `BackgroundScheduler`; all
inbound dispatch is serialized by a single `threading.Lock` so each user's
command runs atomically.

---

## Quick start

### Prerequisites

- Python **3.11** (3.10+ likely works; the deploy host runs 3.11)
- A Webex bot — [create one](https://developer.webex.com/my-apps/new/bot)
- A GitHub Enterprise instance (or `github.com` with caveats — see
  [§ Adapting to your org](#adapting-to-your-org))
- *(optional)* A Jenkins instance you can hit with a user API token

### Install

```bash
git clone git@github.com:vrajeshe/WEBEX_PULL_REQ_BUDDY.git
cd WEBEX_PULL_REQ_BUDDY

python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Configure

```bash
cp .env.example .env
# Open .env and fill in:
#   - WEBEX_BOT_TOKEN, WEBEX_ROOM_ID
#   - GITHUB_BASE_URL, GITHUB_TOKEN
#   - ENCRYPTION_KEY (generate with the command in .env.example)
#   - ADMIN_EMAIL
```

### Run

```bash
./start_bot.sh        # launches `python bot.py` after activating venv
# logs to bot.log (tail -f bot.log)
```

DM your bot `register <YOUR_GITHUB_PAT>` and you're in.

---

## Configuration

All configuration is via environment variables loaded from `.env`
(see [`.env.example`](.env.example) for the full annotated list).

| Variable | Required | Purpose |
|---|---|---|
| `WEBEX_BOT_TOKEN` | ✅ | Bot identity for Webex APIs |
| `WEBEX_ROOM_ID` | ✅ | Room where the bot announces joins / broadcasts |
| `GITHUB_BASE_URL` | ✅ | e.g. `https://github.example.com/api/v3` |
| `GITHUB_TOKEN` | ✅ | Admin/fallback PAT (per-user PATs preferred) |
| `ENCRYPTION_KEY` | ✅ | Fernet key for `users.db` |
| `ADMIN_EMAIL` |  | Receives privileged DMs (`users` etc.) |
| `ALLOWED_USERS` |  | Comma-separated allowlist of registerable emails |
| `DEFAULT_REPO` |  | Bare repo name used by `raise_hash_update` when omitted |
| `DEFAULT_PR_URL` |  | Default PR for legacy single-PR monitoring |
| `POLL_INTERVAL_SECONDS` |  | Webex/PR poll cadence (default 600) |
| `JENKINS_BASE_URL` |  | Enables `jenkins_last` / `jenkins_build` |
| `JENKINS_USER` |  | Jenkins username for API token auth |
| `JENKINS_API_TOKEN` |  | Jenkins user API token |

---

## User registration model

The bot has **no shared service account** for user actions. Every command
that mutates GitHub state uses the invoking user's own Personal Access
Token.

```
You (DM the bot):  register ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
Bot (DM):          ✅ Registered! GitHub user: alice
```

- Tokens are encrypted with Fernet (`ENCRYPTION_KEY`) and stored in
  `users.db`. The DB never leaves disk in plaintext.
- `renew <NEW_PAT>` rotates a stored token after expiry.
- `unregister` removes the user's record.
- `users` (admin only) lists registered users.

`merge_users_db.py` and `sync_users_both_hosts.sh` exist for
multi-host deployments — see [§ Failover deployment](#failover-deployment).

---

## Bot commands

Send any of these in the shared room (mention the bot first) or in DM
(no mention needed). Replies always come back as DMs.

| Command | Purpose |
|---|---|
| `help` | Show the live command list |
| `whoami` | Show registration status + GitHub login |
| `register <PAT>` / `renew <PAT>` / `unregister` | Manage your stored PAT |
| `users` | (admin) list registered users |
| `usage_log` / `usage_log tail [N]` / `usage_log all` / `usage_log stats` / `usage_log <user@example.com>` | (admin) Inspect or export the per-command usage log — see [§ Usage logging](#usage-logging) |
| **PR monitoring** | |
| `monitor <PR_URL\|PR_ID>` | Start watching a PR's checks |
| `status <PR_ID>` / `checks <PR_ID>` | Current checks summary |
| `summary <PR_ID>` / `full_summary <PR_ID>` | PR overview / overview + comments |
| `diff <PR_ID>` | Posted as a downloadable file |
| `notify <PR_ID> <user>` / `unnotify` | Subscribe extra users to status pings |
| `comment <PR_ID> <text>` | Post a comment on the PR |
| `trigger <PR_ID> ut\|precommit\|<custom>` | Post a CI trigger comment |
| `automerge <PR_ID> on\|off` | Toggle GitHub's auto-merge |
| `update_branch <PR_ID>` | Trigger a merge of base into the PR head |
| `queue` | Show your watch queue |
| `interval <seconds>` | Change polling cadence (≥30s) |
| **Submodules** | |
| `raise_hash_update <branch> <hint[,hint…]> <JIRA> [parent]` | Open a PR bumping listed submodules to tip |
| `raise_hash_update_for_all_sub_repos <branch> <JIRA> [parent]` | Bump every stale submodule in one PR |
| **Backports** | |
| `backport <pr_url> <target_branch>` | Cherry-pick a PR onto another branch and open a backport PR |
| **Jenkins** *(if configured)* | |
| `jenkins_last <job URL or path>` | Last build + result |
| `jenkins_build <job URL or path> [KEY=VAL …]` | Queue a build (parameterised if KEY=VAL provided) |

---

## Submodule workflows

The bot's most distinctive feature is its `.gitmodules` automation.

### Single bump

```
raise_hash_update main sub-utilities JIRA-39128
```

- Reads `.gitmodules` on the parent's `main` branch.
- Resolves `sub-utilities` to a unique submodule entry (refine the hint
  if it's ambiguous).
- Determines the source branch in this order:
  1. The submodule's `branch =` field in `.gitmodules`
  2. The parent's branch name (`main`)
  3. `master` / `main`
- Bumps the gitlink (commit SHA) to the tip of that branch.
- For the FRR submodule (`*/sonic-frr/frr` or `repo == frr`) it also
  patches `rules/frr.mk` so `FRR_TAG` matches.
- Opens a PR with the same template + CICD trigger block your team uses
  (auto-detected for `buildimage` repos).

### Multi-submodule, one PR

```
raise_hash_update main sub-mgmt-common,sub-swss JIRA-35315
```

Comma-separated hints → one commit, one PR with all listed gitlinks
bumped. Each hint must resolve uniquely.

### Bulk sweep

```
raise_hash_update_for_all_sub_repos main JIRA-12345
```

Walks **every** submodule in `.gitmodules`, computes its source branch,
keeps only those whose pinned SHA is behind the tip, and opens **one PR**
that bumps them all. Up-to-date and unreachable submodules are listed
in the PR body for transparency.

The PR body for the multi-row case is rendered as a markdown table with
clickable links to each `from`/`to` commit.

---

## Usage logging

Every dispatched command is recorded as one JSON line in a rotating
on-disk log so you can understand who is using what, spot errors, and
guide future improvements.

| Aspect | Behaviour |
|---|---|
| **Where** | `usage.log` next to `bot.py` (override with `USAGE_LOG_PATH`). |
| **Format** | JSONL — one `{"ts":...,"user":...,"cmd":...,"args":...,"source":...,"outcome":...,"detail":...,"elapsed_ms":...}` record per line. |
| **Disk cap** | **5 MB** total: 2.5 MB active + 2.5 MB rotated backup; oldest entries auto-pruned. |
| **PAT redaction** | `register`, `renew`, and `renew_token` arguments are written as the literal `<redacted>` — raw tokens **never** touch the file. |
| **Log-injection guard** | All free-form fields have CR/LF/TAB stripped and are length-bounded before write. |
| **Failure mode** | Logger swallows its own exceptions — usage telemetry can never crash the bot. |

### Inspecting the log (admin only)

```
usage_log              # last 100 entries inline (Webex code block)
usage_log tail 50      # last N entries (max 1000)
usage_log all          # full log delivered as a .jsonl file attachment
usage_log stats        # aggregate counts: by command, by user, errors per cmd
usage_log <user@…>     # full log filtered to that user (file attached)
```

Anyone other than `ADMIN_EMAIL` calling `usage_log` gets a `🚫 admin-only` reply.

### Sample entries

```json
{"ts":"2026-06-08T05:21:11Z","user":"alice@example.com","cmd":"register","args":"<redacted>","source":"dm","outcome":"ok","detail":"","elapsed_ms":42}
{"ts":"2026-06-08T05:21:34Z","user":"alice@example.com","cmd":"monitor","args":"https://github.example.com/myorg/foo/pull/123","source":"dm","outcome":"ok","detail":"","elapsed_ms":318}
{"ts":"2026-06-08T05:23:02Z","user":"bob@example.com","cmd":"raise_hash_update","args":"main sub-utilities JIRA-100","source":"room","outcome":"error","detail":"SubmoduleAmbiguousError: 3 matches","elapsed_ms":612}
```

---

### Branch resolution and drift detection

`raise_hash_update` (and `raise_hash_update_for_all_sub_repos`) treats the
`branch =` field in `.gitmodules` as the **declarative source of truth**
for which branch each submodule tracks.

- When `.gitmodules` declares a branch for a submodule, the bot **always**
  bumps to the tip of *that* branch, even if the currently pinned gitlink
  SHA does not appear in that branch's history. This is the correct
  behaviour for "fix the gitlink back to what `.gitmodules` says".
- If the current gitlink SHA is **not** an ancestor of the declared branch's
  tip — i.e. someone has manually committed a SHA from a *different*
  branch into the parent gitlink — the bot flags this as **branch drift**
  and surfaces a prominent warning in both the PR body (notes column + a
  blockquote) and the Webex reply. The PR itself *corrects* the drift by
  resetting the gitlink onto the declared branch.
- If `.gitmodules` declares a branch that does not exist on the submodule
  remote, the bot surfaces a clear error rather than silently drifting to
  a default branch.

When `.gitmodules` has no `branch =` for a submodule, the bot falls back
to the legacy heuristic: try the submodule's default branch, the parent's
target branch, `master`/`main`, then a paginated list of remote branches,
and pick the first whose tip is descended from the current gitlink SHA.

## Backports

```
backport https://github.example.com/myorg/sub-dhcp-relay/pull/40 main
```

The bot:

1. Resolves the source PR's commit range via the GitHub Merges API.
2. Performs a server-side 3-way merge into a fresh branch off the target.
3. If the merge fails (conflicts), the failure is reported back with the
   list of conflicted paths — no half-applied state is left behind.
4. Opens a brand-new PR titled `[Backport] <original title>`.
5. For `buildimage` repos, the PR body uses the same `raise_hash_update`
   template (PR template + CICD block) so reviewers can trigger
   pre-commit / UT etc. directly.

---

## Jenkins integration

Two flavours:

- **Interactive** — `jenkins_last` / `jenkins_build` bot commands hit
  the configured Jenkins URL using `JENKINS_USER` + `JENKINS_API_TOKEN`.
- **Scheduled** — see [`cron/`](cron/) for a token-based, per-branch
  trigger driven by a plaintext branch list.

---

## Scheduled jobs

[`cron/`](cron/) contains:

- `trigger_update_golden_code.sh` — generic Jenkins parameterised-build
  trigger driven by `cron/golden_branches.txt`. Reads its credentials
  from `cron/.jenkins_golden.env` (mode `0600`, **never committed**).
- `golden_branches.txt` — one branch per line. Edit and the next cron
  run picks it up.
- `sync_users_619_to_577.cron` — example crontab fragment for nightly
  user-store sync between hosts.

See [`cron/README.md`](cron/README.md) for setup.

---

## Failover deployment

The bot uses the same Webex token on two hosts (primary + standby).
Only one runs at a time (Webex would deliver each message twice
otherwise).

```
┌───── primary host ─────┐         ┌──── standby host ────┐
│  bot.py running        │         │  bot.py stopped       │
│  users.db (encrypted)  │ ←─────  │  users.db (mirror)    │
└────────────────────────┘  rsync  └───────────────────────┘
                                   nightly cron via
                                   sync_users_*.sh
```

When the primary host goes down, manually start the bot on the standby.
Both hosts already have the same `users.db`, so no user has to
re-register. Helper scripts:

- `sync_users_both_hosts.sh` — interactive: pull both DBs, merge, push back.
- `sync_users_619_to_577.sh` — one-way push from standby to primary
  (intended for cron).
- `merge_users_db.py` — merge two encrypted DBs into a third with PAT
  validation (last-known-good wins on conflict).

---

## Security model

| Concern | Mitigation |
|---|---|
| GitHub PATs | Per-user, encrypted at rest with Fernet (`ENCRYPTION_KEY`). Never logged. Re-validated on each registration. |
| Webex bot token | Loaded from `.env` only. `.env` is `.gitignore`'d. |
| Jenkins token | Stored in `cron/.jenkins_golden.env` with mode `0600`; `.gitignore`'d. |
| Log injection | All GitHub URLs / SHAs / branch names are logged via parameterized formats. Webex IDs are never logged. |
| Secrets in chat | The bot **never echoes back** a registered PAT. `whoami` shows the GitHub username only. |
| Privilege boundary | Mutations always use the invoking user's PAT — the bot never escalates. |
| TLS | All API calls go over HTTPS (`verify=True`). |

If you find a vulnerability, please open a private issue and tag the
maintainer rather than filing a public PR.

---

## Adapting to your org

The shipped code uses these placeholders — replace before deploying:

| Placeholder | Where | Replace with |
|---|---|---|
| `github.example.com` | `.env.example`, error messages | Your GitHub Enterprise host |
| `jenkins.example.com` | `.env.example`, help text | Your Jenkins host |
| `myorg/` | `bot.py:_resolve_parent_owner_repo`, help text | Your default GitHub org |
| `buildimage` | `github_client.py:raise_submodule_hash_pr`, `.env.example` | The repo whose `.gitmodules` you bump (this is the only repo where the build-image PR-template path is auto-applied) |
| `@example.com` | `bot.py` (3 spots: `_dm_register`, `_cmd_invite`, `_cmd_users`) | Your email domain *(or refactor those to read an `EMAIL_DOMAIN` env var)* |

Most other Cisco-isms in the production scripts have been moved into
[`examples/`](examples/) and are kept verbatim there — they are **not**
part of the runtime.

---

## Layout

```
.
├── bot.py                       # Webex dispatcher + scheduler + commands
├── github_client.py             # GH Enterprise REST client + submodule helpers
├── webex_client.py              # Webex bot client + DM/room routing
├── jenkins_client.py            # Optional Jenkins API wrapper
├── user_store.py                # Encrypted per-user PAT store (Fernet)
├── usage_logger.py              # Rotating JSONL logger of per-command usage (5MB cap, PATs redacted)
├── merge_users_db.py            # Merge two encrypted DBs (with PAT validation)
├── start_bot.sh                 # Activate venv + launch bot
├── sync_users_*.sh              # Multi-host user-store sync scripts
├── cron/                        # Scheduled jobs (Jenkins trigger, etc.)
│   ├── trigger_update_golden_code.sh
│   ├── golden_branches.txt
│   ├── sync_users_619_to_577.cron
│   └── README.md
├── examples/                    # Real one-shot operational scripts (verbatim)
│   ├── _audit_gitmodules_branch.py
│   ├── _open_gitmodules_branch_pr.py
│   ├── _bump_gitlinks_on_existing_branch.py
│   ├── _broadcast_backport_announcement.py
│   └── README.md
├── requirements.txt
├── .env.example
├── .gitignore
├── LICENSE                      # Apache-2.0
└── README.md                    # this file
```

---

## Development

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Run with verbose tracebacks while iterating
PYTHONFAULTHANDLER=1 python bot.py
```

Helpful tips:

- The bot's tests are not yet in this repo — contributions of a `pytest`
  suite are very welcome.
- `github_client.py` is the largest file. The submodule machinery (around
  `_create_submodule_bump_pr_from_rows`, `_resolve_submodule_source_branch`,
  `raise_all_stale_submodules_pr`) is the most interesting reading.
- `bot.py` registers commands inside `_init_commands`. Adding a new
  command is a 5-line edit there + a `_cmd_<name>` handler.

---

## License

Apache License 2.0 — see [`LICENSE`](LICENSE).

Copyright © 2026 Venkata Gouri Rajesh Etla
