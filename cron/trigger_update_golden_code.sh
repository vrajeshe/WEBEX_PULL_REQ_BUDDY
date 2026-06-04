#!/bin/bash
# Daily trigger for Jenkins job: Update_Golden_Code
# - Credentials come from .jenkins_golden.env (mode 0600).
# - Branch list comes from golden_branches.txt (one branch per line).
#   Lines starting with `#` and blank lines are ignored.
# Logs to cron/golden.log next to this script.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.jenkins_golden.env"
BRANCHES_FILE="${SCRIPT_DIR}/golden_branches.txt"
LOG_FILE="${SCRIPT_DIR}/golden.log"

log() { echo "[$(date -Is)] $*" >> "$LOG_FILE"; }
fail() { echo "[$(date -Is)] ERROR: $*" >&2; log "ERROR: $*"; exit 2; }

[[ -r "$ENV_FILE"      ]] || fail "missing or unreadable $ENV_FILE"
[[ -r "$BRANCHES_FILE" ]] || fail "missing or unreadable $BRANCHES_FILE"

# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

: "${JENKINS_BASE_URL:?missing JENKINS_BASE_URL}"
: "${JENKINS_JOB_PATH:?missing JENKINS_JOB_PATH}"
: "${JENKINS_USER:?missing JENKINS_USER}"
: "${JENKINS_TOKEN:?missing JENKINS_TOKEN}"

URL="${JENKINS_BASE_URL%/}/${JENKINS_JOB_PATH#/}/buildWithParameters"

mapfile -t branches < <(
  sed -e 's/[[:space:]]*#.*$//' -e 's/^[[:space:]]\+//' -e 's/[[:space:]]\+$//' \
      "$BRANCHES_FILE" \
    | awk 'NF>0'
)

if (( ${#branches[@]} == 0 )); then
  fail "no branches found in $BRANCHES_FILE"
fi

log "=== run start: ${#branches[@]} branch(es) from $BRANCHES_FILE ==="

response_headers="$(mktemp)"
trap 'rm -f "$response_headers"' EXIT

failures=0
for branch in "${branches[@]}"; do
  log "trigger BRANCH_TO_UPDATE=$branch"
  http_code="$(curl -sS -gL -o /dev/null -D "$response_headers" \
    -w '%{http_code}' \
    --connect-timeout 30 --max-time 120 \
    -u "${JENKINS_USER}:${JENKINS_TOKEN}" \
    -X POST \
    --data-urlencode "BRANCH_TO_UPDATE=${branch}" \
    "$URL")" || http_code="000"

  queue_url="$(awk -F': ' 'tolower($1)=="location"{print $2}' "$response_headers" \
                | tr -d '\r' | tail -n1)"

  if [[ "$http_code" == "201" || "$http_code" == "200" ]]; then
    log "  HTTP $http_code  queue=${queue_url:-<none>}"
    echo "OK   $branch  HTTP $http_code  ${queue_url:-}"
  else
    failures=$((failures + 1))
    log "  HTTP $http_code  FAILED to queue $branch"
    echo "FAIL $branch  HTTP $http_code" >&2
  fi

  sleep 2
done

log "=== run end: ${#branches[@]} requested, $failures failure(s) ==="

(( failures == 0 ))
