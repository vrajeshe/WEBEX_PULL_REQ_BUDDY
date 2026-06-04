# `cron/` — scheduled jobs

Helper scripts and crontab fragments designed to run on the host where
the bot is deployed.

## Files

| File | Purpose |
|---|---|
| `trigger_update_golden_code.sh` | Triggers a Jenkins parameterized job (default: `Update_Golden_Code`) once per branch listed in `golden_branches.txt`. Reads creds from `.jenkins_golden.env` (mode `0600`, **never committed** — see `.gitignore`). |
| `golden_branches.txt` | One branch per line. Lines starting with `#` and blank lines are ignored. Edit this to change the set of branches without touching the script. |
| `sync_users_619_to_577.cron` | Crontab fragment for syncing the encrypted user store from a standby host to the primary host nightly (see `sync_users_619_to_577.sh` at repo root). |

## Setup: nightly Jenkins trigger

1. Create the credentials file (mode `0600`):

   ```bash
   umask 077
   cat > /path/to/cron/.jenkins_golden.env <<EOF
   JENKINS_BASE_URL=https://jenkins.example.com
   JENKINS_JOB_PATH=job/Update_Golden_Code
   JENKINS_USER=your_username
   JENKINS_TOKEN=your_jenkins_api_token
   EOF
   chmod 600 /path/to/cron/.jenkins_golden.env
   ```

2. Edit `golden_branches.txt` with your branch list.

3. Smoke test:

   ```bash
   /path/to/cron/trigger_update_golden_code.sh
   tail /path/to/cron/golden.log
   ```

   Expect `HTTP 201` per branch + a Jenkins queue URL.

4. Install the cron entry (midnight in your local timezone):

   ```cron
   TZ=Asia/Kolkata
   0 0 * * * /path/to/cron/trigger_update_golden_code.sh >> /path/to/cron/golden.log 2>&1
   ```

The script exits non-zero if **any** branch fails to queue.
