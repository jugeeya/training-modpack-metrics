#!/usr/bin/env python3
"""Delete consumed records — and purge unread legacy paths — from Firebase.

This runs **after** the aggregated JSON has been committed and pushed, so a
failed push can never cause data loss. It does two things:

1. Deletes exactly the leaf paths listed in the ``.consumed_paths.json`` manifest
   written by ``aggregate.py`` (and nothing that was written after the read).
2. Purges any paths in ``FIREBASE_PURGE_PATHS`` wholesale — data we no longer
   read but that still occupies storage (e.g. ``event/MENU_OPEN``). The purge
   uses only ``shallow`` reads to enumerate keys, so it never downloads the
   subtree's values, and deletes in bounded chunks.

Setting a path to ``None`` deletes it; Firebase prunes any now-empty parent
nodes automatically.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, db, exceptions

# Must match aggregate.py — the consumed paths are relative to this root.
FIREBASE_EVENTS_PATH = os.environ.get(
    "FIREBASE_EVENTS_PATH", "event/SMASH_OPEN/device"
)

# Paths deleted wholesale each run: data we don't read but want reclaimed.
# Comma-separated; override with the FIREBASE_PURGE_PATHS env var.
PURGE_PATHS = [
    p.strip()
    for p in os.environ.get("FIREBASE_PURGE_PATHS", "event/MENU_OPEN").split(",")
    if p.strip()
]

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


def delete_in_chunks(ref, relative_paths) -> int:
    """Delete each relative path under ``ref`` using batched multi-path updates."""
    deleted = 0
    for start in range(0, len(relative_paths), CHUNK_SIZE):
        chunk = relative_paths[start : start + CHUNK_SIZE]
        ref.update({path: None for path in chunk})
        deleted += len(chunk)
    return deleted


def shallow_child_keys(ref) -> list[str]:
    """Enumerate a node's immediate child keys without downloading values."""
    shallow = ref.get(shallow=True)
    if not shallow:
        return []
    if isinstance(shallow, list):
        return [str(i) for i, present in enumerate(shallow) if present is not None]
    return list(shallow.keys())


def purge_node(ref, label: str) -> int:
    """Delete everything under ``ref``, subdividing when a single delete is too
    large. Tries to delete the node wholesale; if Firebase rejects it because the
    subtree exceeds the per-request write-size limit, it recurses into the node's
    children (enumerated with a cheap shallow read) and deletes them one level
    deeper. Returns the number of subtrees actually deleted."""
    try:
        ref.delete()
        return 1
    except exceptions.InvalidArgumentError:
        children = shallow_child_keys(ref)
        if not children:
            raise  # too large but nothing to subdivide — surface the error
        deleted = 0
        for key in children:
            deleted += purge_node(ref.child(key), f"{label}/{key}")
        return deleted


def purge(path: str) -> None:
    """Wipe everything under ``path``. Never deletes the whole path in one
    request; enumerates the top-level children first and deletes each subtree
    (subdividing further only as needed)."""
    ref = db.reference(path)
    children = shallow_child_keys(ref)
    if not children:
        print(f"Purge: '{path}' is already empty.")
        return
    deleted = sum(purge_node(ref.child(key), f"{path}/{key}") for key in children)
    print(f"Purge: deleted {deleted} subtree(s) under '{path}'.")


def main() -> None:
    consumed: list[str] = []
    if CONSUMED_PATHS_FILE.exists():
        with CONSUMED_PATHS_FILE.open() as fh:
            consumed = json.load(fh)

    if not consumed and not PURGE_PATHS:
        print("Nothing to delete.")
        return

    init_firebase()

    if consumed:
        deleted = delete_in_chunks(db.reference(FIREBASE_EVENTS_PATH), consumed)
        print(f"Deleted {deleted} consumed record(s) from {FIREBASE_EVENTS_PATH}.")
    else:
        print("No consumed records to delete.")
    CONSUMED_PATHS_FILE.unlink(missing_ok=True)

    for path in PURGE_PATHS:
        purge(path)


if __name__ == "__main__":
    main()
