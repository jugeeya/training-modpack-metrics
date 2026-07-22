# MatchLogger ↔ start.gg ↔ VOD splitter — integration design

This document describes how to connect the Rivals of Aether II **MatchLogger**
UE4SS mod (`ue4ss/Mods/MatchLogger/`) to a live tournament: knowing which
station a machine is, pinging when a set ends, optionally reporting the set to
start.gg, and feeding precise timings to the
[VOD splitter](https://github.com/jugeeya/jugeeya.github.io/tree/main/vods).

## The core idea

Everything in the existing toolchain already joins on the same two coordinates:
**station number + wall-clock time**. The VOD splitter fetches sets from the
broker as `{ id, startedAt, completedAt, station, fullRoundText,
players:[{name, character}] }` and computes each clip as `startedAt −
recordingStart − pad`, filtered by station. start.gg is the source of
*identity* (who, which station, which round); the MatchLogger is the source of
*precise timing + characters + stats*. Tying them together just means giving
the MatchLogger the same two coordinates the rest of the system uses.

| Source          | Authoritative for                                          |
| --------------- | ---------------------------------------------------------- |
| **start.gg**    | set id, station, the two entrants, bracket round           |
| **MatchLogger** | frame-accurate set/match start & end, per-game characters, full stats (KOs, damage, parries, …) |
| **Join key**    | station + time window                                      |

## Components

At a real event only **one** person operates consistently (station 1, or a
satellite PC/laptop/phone), while every station needs to report. So the
station PCs run a **headless sender** with no UI, the broker is the
**aggregation hub**, and there is **one operator surface** — a hosted web
console and/or Discord — that sees every station at once.

```mermaid
flowchart LR
    subgraph ST["Stations 1..N PC (headless, no operator)"]
        Game["Rivals 2 + UE4SS<br/>MatchLogger mod"]
        Files["MatchLogger/ JSON<br/>(per-set + current.json)"]
        Sender["Station sender<br/>(watch files → POST)"]
        Game -->|writes| Files
        Sender -->|watches| Files
    end
    Broker["r2tag-broker (Cloudflare Worker)<br/>aggregation store + start.gg token"]
    subgraph OPS["Operator surfaces — one human, all stations"]
        Console["Web console on jugeeya.github.io<br/>(satellite PC / laptop / phone)"]
        Discord["Discord bot<br/>(confirm buttons + /report)"]
    end
    StartGG["start.gg API"]
    Splitter["VOD splitter (browser)"]

    Sender -->|"POST /matchlogger/ingest (station N)"| Broker
    Broker <--> StartGG
    Console <-->|"read sets · confirm report"| Broker
    Discord <-->|"interactions"| Broker
    Broker -->|"/startgg/sets (existing)"| Splitter
```

The design keeps four concerns strictly separated:

- **The mod stays dumb and tournament-agnostic.** It writes JSON to disk and
  nothing else — no networking, no secrets, no station awareness. The same
  install works at any station.
- **The station sender is headless.** A tiny per-station background process
  that watches the MatchLogger folder and POSTs finished sets to the broker
  with its station number. No UI, set-and-forget — this is what lets stations
  2..N run with nobody sitting at them. Its station number is its only config
  (a launch arg or one-line file).
- **The broker is the aggregation hub and holds the secrets.** It stores every
  ingested set per event (keyed by station + time), does the start.gg matching
  and writes, and drives Discord. The start.gg OAuth token and Discord
  credentials never leave the Worker.
- **The operator surface is the human-in-the-loop console**, and there is one
  of it per event, not per station. Two interchangeable forms, both reading
  the broker's aggregated view: a **hosted web console** and **Discord**. All
  ambiguous decisions (confirm a winner, fix an entrant mapping, push a
  report) happen here.

This is the same shape as the existing metrics project (mod → files → sender →
cloud) and the VOD splitter (browser → broker → start.gg).

## Data the mod already writes

`FinalizeSet()` in `main.lua` writes one file per set to `MatchLogger/sets/`:

```jsonc
{
  "setId": "20240115_143000",
  "complete": true,
  "startTime": "2024-01-15T14:30:00Z",       // character select entered
  "firstMatchStartTime": "2024-01-15T14:31:12Z",
  "endTime": "2024-01-15T14:43:05Z",
  "durationSeconds": 785,
  "winsRequired": 3,
  "matchCount": 4,
  "winnerSlot": 1, "winnerName": "…", "winnerCharacter": "clairen",
  "players": [ { "slot": 1, "name": "…", "character": "clairen", "wins": 3 }, … ],
  "matches": [ { "index": 1, "startTime": "…", "endTime": "…", "players": [ …full stats… ] }, … ]
}
```

### Mod additions needed

1. **Epoch timestamps.** The set report has ISO strings; the join with
   start.gg (`startedAt`/`completedAt` are epoch seconds) and with the VOD
   splitter is cleanest if the mod also emits `startEpoch` / `endEpoch`
   (`os.time()` is already computed internally). The sender could parse the
   `Z` ISO strings as UTC instead, but explicit epochs are less error-prone.

2. **A live-state file for "now playing".** To drive the UI's live station
   tracking — and, more importantly, to pre-bind entrant identity *before* a
   set ends — the mod overwrites a single `MatchLogger/current.json` at the
   hooks it already has:

   | Hook (existing)                | `current.json` becomes                              |
   | ------------------------------ | --------------------------------------------------- |
   | CharacterSelect → set start    | `{ "state": "set_start", "setId", "startEpoch" }`   |
   | VersusScreen → match start     | `{ "state": "match_start", "setId", "matchIndex" }` |
   | Results → match/set end         | `{ "state": "idle" }` (per-set file already written) |

   This is a small addition riding on hooks already in `main.lua`, and it is
   what makes identity matching reliable (see below).

## The station sender (headless, per station)

A tiny background process on each game PC — Python or Node, or an eventual
small `.exe`. It has no UI and no secrets; its only config is which station it
is (`--station 3`, or a one-line file). It:

- **Watches** `MatchLogger/sets/*.json` (new set) and `current.json` (live
  state).
- **On set start** (`current.json` → `set_start`): POSTs a lightweight
  heartbeat to `/matchlogger/current` so the broker (and thus the operator
  surface) knows station N just started a set — this is what triggers the
  broker's `/startgg/station` pre-binding.
- **On a new set file:** stamps the station and POSTs it to
  `/matchlogger/ingest`, then marks the file consumed (same "clear after
  consume" pattern as the metrics project).

It retries on failure and is otherwise invisible. Stations 2..N run only this.

## The broker as aggregation hub

The broker stores, per event, every ingested set keyed by station + time, plus
the latest `current` heartbeat per station. That aggregated view is what both
operator surfaces read, so the human sees all stations without anything being
co-located. Suggested shape (Cloudflare KV/R2/D1):

```jsonc
// GET /matchlogger/event?slug=…  → the operator's whole-event view
{
  "stations": {
    "3": { "current": { "state": "match_start", "setId": "…", "since": 170533… },
           "entrants": [ { "id": "…", "name": "…" }, … ] }   // pre-bound at set start
  },
  "sets": [
    { "id": "…", "station": 3, "ingestedAt": 170533…,
      "modSet": { …character/score/stats… },
      "matchedStartggSetId": "12345678",
      "candidateWinnerEntrantId": "…", "confidence": "high|low|none",
      "status": "recorded | matched | notified | reported | error" }
  ]
}
```

### Endpoints

Existing:

- `GET /startgg/sets?slug=…` → completed sets for the VOD splitter (unchanged).

New:

- `POST /matchlogger/current` → body `{ slug, station, current }`. Records the
  heartbeat; on a `set_start`, looks up `/startgg/station` and caches the
  entrants for pre-binding.
- `GET /startgg/station?slug=…&station=N` → the set called/in progress at
  station N: `{ setId, fullRoundText, state, entrants:[{id, name, seed}] }`.
- `POST /matchlogger/ingest` → body `{ slug, station, set }`. Stores the set,
  matches it (station + time window, using the pre-bound entrants), computes a
  candidate winner + confidence, fires the Discord notification. **Read-only
  with respect to the bracket.**
- `GET /matchlogger/event?slug=…` → the aggregated whole-event view above,
  for the web console (and an SSE variant for live updates).
- `POST /matchlogger/report` → body `{ slug, setId, winnerEntrantId,
  gameData? }`. Calls start.gg's `reportBracketSet` mutation. Invoked from an
  explicit operator action on either surface (or auto, guarded — see below).
- `POST /discord/interactions` → Discord's interaction webhook: handles the
  confirm/report buttons and the manual `/report` slash command.

The start.gg token and Discord credentials stay server-side in the Worker.

## Operator surface 1 — the web console (on jugeeya.github.io)

A static page alongside `/vods`, sharing `styles.css` and the broker — no
local server, runs on any satellite PC, laptop, or phone. It reads
`/matchlogger/event` (SSE for live updates) and shows:

- **Config:** event slug (broker URL is implicit).
- **Stations panel:** one live "now playing" card per station from the
  heartbeats — "Station 3: [A] vs [B] — Winners R2".
- **Sets-today table across all stations:** columns for station, time, players
  (character), score, matched start.gg round, and **status**. Ambiguous rows
  expose *confirm winner*, *fix entrant mapping*, *report to start.gg* — each
  a call to `/matchlogger/report`.

## Operator surface 2 — Discord

Interchangeable with the web console, and often the more practical one since
TOs already live in Discord:

- **Notify + confirm inline.** On ingest the broker posts a message to a
  configured channel — "Station 3: set complete, 3–1, ~12 min, winner on
  Clairen → likely **[EntrantA]**" — with **Report 3–1** / **Swap winner** /
  **Ignore** buttons. Clicking Report calls the same `/matchlogger/report`
  path. Works from a phone, no software.
- **Manual `/report` slash command.** `/report station:3 score:3-1
  winner:@Player` — a fallback ingestion path for stations *not* running the
  mod, or for corrections. The broker resolves the station's set and writes
  it, so Discord doubles as a lightweight reporting UI for the whole event.

## Identity matching — the hard part, and the rule

To report a score you must map the game-set to a start.gg set **and its
winner**.

- **Which set?** Broker queries the event for the set called at station N near
  the reported time. Station + time window is usually unique — the same
  assumption the VOD splitter and TSH already rely on.
- **Which entrant won?** Fragile: in-game names (Steam/display) do not
  reliably equal start.gg tags, so exact-match is unreliable. The fix is to
  **capture the two entrants at set start** (the sender's
  `/matchlogger/current` heartbeat triggers the broker's `/startgg/station`
  lookup), so by set end the pairing is known and the winner follows from
  side + score.

**Rule: notify + one-click confirm; never silently guess.** The ingest ping
always fires; an actual bracket write happens only when the operator confirms,
or (later) automatically *only* when identity is unambiguous (e.g. in-game
tags matched start.gg tags exactly). Reporting a wrong score to a live bracket
is worse than not reporting, so the system fails toward pinging a human.

## VOD splitter tie-in

start.gg's `startedAt`/`completedAt` are report/call times (loose). The
MatchLogger's are frame-accurate. Two low-cost wins:

- **Timing export:** because the broker already holds every ingested set, it
  can serve a `sets[]` array in the exact shape the splitter consumes (`{
  startedAt, completedAt, station, fullRoundText, players:[{name, character}]
  }`) but with MatchLogger timestamps — tighter clips, auto-named by merging
  start.gg round text with MatchLogger characters. The splitter just points at
  a `/matchlogger/sets` endpoint instead of `/startgg/sets`.
- **Filename station stamp:** putting the station in the OBS recording
  filename (`Station5_2024-01-15 14-30-00.mkv`) lets the mod, the sender, and
  the splitter agree on station with no extra config, and the splitter can
  auto-select the station from the filename it already parses.

## Where each piece lives

- **`jugeeya.github.io`** — the web console (sibling to `/vods`, shares
  `styles.css`), and this design doc's home. This is the natural repo: it
  already hosts the broker-backed browser tools.
- **The broker Worker** — the new `/matchlogger/*` and `/startgg/station`
  endpoints, the aggregation store, and the Discord bot/interactions.
- **This repo (`training-modpack-metrics/matchlogger/`)** — the mod itself and
  the headless **station sender**, since they install together on the game PC.
- Kept here for now as a working draft; the doc should move to
  `jugeeya.github.io` once a session scoped to that repo can commit it.

## Phasing

- **Phase 0 — sender + console skeleton.** Headless station sender (watch →
  POST) and a static console page reading a stub `/matchlogger/event`;
  aggregated "sets today across stations" table. Unblocks everything.
- **Phase 1 — ingest + Discord notify.** `/matchlogger/ingest` stores sets and
  posts to Discord on set end. Read-only w.r.t. start.gg.
- **Phase 2 — live tracking + confirm-report.** `current.json` mod addition +
  `/matchlogger/current` heartbeat + `/startgg/station` pre-binding; real
  names/round in the console and Discord; one-click report from either surface
  and the `/report` slash command.
- **Phase 3 — guarded auto-report + timing export.** Auto-report only on
  unambiguous identity; `/matchlogger/sets` timing export for the splitter.

## Operational notes

- start.gg token and Discord credentials live only in the broker.
- Bracket writes default to operator confirmation, on whichever surface.
- Stations 2..N are headless; a station that isn't running the mod at all can
  still be reported via the Discord `/report` command.
- The anti-cheat/offline caveat from the mod README still applies — UE4SS only
  injects when the game runs without Easy Anti-Cheat.
