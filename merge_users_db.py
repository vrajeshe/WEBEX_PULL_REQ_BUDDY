#!/usr/bin/env python3
"""Merge encrypted users.db files from primary + standby bot hosts."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from user_store import UserStore

_BOT_DIR = Path(__file__).resolve().parent


def _load_key(env_file: str | None) -> str:
    if env_file:
        load_dotenv(env_file)
    else:
        for candidate in (_BOT_DIR / ".env.jira", _BOT_DIR.parent / ".env.jira"):
            if candidate.exists():
                load_dotenv(candidate)
                break
    key = os.environ.get("ENCRYPTION_KEY", "").strip()
    if not key:
        sys.exit("ENCRYPTION_KEY missing (set in .env.jira or pass --env-file)")
    return key


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "stores",
        nargs="+",
        help="Two or more users.db paths (e.g. from 577 and 619)",
    )
    parser.add_argument(
        "-o",
        "--output",
        required=True,
        help="Merged users.db output path",
    )
    parser.add_argument(
        "--env-file",
        help="Path to .env.jira (default: ./ or parent .env.jira)",
    )
    parser.add_argument(
        "--github-base-url",
        default=os.environ.get(
            "GITHUB_BASE_URL", "https://github.example.com/api/v3"
        ),
    )
    args = parser.parse_args()

    key = _load_key(args.env_file)
    maps = []
    for p in args.stores:
        path = Path(p)
        users = UserStore.read_all(str(path), key)
        print(f"{path}: {len(users)} user(s)")
        maps.append(users)

    merged, notes = UserStore.merge_maps(
        *maps, github_base_url=args.github_base_url
    )
    for line in notes:
        print(f"  conflict: {line}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    UserStore.write_all(str(out), key, merged)
    print(f"Wrote {len(merged)} user(s) -> {out}")


if __name__ == "__main__":
    main()
