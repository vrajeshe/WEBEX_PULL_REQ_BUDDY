#!/bin/bash
# Push users.db from bgl-ads-619 (BOT) to vxr-slurm-577 (WEBEXBOT).
# Intended for cron on 619; 619 is the source of truth while the standby bot runs there.
set -euo pipefail

SOURCE_DIR="${SOURCE_DIR:-/nobackup/vrajeshe/BOT}"
TARGET_HOST="${TARGET_HOST:-vxr-slurm-577}"
TARGET_DIR="${TARGET_DIR:-/nobackup/vrajeshe/WEBEXBOT}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=25 -o StrictHostKeyChecking=accept-new)
LOG_FILE="${LOG_FILE:-$SOURCE_DIR/logs/sync_users_619_to_577.log}"

mkdir -p "$(dirname "$LOG_FILE")"

log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$LOG_FILE"
}

SOURCE_DB="$SOURCE_DIR/users.db"
TARGET_DB="$TARGET_DIR/users.db"

if [[ ! -f "$SOURCE_DB" ]]; then
  log "ERROR: missing source $SOURCE_DB"
  exit 1
fi

if ! ssh "${SSH_OPTS[@]}" "$TARGET_HOST" "test -d '$TARGET_DIR'"; then
  log "ERROR: target dir missing on $TARGET_HOST:$TARGET_DIR"
  exit 1
fi

ssh "${SSH_OPTS[@]}" "$TARGET_HOST" "bash -s" "$TARGET_DIR" "$TARGET_DB" <<'REMOTE'
set -euo pipefail
TARGET_DIR=$1
TARGET_DB=$2
if [[ -f "$TARGET_DB" ]]; then
  cp -a "$TARGET_DB" "${TARGET_DB}.bak.$(date +%Y%m%d)"
fi
ls -1t "${TARGET_DB}.bak."* 2>/dev/null | tail -n +8 | xargs -r rm -f
REMOTE

scp "${SSH_OPTS[@]}" "$SOURCE_DB" "${TARGET_HOST}:${TARGET_DB}"
ssh "${SSH_OPTS[@]}" "$TARGET_HOST" "chmod 600 '$TARGET_DB'"

COUNT=""
if [[ -x "$SOURCE_DIR/venv/bin/python" ]] && [[ -f "$SOURCE_DIR/.env.jira" ]]; then
  COUNT=$(cd "$SOURCE_DIR" && ./venv/bin/python -c "
import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv('.env.jira')
from user_store import UserStore
key = os.environ['ENCRYPTION_KEY']
print(len(UserStore.read_all('users.db', key)))
" 2>/dev/null || true)
fi

BYTES=$(wc -c <"$SOURCE_DB" | tr -d ' ')
if [[ -n "$COUNT" ]]; then
  log "OK: pushed users.db (${BYTES} bytes, ${COUNT} users) -> ${TARGET_HOST}:${TARGET_DB}"
else
  log "OK: pushed users.db (${BYTES} bytes) -> ${TARGET_HOST}:${TARGET_DB}"
fi
