#!/usr/bin/env python3
"""Read-only inspector: report what is currently stored under the events path.

This never writes or deletes anything. Use it to verify the state of Firebase
after an aggregation run — e.g. to confirm that only today's (still
accumulating) events remain and that past days have been drained.

It reuses the same chunked-read, walking and date logic as ``aggregate.py`` so
what it reports matches exactly what an aggregation run would see.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from firebase_admin import db

import aggregate


def main() -> None:
    aggregate.init_firebase()
    root_ref = db.reference(aggregate.FIREBASE_EVENTS_PATH)

    today = datetime.now(timezone.utc).date().isoformat()

    total = 0
    undated = 0
    by_date: Counter[str] = Counter()
    devices: set = set()
    sessions: set = set()

    for _rel_path, event in aggregate.iter_chunked_events(root_ref):
        total += 1
        day = aggregate.event_day(event)
        if day is None:
            undated += 1
            continue
        by_date[day] += 1
        devices.add(event.get("device_id"))
        sessions.add(event.get("session_id"))

    print("\n===== Firebase contents =====")
    print(f"path:            {aggregate.FIREBASE_EVENTS_PATH}")
    print(f"total events:    {total}")
    print(f"distinct devices:{len(devices)}")
    print(f"distinct sessions:{len(sessions)}")
    print(f"undated/invalid: {undated}")

    if not by_date:
        print("\nNo dated events remain.")
        if total == 0:
            print("The events path is EMPTY.")
        return

    past = sum(count for day, count in by_date.items() if day < today)
    today_count = by_date.get(today, 0)
    future = sum(count for day, count in by_date.items() if day > today)

    print("\nevents by day (UTC):")
    for day in sorted(by_date):
        marker = "  <- today" if day == today else (" <- FUTURE" if day > today else "")
        print(f"  {day}: {by_date[day]}{marker}")

    print(
        f"\nsummary: {past} past-day event(s) (expected 0 after a drain), "
        f"{today_count} today, {future} future-dated."
    )
    if past:
        print(
            "NOTE: past-day events remain — these are days already present in "
            "daily_metrics.json (left in place on purpose) or a run that has not "
            "finalized them yet."
        )


if __name__ == "__main__":
    main()
