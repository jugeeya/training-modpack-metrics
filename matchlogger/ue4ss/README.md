# Minimal UE4SS profile for the Rivals of Aether II MatchLogger

A stripped-down UE4SS configuration that keeps only what a passive Lua stats
logger needs, and a guide for isolating whatever residual lag remains.

> **Anti-cheat note:** Rivals of Aether II ships with Easy Anti-Cheat. UE4SS
> only injects when the game runs without EAC (e.g. launching the shipping exe
> directly for local/offline play). Keep the MatchLogger to contexts where
> that is permitted.

## Install layout

Copy these files over a **current experimental release** of
[RE-UE4SS](https://github.com/UE4SS-RE/RE-UE4SS/releases) (Rivals 2 is UE5;
the old 0.3.0 stable predates proper UE5.3+ support and had known performance
issues since fixed):

```
Rivals2/Binaries/Win64/
├── Rivals2-Win64-Shipping.exe      (the game — already there)
├── dwmapi.dll                      (UE4SS proxy loader, from the release zip)
├── UE4SS.dll
├── UE4SS-settings.ini              ← replace with the one in this directory
└── Mods/
    ├── mods.txt                    ← replace with the one in this directory
    ├── mods.json                   ← replace with the one in this directory
    ├── MatchLogger/
    │   ├── enabled.txt             (optional; mods.txt already enables it)
    │   └── Scripts/main.lua
    └── ...                         (the mods that ship with UE4SS)
```

Two install mistakes that silently undo the minimal profile:

* **`enabled.txt` overrides everything.** A mod folder containing
  `enabled.txt` loads even when `mods.txt`/`mods.json` says `0`. Delete
  `enabled.txt` from every shipped mod folder you're disabling.
* **Renaming DLLs is not a configuration mechanism.** The console windows are
  controlled by the three switches in `[Debug]`. Renaming `dwmapi.dll` just
  stops UE4SS from loading at all, and renaming other DLLs leaves UE4SS in a
  half-configured state. With this profile in place, no DLLs need renaming.

## Where UE4SS lag actually comes from

Roughly in order of impact for a game like Rivals 2:

1. **The GUI console — and it degrades over time.** `GuiConsoleEnabled = 1`
   spins up an imgui rendering loop on its own thread even when the window is
   hidden (`GuiConsoleVisible = 0` only hides it). Both consoles also append
   every log line to an in-memory buffer that is never trimmed, so the cost
   (and memory) grows the longer the session runs — the classic
   "fine at first, laggy after a while" pattern. This profile disables both
   consoles outright.

2. **Global engine hooks.** Each `= 1` line in `[Hooks]` detours a hot engine
   function and pays a dispatch cost on *every* call, whether or not any mod
   listens. The worst offenders:
   * `HookUObjectProcessEvent` — wraps every UFunction call in the game.
   * `HookProcessInternal` / `HookProcessLocalScriptFunction` — wrap every
     *Blueprint* function call. Rivals 2's game flow is heavily
     Blueprint-based, so these are hot. They are unfortunately also exactly
     what `RegisterHook()` on a Blueprint function needs (see table below).
   * `HookAActorTick` — fires per ticking actor, per frame.
   This profile turns off everything except the script hooks and
   `HookEngineTick`.

3. **The default mod set.** UE4SS ships with six mods enabled
   (CheatManagerEnabler, ConsoleCommands, ConsoleEnabler, LineTrace,
   BPML_GenericFunctions, BPModLoader). Each runs its own Lua state and most
   register hooks or keybinds. None are needed for match logging; all are
   disabled here.

4. **Logging volume.** Every `print()` from Lua goes to `UE4SS.log` (and to
   the console buffers when those are on). A logger that prints per-hit
   debug lines will pay file I/O during gameplay. Log only at set boundaries,
   or buffer in Lua and flush when the set ends.

5. **The MatchLogger itself accumulating work.** See the next section — if
   lag *only* shows up after many sets, the mod is as suspect as UE4SS.

## Progressive lag: audit the MatchLogger for these

The "after a while" symptom has three classic causes inside a Lua mod, all of
which look fine in the first game and degrade linearly with playtime:

