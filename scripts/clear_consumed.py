#!/usr/bin/env python3
"""Delete the records that ``aggregate.py`` consumed, from Firebase.

This runs **after** the aggregated JSON has been committed and pushed, so a
failed push can never cause data loss. It reads the ``.consumed_paths.json``
manifest written by ``aggregate.py`` and deletes exactly those leaf paths (and
nothing that was written to Firebase after the read), using a single multi-path
update. Setting a path to ``None`` deletes it; Firebase prunes any now-empty
parent nodes automatically.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, db

# Must match aggregate.py — the consumed paths are relative to this root.
FIREBASE_EVENTS_PATH = os.environ.get(
    "FIREBASE_EVENTS_PATH", "event/SMASH_OPEN/device"
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONSUMED_PATHS_FILE = REPO_ROOT / ".consumed_paths.json"

# Firebase rejects very large multi-path updates; delete in chunks.
CHUNK_SIZE = 500


def init_firebase() -> None:
    service_account = os.environ.get("FIREBASE_SERVICE_ACCOUNT_KEY")
    database_url = os.environ.get("FIREBASE_DATABASE_URL")
    if not service_account or not database_url:
        sys.exit(
            "FIREBASE_SERVICE_ACCOUNT_KEY and FIREBASE_DATABASE_URL must be set."
        )
    cred = credentials.Certificate(json.loads(service_account))
    firebase_admin.initialize_app(cred, {"databaseURL": database_url})


def main() -> None:
    if not CONSUMED_PATHS_FILE.exists():
        print("No consumed-paths manifest found; nothing to delete.")
        return

    with CONSUMED_PATHS_FILE.open() as fh:
        paths = json.load(fh)

    if not paths:
        print("No consumed records to delete.")
        CONSUMED_PATHS_FILE.unlink(missing_ok=True)
        return

    init_firebase()
    ref = db.reference(FIREBASE_EVENTS_PATH)

    deleted = 0
    for start in range(0, len(paths), CHUNK_SIZE):
        chunk = paths[start : start + CHUNK_SIZE]
        ref.update({path: None for path in chunk})
        deleted += len(chunk)

    print(f"Deleted {deleted} consumed record(s) from {FIREBASE_EVENTS_PATH}.")
    CONSUMED_PATHS_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
