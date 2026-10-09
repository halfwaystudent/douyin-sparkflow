#!/usr/bin/env python3
"""Drop non-nickname entries that older friend scans wrote into usersData.

An earlier reader fell back to a chat row's first text line, which put message
timestamps, dates and unread counters into ``friends_cache`` and
``friend_index``. This one-off tool removes those entries; the next complete
refresh rebuilds the index from real nicknames.

Dry run by default; pass ``--apply`` to write ``usersData.json``.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.config import get_userData, update_user_data  # noqa: E402


CLOCK_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")
DATE_RE = re.compile(r"^(\d{4}[-/年])?\d{1,2}[-/月]\d{1,2}日?$")
DIGITS_RE = re.compile(r"^\d+$")
RELATIVE_MARKERS = frozenset({"刚刚", "昨天", "前天", "今天"})


def looks_like_non_name(value):
    """True when the text can only be a chat-list time, date or unread badge."""
    text = str(value or "").strip()
    if not text:
        return False
    if DIGITS_RE.match(text):
        return True
    if CLOCK_RE.match(text):
        return True
    if DATE_RE.match(text):
        return True
    return text in RELATIVE_MARKERS


def _index_entry_is_junk(key, record):
    if looks_like_non_name(key):
        return True
    visible = str((record or {}).get("visibleName") or "").strip()
    return bool(visible) and looks_like_non_name(visible)


def clean_account(account):
    """Remove junk entries from one account in place; return what was removed."""
    cache = [str(name or "") for name in (account.get("friends_cache") or [])]
    removed_cache = [name for name in cache if looks_like_non_name(name)]
    if removed_cache:
        account["friends_cache"] = [
            name for name in cache if not looks_like_non_name(name)
        ]

    index = dict(account.get("friend_index") or {})
    removed_index = [
        key
        for key, record in index.items()
        if _index_entry_is_junk(key, record)
    ]
    for key in removed_index:
        index.pop(key, None)
    if removed_index:
        account["friend_index"] = index

    return removed_cache, removed_index


def _plan(accounts):
    plan = []
    for account in accounts:
        removed_cache, removed_index = clean_account(account)
        if removed_cache or removed_index:
            plan.append(
                {
                    "account": str(
                        account.get("username")
                        or account.get("unique_id")
                        or "unknown"
                    ),
                    "friends_cache": removed_cache,
                    "friend_index": removed_index,
                }
            )
    return plan


def _report(plan):
    for entry in plan:
        print(
            f"{entry['account']}: friends_cache -{len(entry['friends_cache'])} "
            f"{entry['friends_cache']}; friend_index -{len(entry['friend_index'])} "
            f"{entry['friend_index']}"
        )
    cache_total = sum(len(entry["friends_cache"]) for entry in plan)
    index_total = sum(len(entry["friend_index"]) for entry in plan)
    print(f"total: friends_cache -{cache_total}, friend_index -{index_total}")
    return cache_total + index_total


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the cleaned usersData.json (a snapshot is kept first)",
    )
    args = parser.parse_args(argv)

    if not args.apply:
        removed = _report(_plan(get_userData(force_reload=True)))
        if removed:
            print("dry run; pass --apply to write usersData.json")
        return 0

    plan = []

    def mutate(accounts):
        plan.clear()
        plan.extend(_plan(accounts))
        return None, bool(plan)

    update_user_data(mutate, force_reload=True)
    removed = _report(plan)
    if removed:
        print("written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
