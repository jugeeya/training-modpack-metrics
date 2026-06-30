#!/usr/bin/env python3
"""Read raw analytics events from Firebase, aggregate them into daily buckets,
and merge the result into ``data/daily_metrics.json``.

This is the Python port of the original ``training_mod_metrics`` Rust tool. The
SQL it replaces was::

    SELECT
        COUNT(DISTINCT device_id)  AS num_devices,
        COUNT(DISTINCT session_id) AS num_sessions,
        COUNT(*)                   AS num_events,
        DATE_TRUNC('day', event_time) AS date
    FROM events
    GROUP BY date
    ORDER BY date

Events live in the Realtime Database under ``SMASH_OPEN/device`` in a nested
structure (the original tooling flattened it with ``jq -c '.SMASH_OPEN.device[][][]'``).
Each leaf object has the fields: ``device_id``, ``event_name``, ``event_time``,
``menu_settings``, ``session_id``, ``smash_version``, ``mod_version``, ``user_id``.

``event_time`` is epoch **milliseconds**.

Design notes
------------
* **Chunked reads.** The database is never downloaded in a single ``.get()``.
  A cheap ``shallow=True`` read enumerates the top-level nodes under
  ``SMASH_OPEN/device``, then each node's sub-tree is read and processed one at
  a time, so peak memory and per-request size stay bounded to a single node
  rather than the whole tree. Every node is still visited each run (a full
  pass), which is what keeps the past-day finalization below exact.
* This script never deletes from Firebase. It only *reads* and writes the
  aggregated JSON plus a local ``.consumed_paths.json`` manifest. The workflow
  runs ``clear_consumed.py`` to delete the consumed records **after** the
  aggregated data has been committed and pushed, so a failed push can never lose
  data.
* Only days strictly **before today (UTC)** are finalized. A past day is
  guaranteed to be complete (no more events can arrive for it once the run that
  finalizes it has happened), so each finalized day is computed exactly once
  from its full set of events. Today's still-accumulating events are left in
  Firebase for a later run. This keeps distinct counts exact without having to
  persist per-day id sets.
* The merge is idempotent: a date already present in ``daily_metrics.json`` is
  never re-finalized or duplicated, and its source records are left untouched in
  Firebase (so nothing is lost) rather than silently dropped.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, db

# ``SMASH_OPEN/device`` is the root of the nested event tree.
FIREBASE_EVENTS_PATH = "SMASH_OPEN/device"

# Reject anything before 2021-09-01 (matches the original Rust WHERE clause).
MIN_EVENT_TIME_MS = 1_630_454_400_000

REPO_ROOT = Path(__file__).resolve().parent.parent
METRICS_FILE = REPO_ROOT / "data" / "daily_metrics.json"
CONSUMED_PATHS_FILE = REPO_ROOT / ".consumed_paths.json"

EVENT_FIELDS = {
    "device_id",
    "event_name",
    "event_time",
    "menu_settings",
    "session_id",
    "smash_version",
    "mod_version",
    "user_id",
}


def init_firebase() -> None:
    """Initialise the Firebase Admin SDK from environment secrets."""
    service_account = os.environ.get("FIREBASE_SERVICE_ACCOUNT_KEY")
    database_url = os.environ.get("FIREBASE_DATABASE_URL")
    if not service_account or not database_url:
        sys.exit(
            "FIREBASE_SERVICE_ACCOUNT_KEY and FIREBASE_DATABASE_URL must be set."
        )
    cred = credentials.Certificate(json.loads(service_account))
    firebase_admin.initialize_app(cred, {"databaseURL": database_url})


def looks_like_event(node) -> bool:
    """Return True if ``node`` is a leaf event object."""
    return isinstance(node, dict) and "event_time" in node and "device_id" in node


def walk_events(node, path):
    """Yield ``(relative_path, event)`` for every leaf event under ``node``.

    ``relative_path`` is relative to ``FIREBASE_EVENTS_PATH`` and uses ``/`` as a
    separator so it can be fed straight back into a Firebase multi-path update.
    The tree is walked generically so it does not matter whether intermediate
    levels come back as dicts (push ids) or lists (sequential integer keys).
    """
    if looks_like_event(node):
        yield path, node
        return
    if isinstance(node, dict):
        items = node.items()
    elif isinstance(node, list):
        # Firebase returns a list when keys are sequential integers; ``None``
        # holes are possible.
        items = ((str(i), v) for i, v in enumerate(node) if v is not None)
    else:
        return
    for key, child in items:
        child_path = f"{path}/{key}" if path else str(key)
        yield from walk_events(child, child_path)


def event_day(event) -> str | None:
    """Return the UTC ``YYYY-MM-DD`` date for an event, or None if unusable."""
    raw = event.get("event_time")
    try:
        ts_ms = int(raw)
    except (TypeError, ValueError):
        return None
    if ts_ms < MIN_EVENT_TIME_MS:
        return None
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date().isoformat()


def load_existing_metrics() -> list[dict]:
    if not METRICS_FILE.exists():
        return []
    with METRICS_FILE.open() as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError:
            return []
    return data if isinstance(data, list) else []


def top_level_keys(root_ref) -> list[str]:
    """Cheaply enumerate the immediate child keys of the events root.

    A ``shallow=True`` read returns only the keys (mapped to ``True``) without
    any of their values, so this is a single small request regardless of how
    much data lives underneath. Returns ``[]`` when the path is empty.
    """
    shallow = root_ref.get(shallow=True)
    if not shallow:
        return []
    if isinstance(shallow, list):
        # Sequential integer keys come back as a list (with possible holes).
        return [str(i) for i, present in enumerate(shallow) if present is not None]
    return list(shallow.keys())


def iter_chunked_events(root_ref):
    """Yield ``(relative_path, event)`` for every event, one top-level node at a
    time, so the whole tree is never held in memory at once."""
    keys = top_level_keys(root_ref)
    print(
        f"Found {len(keys)} top-level node(s) under {FIREBASE_EVENTS_PATH}; "
        "reading them one at a time."
    )
    for index, key in enumerate(keys, start=1):
        subtree = root_ref.child(key).get()
        if subtree is None:
            continue
        chunk = list(walk_events(subtree, key))
        print(f"  [{index}/{len(keys)}] node '{key}': {len(chunk)} event(s)")
        yield from chunk


def main() -> None:
    init_firebase()
    root_ref = db.reference(FIREBASE_EVENTS_PATH)

    today = datetime.now(timezone.utc).date().isoformat()

    existing = load_existing_metrics()
    existing_dates = {row["date"] for row in existing}

    # Bucket events for complete past days that we have not already finalized.
    # device_ids / session_ids are sets so the counts are distinct.
    buckets: dict[str, dict] = {}
    consumed_paths: list[str] = []
    total_events = 0

    for rel_path, event in iter_chunked_events(root_ref):
        total_events += 1
        day = event_day(event)
        if day is None:
            continue
        # Leave today's (and any future-dated) events in Firebase to accumulate.
        if day >= today:
            continue
        # A day already in the output has been finalized; leave its (late)
        # records in Firebase rather than corrupting the existing counts or
        # dropping the data.
        if day in existing_dates:
            continue
        bucket = buckets.setdefault(
            day, {"device_ids": set(), "session_ids": set(), "num_events": 0}
        )
        bucket["device_ids"].add(event.get("device_id"))
        bucket["session_ids"].add(event.get("session_id"))
        bucket["num_events"] += 1
        consumed_paths.append(rel_path)

    print(f"Read {total_events} event(s) in total.")

    new_rows = [
        {
            "date": day,
            "num_devices": len(bucket["device_ids"]),
            "num_sessions": len(bucket["session_ids"]),
            "num_events": bucket["num_events"],
        }
        for day, bucket in buckets.items()
    ]

    merged = sorted(existing + new_rows, key=lambda row: row["date"])

    METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with METRICS_FILE.open("w") as fh:
        json.dump(merged, fh, indent=2)
        fh.write("\n")

    with CONSUMED_PATHS_FILE.open("w") as fh:
        json.dump(consumed_paths, fh)

    print(
        f"Finalized {len(new_rows)} new day(s); "
        f"{len(consumed_paths)} record(s) queued for deletion."
    )


if __name__ == "__main__":
    main()
