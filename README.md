# Ultimate Training Modpack — Metrics Dashboard

A self-contained project that:

1. Runs a **daily GitHub Action** to pull raw analytics events from a Firebase
   Realtime Database, aggregate them into daily buckets, commit the aggregated
   JSON to the repo, and then clear the consumed records from Firebase.
2. Serves a **static GitHub Pages site** that visualizes the aggregated data as
   an interactive chart.

It is the cloud-native replacement for the local Rust tool at
[`training_mod_metrics`](https://github.com/jugeeya/UltimateTrainingModpack/blob/main/training_mod_metrics/src/main.rs).

## Layout

```
training-modpack-metrics/
├── .github/workflows/daily-aggregate.yml   # daily cron + manual trigger
├── scripts/
│   ├── aggregate.py                        # Firebase → daily buckets → JSON
│   └── clear_consumed.py                   # deletes consumed records (post-push)
├── data/daily_metrics.json                 # aggregated output (committed)
├── index.html                              # static dashboard (Chart.js, dark theme)
├── requirements.txt
└── README.md
```

## How aggregation works

Raw events live in the Realtime Database under `SMASH_OPEN/device` in a nested
structure. Each leaf object has the fields `device_id`, `event_name`,
`event_time` (epoch **milliseconds**), `menu_settings`, `session_id`,
`smash_version`, `mod_version`, `user_id`.

`aggregate.py` reproduces the original SQL:

```sql
SELECT COUNT(DISTINCT device_id)  AS num_devices,
       COUNT(DISTINCT session_id) AS num_sessions,
       COUNT(*)                   AS num_events,
       DATE_TRUNC('day', event_time) AS date
FROM events GROUP BY date ORDER BY date
```

The output `data/daily_metrics.json` is an array, sorted by date ascending:

```json
[
  { "date": "2021-09-01", "num_devices": 42, "num_sessions": 87, "num_events": 1234 }
]
```

### Why it is safe and idempotent

* **Chunked reads.** The database is never pulled down in one request. A cheap
  `shallow=True` read lists the top-level nodes under `SMASH_OPEN/device`, then
  each node's sub-tree is read and processed one at a time, so peak memory and
  per-request size stay bounded to a single node no matter how large the backlog
  is. (Every node is still visited each run — see the next point.)
* **Only complete past days are finalized.** A day is aggregated only once
  it is strictly in the past (UTC), so all of its events are present and its
  distinct counts are exact. Today's still-arriving events are left in Firebase
  for a later run. This avoids splitting a day across runs, so no per-day state
  needs to be persisted.
* **Delete happens only after a successful push.** `aggregate.py` never deletes
  anything — it writes the JSON plus a local `.consumed_paths.json` manifest of
  exactly which Firebase leaf paths were consumed. The workflow commits and
  pushes the JSON first, and only then runs `clear_consumed.py` to delete those
  paths. If the push fails, nothing is deleted and the data is retried next run.
* **No duplicates.** A date already present in `daily_metrics.json` is never
  re-finalized, and records for an already-finalized day are left in Firebase
  rather than corrupting existing counts. Re-running the workflow on the same
  data is a no-op.

## Setup

### 1. Firebase service account

In the [Firebase console](https://console.firebase.google.com/) → Project
Settings → Service accounts → **Generate new private key**. This downloads a
JSON file. You also need your Realtime Database URL (e.g.
`https://<project>-default-rtdb.firebaseio.com`).

The service account needs read + write access to `SMASH_OPEN/device` (write is
required for the delete step).

### 2. GitHub Actions secrets

In the repository → Settings → Secrets and variables → Actions, add:

| Secret | Value |
| --- | --- |
| `FIREBASE_SERVICE_ACCOUNT_KEY` | the **entire contents** of the service-account JSON |
| `FIREBASE_DATABASE_URL` | your Realtime Database URL |

Credentials are never committed — `.gitignore` already excludes
`serviceAccountKey.json`.

### 3. Enable GitHub Pages

Repository → Settings → Pages → **Source: Deploy from a branch**, branch
`main`, folder **`/ (root)`**. The dashboard is then served at
`https://<owner>.github.io/<repo>/`.

### 4. First run

Trigger the workflow manually from the Actions tab (**Run workflow**) instead of
waiting for the 06:00 UTC cron. It will populate `data/daily_metrics.json` and
the dashboard will render on the next Pages build.

## Running locally

```bash
pip install -r requirements.txt
export FIREBASE_SERVICE_ACCOUNT_KEY="$(cat serviceAccountKey.json)"
export FIREBASE_DATABASE_URL="https://<project>-default-rtdb.firebaseio.com"

python scripts/aggregate.py      # writes data/daily_metrics.json + .consumed_paths.json
# inspect data/daily_metrics.json, commit it, THEN:
python scripts/clear_consumed.py # deletes the consumed records from Firebase
```

To preview the dashboard, serve the directory and open it (a plain
`file://` open will not let the page `fetch` the JSON):

```bash
python -m http.server 8000   # then visit http://localhost:8000/
```

## Hosting

This lives in its own public repo (`jugeeya/training-modpack-metrics`) so its
Actions minutes and storage are measured separately from the main modpack repo.
With Pages enabled (step 3 above) the dashboard is served at
`https://jugeeya.github.io/training-modpack-metrics/`.

To host it under the `jugeeya.github.io` user-pages domain root instead, copy
`index.html` + `data/` into the `jugeeya.github.io` repo and point this
workflow there — but note that puts its storage back under that repo rather
than measuring it separately.
