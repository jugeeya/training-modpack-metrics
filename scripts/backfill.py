#!/usr/bin/env python3
"""Backfill ``data/daily_metrics.json`` from local raw analytics event files.

The daily GitHub Action (``aggregate.py``) only reads live events out of
Firebase. This companion script does the same aggregation from **local** files
— e.g. the historical exports the original Rust tool (``training_mod_metrics``)
consumed — so old data can be folded into the dashboard without going through
Firebase.

It reads event objects (the same shape the pipeline uses: ``device_id``,
``session_id``, ``event_time`` [epoch **ms**], plus the usual extra fields),
buckets them per UTC day into ``num_devices`` (distinct device_id),
``num_sessions`` (distinct session_id) and ``num_events`` (row count), and
merges the result into ``data/daily_metrics.json``.

Input formats (auto-detected per file):
  * **NDJSON** — one JSON event object per line (the ``menu_open.json`` /
    ``smash_open.json`` format the Rust tool used).
  * a **JSON array** of event objects.
  * a **nested Firebase export** (e.g. ``export.json``, or a dump of
    ``SMASH_OPEN/device/...``) — the tree is walked generically and every leaf
    object that has ``event_time`` + ``device_id`` is treated as an event.
Directories are scanned recursively for ``*.json`` / ``*.ndjson`` files.

Usage:
    python scripts/backfill.py PATH [PATH ...] [--only-new] [--dry-run]

    PATH         a file or directory of raw events
                 (e.g. C:\\Users\\Josh\\Documents\\Games\\TrainingModpackData)
    --only-new   only add dates that aren't already in the JSON; leave existing
                 dates untouched
    --dry-run    report what would change without writing the file

Merge policy (default): dates not already present are added. For a date present
in **both** the JSON and the backfill, each metric is set to the **max** of the
two values, so a backfill never *reduces* a count — it enriches sparse early
days without clobbering the complete counts the live pipeline already produced.
Pass ``--only-new`` to skip overlapping dates entirely.

No third-party dependencies — standard library only.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Reject anything before 2021-09-01 (matches aggregate.py / the original Rust
# WHERE clause). Value is epoch milliseconds.
MIN_EVENT_TIME_MS = 1_630_454_400_000

REPO_ROOT = Path(__file__).resolve().parent.parent
METRICS_FILE = REPO_ROOT / "data" / "daily_metrics.json"


def looks_like_event(node) -> bool:
    return isinstance(node, dict) and "event_time" in node and "device_id" in node


def walk_events(node):
    """Yield every leaf event object under ``node`` (dicts or lists, any depth)."""
    if looks_like_event(node):
        yield node
        return
    if isinstance(node, dict):
        children = node.values()
    elif isinstance(node, list):
        children = (v for v in node if v is not None)
    else:
        return
    for child in children:
        yield from walk_events(child)


def iter_events_from_file(path: Path):
    """Yield event objects from one file, auto-detecting the format."""
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return
    # First try to parse the whole file as a single JSON document (array or
    # nested export). If that fails, fall back to NDJSON (one object per line).
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        for line in text.splitlines():
            line = line.strip().rstrip(",")
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield from walk_events(obj)
        return
    yield from walk_events(doc)


def iter_input_files(paths):
    """Expand paths (files or dirs) into a de-duplicated list of data files."""
    seen = set()
    for raw in paths:
        p = Path(raw).expanduser()
        if not p.exists():
            print(f"! Path not found, skipping: {p}", file=sys.stderr)
            continue
        candidates = (
            sorted(q for q in p.rglob("*") if q.is_file() and q.suffix.lower() in (".json", ".ndjson"))
            if p.is_dir()
            else [p]
        )
        for q in candidates:
            key = q.resolve()
            if key not in seen:
                seen.add(key)
                yield q


def normalize_ms(raw):
    """Return an epoch-ms int for a timestamp, tolerating seconds. None if bad."""
    try:
        ts = int(raw)
    except (TypeError, ValueError):
        return None
    # Heuristic: 2021+ in ms is ~1.6e12 (13 digits); the same in seconds is
    # ~1.6e9 (10 digits). Promote plausible second-scale values to ms.
    if 1_000_000_000 <= ts < 100_000_000_000:
        ts *= 1000
    return ts


def event_day(event, today):
    """UTC ``YYYY-MM-DD`` for a usable event, or None if it should be dropped.

    Matches aggregate.py's cutoff: today's still-accumulating events and
    anything dated later (near-future clock skew, or genuine future garbage)
    are dropped rather than finalized, since a backfill run can't guarantee
    it has seen every event for an incomplete day.
    """
    ts_ms = normalize_ms(event.get("event_time"))
    if ts_ms is None:
        return None
    if ts_ms < MIN_EVENT_TIME_MS:
        return None
    day = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date().isoformat()
    if day >= today:
        return None
    return day


def load_existing():
    if not METRICS_FILE.exists():
        return []
    try:
        data = json.loads(METRICS_FILE.read_text())
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill daily_metrics.json from local event files.")
    ap.add_argument("paths", nargs="+", help="files or directories of raw events")
    ap.add_argument("--only-new", action="store_true",
                    help="only add dates not already present; don't update existing dates")
    ap.add_argument("--dry-run", action="store_true",
                    help="report changes without writing the file")
    args = ap.parse_args()

    today = datetime.now(timezone.utc).date().isoformat()

    buckets: dict[str, dict] = {}
    total = kept = 0
    files = list(iter_input_files(args.paths))
    if not files:
        sys.exit("No .json/.ndjson files found under the given path(s).")
    print(f"Scanning {len(files)} file(s)…")
    for path in files:
        n_file = 0
        for event in iter_events_from_file(path):
            total += 1
            day = event_day(event, today)
            if day is None:
                continue
            kept += 1
            n_file += 1
            b = buckets.setdefault(day, {"device_ids": set(), "session_ids": set(), "num_events": 0})
            b["device_ids"].add(event.get("device_id"))
            b["session_ids"].add(event.get("session_id"))
            b["num_events"] += 1
        print(f"  {path}: {n_file} usable event(s)")

    print(f"\nRead {total} event(s); {kept} usable across {len(buckets)} day(s).")
    if not buckets:
        sys.exit("Nothing to backfill — no usable events (check the path/format).")

    backfill = {
        day: {
            "num_devices": len(b["device_ids"]),
            "num_sessions": len(b["session_ids"]),
            "num_events": b["num_events"],
        }
        for day, b in buckets.items()
    }

    existing = {row["date"]: row for row in load_existing()}
    result = dict(existing)
    added, updated = [], []
    for day, bf in backfill.items():
        if day not in existing:
            result[day] = {"date": day, **bf}
            added.append(day)
        elif not args.only_new:
            cur = existing[day]
            merged = {k: max(cur.get(k, 0), bf[k]) for k in ("num_devices", "num_sessions", "num_events")}
            if any(merged[k] != cur.get(k) for k in merged):
                result[day] = {"date": day, **merged}
                updated.append(day)

    final = sorted(result.values(), key=lambda r: r["date"])

    print(f"\n{'DRY RUN — ' if args.dry_run else ''}Result:")
    print(f"  dates before: {len(existing)}  ({_range(existing)})")
    print(f"  dates after:  {len(result)}  ({_range({r['date']: r for r in final})})")
    print(f"  added:   {len(added)} day(s)" + (f"  ({added[0]} … {added[-1]})" if added else ""))
    print(f"  updated: {len(updated)} day(s)" + ("  [max-merged]" if updated else ""))

    if args.dry_run:
        print("\n(dry run — data/daily_metrics.json not written)")
        return
    if not added and not updated:
        print("\nNo change — data/daily_metrics.json already covers this data.")
        return

    METRICS_FILE.write_text(json.dumps(final, indent=2) + "\n")
    print(f"\nWrote {METRICS_FILE.relative_to(REPO_ROOT)} ({len(final)} rows). "
          "Review it, then commit + push.")


def _range(rows_by_date):
    if not rows_by_date:
        return "empty"
    dates = sorted(rows_by_date)
    return f"{dates[0]} … {dates[-1]}"


if __name__ == "__main__":
    main()
