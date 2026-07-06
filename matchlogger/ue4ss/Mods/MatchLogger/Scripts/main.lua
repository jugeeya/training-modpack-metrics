-- MatchLogger: Logs player stats per match AND per set as JSON files.
--
-- Rivals 2 session state machine (ERivalsSessionState):
--   CharacterSelect(1) → StageSelect(2) → VersusScreen(3) → Gameplay(4) → Results(5)
-- A *set* is a series of matches sharing one character-select. Characters are
-- picked once (CharacterSelect), then each game runs StageSelect → VersusScreen
-- → Gameplay → Results, looping until a player reaches the required wins.
--
-- We timestamp the boundaries by hooking the widgets that mark them:
--   CharacterSelectScreenWidget created → SET start
--   VersusScreenWidget created          → MATCH start (fires before every game)
--   ResultsScreenWidget created         → MATCH end (+ SET end via IsLastMatchInSet)
print("[MatchLogger] Script loaded")

---@class URivalsCharacterDefinition
---@field ImmutableName FString
---@field DisplayName FText
---@field CharacterFName FName

---@class FResultsPlayerBoxInfo
---@field Character URivalsCharacterDefinition
---@field KOs int32
---@field Deaths int32
---@field SelfDestructs int32
---@field AirTime int32
---@field DamageDealt int32
---@field DamageShielded int32
---@field DamageTaken int32
---@field GrabAttempts int32
---@field GrabSuccesses int32
---@field GroundTime int32
---@field LedgeGrabs int32
---@field ParryAttempts int32
---@field ParrySuccesses int32
---@field PummelAttempts int32
---@field PummelSuccesses int32
---@field Throws int32

---@class UResultsScreenWidget
---@field PlayerEntities ARivalsPlayerEntity[]

-- The JSON files are the record; prints are diagnostics only. Errors always
-- print (a silent mod means silently lost sets), but the informational trace
-- is off by default. Set true when debugging (e.g. after a game patch).
local VERBOSE = false

local function Log(msg)
    if VERBOSE then print(msg) end
end

-- Output folder sits next to the game binary (same dir as Mods/)
local OUTPUT_DIR = "MatchLogger"
local SETS_DIR = OUTPUT_DIR .. "/sets"

-- Create the output directories on load (os.execute works in UE4SS Lua)
os.execute('mkdir "' .. OUTPUT_DIR .. '" 2>nul')
os.execute('mkdir "' .. SETS_DIR .. '" 2>nul')

-- ---------------------------------------------------------------------------
-- Time helpers
-- ---------------------------------------------------------------------------
local function NowIso()
    return os.date("!%Y-%m-%dT%H:%M:%SZ")
end

local function NowStamp()
    return os.date("!%Y%m%d_%H%M%S")
end

local function NowEpoch()
    return os.time()
end

-- ---------------------------------------------------------------------------
-- Set tracking state
-- ---------------------------------------------------------------------------
-- CurrentSet accumulates matches between a CharacterSelect and the final
-- Results screen. It is nil whenever we are not inside a tracked set.
--   {
--     id, startEpoch, startIso,           -- captured at CharacterSelect
--     firstMatchStartIso,                 -- first VersusScreen of the set
--     pendingMatchStartEpoch/Iso,         -- VersusScreen time, consumed at Results
--     matches = { <perMatch tables> },
--   }
local CurrentSet = nil

-- ---------------------------------------------------------------------------
-- Per-match extraction (shared by the per-match file and the set timeline)
-- ---------------------------------------------------------------------------

-- Pulls every player's results off a results widget into a plain Lua table.
---@param widget UResultsScreenWidget
local function ExtractPlayers(widget)
    local activeSlots = widget:GetActivePlayerSlots()
    local playerCount = #activeSlots
    local players = {}

    for i = 1, playerCount do
        local slot = activeSlots[i]:get()

        local playerName = widget:GetPlayerName(slot):ToString()
        local wins = widget:GetPlayerWins(slot)

        ---@type FResultsPlayerBoxInfo
        local results = widget:GetPlayerResultsInfo(slot)

        ---@type URivalsCharacterDefinition
        local charDef = results.Character
        local charName = charDef.ImmutableName:ToString()

        table.insert(players, {
            slot = slot,
            name = playerName,
            character = charName,
            wins = wins,
            kos = results.KOs,
            deaths = results.Deaths,
            falls = results.SelfDestructs,
            damageDealt = results.DamageDealt,
            damageTaken = results.DamageTaken,
            damageShielded = results.DamageShielded,
            airTime = results.AirTime,
            groundTime = results.GroundTime,
            grabAttempts = results.GrabAttempts,
            grabSuccesses = results.GrabSuccesses,
            parryAttempts = results.ParryAttempts,
            parrySuccesses = results.ParrySuccesses,
            pummelAttempts = results.PummelAttempts,
            pummelSuccesses = results.PummelSuccesses,
            ledgeGrabs = results.LedgeGrabs,
            throws = results.Throws,
        })
    end

    return players, playerCount
