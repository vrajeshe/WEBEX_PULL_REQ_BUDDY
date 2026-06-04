#!/bin/bash
# Primary: WEBEXBOT on vxr-slurm-577. Standby: BOT on bgl-ads-619.
# Run only ONE instance at a time (same Webex bot token).
BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$BOT_DIR"
export TZ="${TZ:-Asia/Kolkata}"
source venv/bin/activate
# venv must be created with: /usr/bin/python3.11 -m venv venv
exec python bot.py >> bot.log 2>&1
