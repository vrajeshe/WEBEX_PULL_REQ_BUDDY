#!/bin/bash
# Merge users.db from vxr-slurm-577 (WEBEXBOT) and bgl-ads-619 (BOT), push to both.
set -euo pipefail

PRIMARY_HOST="${PRIMARY_HOST:-vxr-slurm-577}"
STANDBY_HOST="${STANDBY_HOST:-bgl-ads-619}"
PRIMARY_DIR="${PRIMARY_DIR:-/nobackup/vrajeshe/WEBEXBOT}"
STANDBY_DIR="${STANDBY_DIR:-/nobackup/vrajeshe/BOT}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

RSYNC_RSH='ssh -o BatchMode=yes -o ConnectTimeout=25'
export RSYNC_RSH

scp $RSYNC_RSH "${PRIMARY_HOST}:${PRIMARY_DIR}/users.db" "$TMP/users-577.db" 2>/dev/null || true
scp $RSYNC_RSH "${STANDBY_HOST}:${STANDBY_DIR}/users.db" "$TMP/users-619.db" 2>/dev/null || true
scp $RSYNC_RSH "${PRIMARY_HOST}:${PRIMARY_DIR}/.env.jira" "$TMP/.env.jira"

STORES=()
[[ -f "$TMP/users-577.db" ]] && STORES+=("$TMP/users-577.db")
[[ -f "$TMP/users-619.db" ]] && STORES+=("$TMP/users-619.db")
if [[ ${#STORES[@]} -eq 0 ]]; then
  echo "No users.db found on either host." >&2
  exit 1
fi

"$SCRIPT_DIR/venv/bin/python" "$SCRIPT_DIR/merge_users_db.py" \
  --env-file "$TMP/.env.jira" \
  "${STORES[@]}" \
  -o "$TMP/users-merged.db"

chmod 600 "$TMP/users-merged.db"
scp $RSYNC_RSH "$TMP/users-merged.db" "${PRIMARY_HOST}:${PRIMARY_DIR}/users.db"
scp $RSYNC_RSH "$TMP/users-merged.db" "${STANDBY_HOST}:${STANDBY_DIR}/users.db"

_restart_bot() {
  local host="$1" dir="$2"
  ssh $RSYNC_RSH "$host" "cd '$dir' && for pid in \$(pgrep -f 'python.*bot.py' 2>/dev/null); do
    cwd=\$(readlink -f /proc/\$pid/cwd 2>/dev/null || echo '')
    [[ \"\$cwd\" == '$dir' ]] && kill \$pid
  done
  sleep 1
  setsid ./start_bot.sh </dev/null >/dev/null 2>&1 &
  sleep 2
  pgrep -af 'python.*bot.py' || true"
}

_restart_bot "$PRIMARY_HOST" "$PRIMARY_DIR"
_restart_bot "$STANDBY_HOST" "$STANDBY_DIR"
echo "Merged users.db deployed to $PRIMARY_HOST and $STANDBY_HOST"