end

-- Safely call a no-arg results-widget UFunction, returning a fallback on error.
local function SafeCall(widget, fnName, fallback)
    local ok, result = pcall(function() return widget[fnName](widget) end)
    if ok then return result end
    return fallback
end

local function WriteJsonFile(filepath, tbl)
    local file, err = io.open(filepath, "w")
    if file then
        file:write(ToJson(tbl))
        file:close()
        Log("[MatchLogger] Saved: " .. filepath)
        return true
    end
    print("[MatchLogger] ERROR writing file: " .. tostring(err))
    return false
end

-- ---------------------------------------------------------------------------
-- Set finalization
-- ---------------------------------------------------------------------------
local function FinalizeSet(complete)
    if not CurrentSet or #CurrentSet.matches == 0 then
        CurrentSet = nil
        return
    end

    local endEpoch = NowEpoch()
    local lastMatch = CurrentSet.matches[#CurrentSet.matches]

    -- Final standings come from the last match's player snapshot (wins are
    -- cumulative across the set), so we can name a winner by highest wins.
    local standings = {}
    local winner = nil
    for _, p in ipairs(lastMatch.players) do
        table.insert(standings, {
            slot = p.slot,
            name = p.name,
            character = p.character,
            wins = p.wins,
        })
        if not winner or p.wins > winner.wins then
            winner = p
        end
    end

    local report = {
        setId = CurrentSet.id,
        complete = complete,                 -- false if interrupted before the deciding game
        startTime = CurrentSet.startIso,     -- character select entered (nil if mod loaded mid-set)
        firstMatchStartTime = CurrentSet.firstMatchStartIso,
        endTime = NowIso(),
        durationSeconds = CurrentSet.startEpoch and (endEpoch - CurrentSet.startEpoch) or nil,
        winsRequired = CurrentSet.winsRequired,
        matchCount = #CurrentSet.matches,
        winnerSlot = winner and winner.slot or nil,
        winnerName = winner and winner.name or nil,
        winnerCharacter = winner and winner.character or nil,
        players = standings,
        matches = CurrentSet.matches,
    }

    local filename = "set_" .. (CurrentSet.id or NowStamp()) ..
                     (complete and "" or "_interrupted") .. ".json"
    WriteJsonFile(SETS_DIR .. "/" .. filename, report)
    Log(string.format("[MatchLogger] === SET %s (%d matches, complete=%s) ===",
        tostring(CurrentSet.id), #CurrentSet.matches, tostring(complete)))

    CurrentSet = nil
end

-- ---------------------------------------------------------------------------
-- Hook: CharacterSelect → a new set begins
-- ---------------------------------------------------------------------------
NotifyOnNewObject("/Script/Rivals2.CharacterSelectScreenWidget", function()
    -- If a set was still open (e.g. someone quit out before the deciding game),
    -- flush it as interrupted before starting the new one.
    if CurrentSet then
        FinalizeSet(false)
    end

    CurrentSet = {
        id = NowStamp(),
        startEpoch = NowEpoch(),
        startIso = NowIso(),
        firstMatchStartIso = nil,
        pendingMatchStartEpoch = nil,
        pendingMatchStartIso = nil,
        winsRequired = nil,
        matches = {},
    }
    Log("[MatchLogger] Set started (id=" .. CurrentSet.id .. ")")
end)

-- ---------------------------------------------------------------------------
-- Hook: VersusScreen → a match is about to start (fires for every game)
-- ---------------------------------------------------------------------------
NotifyOnNewObject("/Script/Rivals2.VersusScreenWidget", function()
    -- If the mod loaded mid-set (no CharacterSelect seen), start tracking now
    -- with an unknown set-start time.
    if not CurrentSet then
        CurrentSet = {
            id = NowStamp(),
            startEpoch = nil,
            startIso = nil,
            firstMatchStartIso = nil,
            matches = {},
        }
        Log("[MatchLogger] Set start not seen; tracking from this match (id=" .. CurrentSet.id .. ")")
    end

    CurrentSet.pendingMatchStartEpoch = NowEpoch()
    CurrentSet.pendingMatchStartIso = NowIso()
    if not CurrentSet.firstMatchStartIso then
        CurrentSet.firstMatchStartIso = CurrentSet.pendingMatchStartIso
    end
end)

-- ---------------------------------------------------------------------------
-- Hook: Results → a match ended (log match; finalize set if it was the last)
-- ---------------------------------------------------------------------------
NotifyOnNewObject("/Script/Rivals2.ResultsScreenWidget", function()
    Log("[MatchLogger] Results screen created, logging stats...")

    ExecuteWithDelay(2000, function()
        ExecuteInGameThread(function()
            local widget = FindFirstOf("ResultsScreenWidget")
            if not widget or not widget:IsValid() then
                print("[MatchLogger] Could not find ResultsScreenWidget")
                return
            end

            LogFromResultsWidget(widget)
        end)
    end)
end)

---@param widget UResultsScreenWidget
function LogFromResultsWidget(widget)
    _G.widget = widget

    local matchEndEpoch = NowEpoch()
    local matchEndIso = NowIso()

    local players, playerCount = ExtractPlayers(widget)
    local winsRequired = SafeCall(widget, "GetWinsRequired", nil)
    local isLastInSet = SafeCall(widget, "IsLastMatchInSet", nil)

    -- Fallback if IsLastMatchInSet is unavailable: decide from the wins totals.
    if isLastInSet == nil and winsRequired and winsRequired > 0 then
        local maxWins = 0
        for _, p in ipairs(players) do
            if p.wins > maxWins then maxWins = p.wins end
        end
        isLastInSet = maxWins >= winsRequired
    end

    if VERBOSE then
        print(string.format("[MatchLogger] === MATCH RESULTS (%d players, winsRequired=%s, isLastInSet=%s) ===",
            playerCount, tostring(winsRequired), tostring(isLastInSet)))
        for _, p in ipairs(players) do
            print(string.format(
                "[MatchLogger] Slot %d | Name: %s | Char: %s | Wins: %d | KOs: %d | Deaths: %d | Falls: %d | DmgDealt: %d | DmgTaken: %d",
                p.slot, p.name, p.character, p.wins, p.kos, p.deaths, p.falls, p.damageDealt, p.damageTaken))
        end
    end

    -- Make sure a set exists to attach this match to (mid-set mod load).
    if not CurrentSet then
        CurrentSet = {
            id = NowStamp(),
            startEpoch = nil,
            startIso = nil,
            firstMatchStartIso = nil,
            matches = {},
        }
    end
    CurrentSet.winsRequired = winsRequired

    -- Build this match's record, tying in the VersusScreen start time.
    local startEpoch = CurrentSet.pendingMatchStartEpoch
    local matchRecord = {
        index = #CurrentSet.matches + 1,
        startTime = CurrentSet.pendingMatchStartIso,
        endTime = matchEndIso,
        durationSeconds = startEpoch and (matchEndEpoch - startEpoch) or nil,
        playerCount = playerCount,
        players = players,
    }
    -- consume the pending start so the next game doesn't reuse it
    CurrentSet.pendingMatchStartEpoch = nil
    CurrentSet.pendingMatchStartIso = nil

    table.insert(CurrentSet.matches, matchRecord)

    -- Per-match file (backward compatible with the original output)
    local matchReport = {
        timestamp = matchEndIso,
        playerCount = playerCount,
        players = players,
    }
    WriteJsonFile(OUTPUT_DIR .. "/" .. NowStamp() .. ".json", matchReport)
    Log("[MatchLogger] === END RESULTS ===")

    -- If this game decided the set, write the set file and reset.
    if isLastInSet == true then
        FinalizeSet(true)
    end
end

-- ---------------------------------------------------------------------------
-- Minimal JSON serializer (UE4SS Lua doesn't include a JSON library)
-- ---------------------------------------------------------------------------
function ToJson(value, indent)
    indent = indent or 0
    local t = type(value)

    if t == "nil" then
        return "null"
    elseif t == "boolean" then
        return value and "true" or "false"
    elseif t == "number" then
        return tostring(value)
    elseif t == "string" then
        return '"' .. EscapeJsonString(value) .. '"'
    elseif t == "table" then
        -- Check if array (sequential integer keys starting at 1)
        if #value > 0 or next(value) == nil then
            return ArrayToJson(value, indent)
        else
            return ObjectToJson(value, indent)
        end
    else
        return '"' .. tostring(value) .. '"'
    end
end

function EscapeJsonString(s)
    s = s:gsub('\\', '\\\\')
    s = s:gsub('"', '\\"')
    s = s:gsub('\n', '\\n')
    s = s:gsub('\r', '\\r')
    s = s:gsub('\t', '\\t')
    return s
end

function ArrayToJson(arr, indent)
    if #arr == 0 then return "[]" end

    local parts = {}
    local inner = indent + 2
    local pad = string.rep(" ", inner)

    for i = 1, #arr do
        table.insert(parts, pad .. ToJson(arr[i], inner))
    end

    return "[\n" .. table.concat(parts, ",\n") .. "\n" .. string.rep(" ", indent) .. "]"
end

function ObjectToJson(obj, indent)
    local parts = {}
    local inner = indent + 2
    local pad = string.rep(" ", inner)

    -- Sort keys for consistent output
    local keys = {}
    for k in pairs(obj) do
        table.insert(keys, k)
    end
    table.sort(keys, function(a, b) return tostring(a) < tostring(b) end)

    for _, k in ipairs(keys) do
        local key = '"' .. EscapeJsonString(tostring(k)) .. '"'
        local val = ToJson(obj[k], inner)
        table.insert(parts, pad .. key .. ": " .. val)
    end

    if #parts == 0 then return "{}" end
    return "{\n" .. table.concat(parts, ",\n") .. "\n" .. string.rep(" ", indent) .. "}"
end