* **Hook accumulation.** Calling `RegisterHook()` from inside another
  callback (e.g. re-hooking the scoreboard function every time a match
  starts) without a matching `UnregisterHook()` stacks a new callback each
  time; after 50 sets, every call of the hooked function runs 50 Lua
  callbacks. Register all hooks **once** at script load, or store the
  pre/post IDs `RegisterHook` returns and unregister when the set ends.
* **`LoopAsync` / `NotifyOnNewObject` accumulation.** Same pattern: a loop
  started per-match that never returns `true` (to stop), or object listeners
  registered repeatedly, pile up.
* **Unbounded tables.** Appending every hit/frame event to a table that is
  never flushed grows memory and GC time. Flush to disk at set end and clear.

Quick check: play ~20 sets, watch `UE4SS.log`. If the same message starts
appearing 2×, 3×, 4× per event, hooks are accumulating.

## What each MatchLogger API actually requires

Verified against the RE-UE4SS source (`LuaMod.cpp`): `RegisterHook` on a
native function patches that one function directly and needs **no** global
hooks; only script/Blueprint targets go through the global dispatcher.

| MatchLogger uses…                          | Must keep in `UE4SS-settings.ini`                    |
| ------------------------------------------ | ---------------------------------------------------- |
| `RegisterHook` on a **Blueprint** function | `HookProcessInternal` + `HookProcessLocalScriptFunction` |
| `RegisterHook` on a **native** function    | nothing — direct hook                                |
| `ExecuteInGameThread`                      | `HookEngineTick` (with `DefaultExecuteInGameThreadMethod = EngineTick`) |
| `FindFirstOf` / `FindAllOf` / `StaticFindObject` | `bUseUObjectArrayCache = true` (for speed; works without) |
| `NotifyOnNewObject`                        | nothing — object-array listeners, no `[Hooks]` entry |
| `LoopAsync` / `ExecuteAsync` / file I/O    | nothing                                              |
| `RegisterKeyBind`                          | `Keybinds` mod enabled in `mods.txt`                 |
| `RegisterConsoleCommandHandler`            | `HookProcessConsoleExec = 1` (off in this profile)   |
| `RegisterInitGameStatePreHook/PostHook`    | `HookInitGameState = 1` (off in this profile)        |
| `RegisterLoadMapPreHook/PostHook`          | `HookLoadMap = 1` (off in this profile)              |
| `RegisterBeginPlayPreHook/PostHook`        | `HookBeginPlay = 1` (off in this profile)            |

If the MatchLogger errors at startup or a callback stops firing after
switching to this profile, the log line will name the API — re-enable the
matching row and nothing else.

**Best case:** if the MatchLogger turns out to hook only native functions and
not use `ExecuteInGameThread`, set `HookProcessInternal`,
`HookProcessLocalScriptFunction`, and `HookEngineTick` to `0` too. That
leaves UE4SS with *zero* per-frame presence — injection, the object cache,
and the mod's own directly-hooked functions — which should be
indistinguishable from vanilla.

## Bisecting whatever lag remains

Measure the same scenario each time (e.g. 5 minutes of local versus on the
same stage; use the Steam FPS counter or PresentMon, and note frame *time*
spikes, not just average FPS):

1. **Baseline:** no UE4SS (`dwmapi.dll` removed). Record.
2. **Minimal profile (this directory):** restore `dwmapi.dll`. If this
   already lags noticeably vs. baseline and it's present from the first
   match, test step 3; if it only appears after long sessions, audit the
   MatchLogger per the section above.
3. **Script hooks:** set `HookProcessInternal = 0` and
   `HookProcessLocalScriptFunction = 0` (MatchLogger's BP hooks will stop
   working — this is a measurement, not a fix). If the lag disappears, the
   cost is per-Blueprint-call dispatch; the fix is to move the MatchLogger's
   hooks to native-function targets where possible.
4. **Object cache:** set `bUseUObjectArrayCache = false` and re-measure
   (rarely the cause; also slows `FindFirstOf`).
5. **Everything else was already off.** If lag persists with all of the
   above disabled, it's the injection itself or an interaction with the
   game's build — try the newest experimental UE4SS release before digging
   further.

To confirm the old setup's "after a while" theory cheaply: restore the
*default* settings, play until it lags, then check whether `UE4SS.log` is
huge and whether toggling the GUI console off (this profile's `[Debug]`
section alone) fixes it.
