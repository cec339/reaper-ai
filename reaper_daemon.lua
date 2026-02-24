-- REAPER AI FX Chain Controller - Lua Daemon
-- Run via Actions > Run ReaScript in REAPER
-- Polls queue/in/ for JSON commands, executes them, writes responses to queue/out/

local POLL_INTERVAL = 0.1  -- seconds between polls
local QUEUE_BASE = nil      -- set from config or default

-- ---------------------------------------------------------------------------
-- Utility helpers
-- ---------------------------------------------------------------------------

local function file_exists(path)
  local f = io.open(path, "r")
  if f then f:close() return true end
  return false
end

local function read_file(path)
  local f = io.open(path, "r")
  if not f then return nil end
  local content = f:read("*a")
  f:close()
  return content
end

local function write_file(path, content)
  local f = io.open(path, "w")
  if not f then return false end
  f:write(content)
  f:close()
  return true
end

local function delete_file(path)
  os.remove(path)
end

-- Minimal JSON encoder (handles strings, numbers, booleans, tables, nil)
local function json_encode(val)
  if val == nil then return "null" end
  local t = type(val)
  if t == "boolean" then return val and "true" or "false" end
  if t == "number" then return tostring(val) end
  if t == "string" then
    local s = val:gsub('\\', '\\\\'):gsub('"', '\\"'):gsub('\n', '\\n'):gsub('\r', '\\r'):gsub('\t', '\\t')
    return '"' .. s .. '"'
  end
  if t == "table" then
    -- Check if array
    local is_array = (#val > 0) or next(val) == nil
    if is_array then
      local parts = {}
      for i = 1, #val do
        parts[i] = json_encode(val[i])
      end
      return "[" .. table.concat(parts, ",") .. "]"
    else
      local parts = {}
      for k, v in pairs(val) do
        parts[#parts + 1] = json_encode(tostring(k)) .. ":" .. json_encode(v)
      end
      return "{" .. table.concat(parts, ",") .. "}"
    end
  end
  return "null"
end

-- Minimal JSON decoder
local json_decode
do
  local function skip_ws(s, i)
    while i <= #s and s:sub(i,i):match("[ \t\r\n]") do i = i + 1 end
    return i
  end

  local function parse_string(s, i)
    -- i points to opening quote
    i = i + 1
    local parts = {}
    while i <= #s do
      local c = s:sub(i,i)
      if c == '"' then
        return table.concat(parts), i + 1
      elseif c == '\\' then
        i = i + 1
        c = s:sub(i,i)
        if c == 'n' then parts[#parts+1] = '\n'
        elseif c == 'r' then parts[#parts+1] = '\r'
        elseif c == 't' then parts[#parts+1] = '\t'
        elseif c == '"' then parts[#parts+1] = '"'
        elseif c == '\\' then parts[#parts+1] = '\\'
        elseif c == '/' then parts[#parts+1] = '/'
        elseif c == 'u' then
          -- basic unicode escape (ASCII range only)
          local hex = s:sub(i+1, i+4)
          parts[#parts+1] = string.char(tonumber(hex, 16) or 63)
          i = i + 4
        end
        i = i + 1
      else
        parts[#parts+1] = c
        i = i + 1
      end
    end
    return table.concat(parts), i
  end

  local function parse_value(s, i)
    i = skip_ws(s, i)
    if i > #s then return nil, i end
    local c = s:sub(i,i)

    if c == '"' then
      return parse_string(s, i)
    elseif c == '{' then
      local obj = {}
      i = skip_ws(s, i + 1)
      if s:sub(i,i) == '}' then return obj, i + 1 end
      while true do
        i = skip_ws(s, i)
        local key
        key, i = parse_string(s, i)
        i = skip_ws(s, i)
        i = i + 1  -- skip colon
        local val
        val, i = parse_value(s, i)
        obj[key] = val
        i = skip_ws(s, i)
        if s:sub(i,i) == '}' then return obj, i + 1 end
        i = i + 1  -- skip comma
      end
    elseif c == '[' then
      local arr = {}
      i = skip_ws(s, i + 1)
      if s:sub(i,i) == ']' then return arr, i + 1 end
      while true do
        local val
        val, i = parse_value(s, i)
        arr[#arr + 1] = val
        i = skip_ws(s, i)
        if s:sub(i,i) == ']' then return arr, i + 1 end
        i = i + 1  -- skip comma
      end
    elseif c == 't' then
      return true, i + 4
    elseif c == 'f' then
      return false, i + 5
    elseif c == 'n' then
      return nil, i + 4
    else
      -- number
      local j = i
      if s:sub(j,j) == '-' then j = j + 1 end
      while j <= #s and s:sub(j,j):match("[0-9.eE%+%-]") do j = j + 1 end
      return tonumber(s:sub(i, j-1)), j
    end
  end

  json_decode = function(s)
    if not s or s == "" then return nil end
    local val, _ = parse_value(s, 1)
    return val
  end
end

-- ---------------------------------------------------------------------------
-- Normalize a string for fuzzy matching
-- ---------------------------------------------------------------------------
local function normalize(s)
  if not s then return "" end
  return s:lower():gsub("[%s%p]", "")
end

-- ---------------------------------------------------------------------------
-- Resolve queue base path from config.json next to this script
-- ---------------------------------------------------------------------------
local function resolve_queue_path()
  -- Try to find the script path
  local info = debug.getinfo(1, "S")
  local script_dir = nil
  if info and info.source then
    local src = info.source:gsub("^@", "")
    script_dir = src:match("(.+)[/\\]")
  end

  -- Try config.json
  if script_dir then
    local cfg_path = script_dir .. "/config.json"
    local cfg_raw = read_file(cfg_path)
    if cfg_raw then
      local cfg = json_decode(cfg_raw)
      if cfg and cfg.queue_path then
        return cfg.queue_path
      end
    end
    -- Default to queue/ next to script
    return script_dir .. "/queue"
  end

  -- Fallback: use LOCALAPPDATA/reaper-ai/queue (matches installer default)
  local appdata = os.getenv("LOCALAPPDATA")
  if appdata then
    return appdata:gsub("\\", "/") .. "/reaper-ai/queue"
  end
  return "reaper-ai/queue"
end

-- ---------------------------------------------------------------------------
-- REAPER operations
-- ---------------------------------------------------------------------------

local function find_track_by_name(name)
  if name:lower() == "master" then
    return reaper.GetMasterTrack(0), -1
  end
  local count = reaper.CountTracks(0)
  for i = 0, count - 1 do
    local tr = reaper.GetTrack(0, i)
    local _, tr_name = reaper.GetTrackName(tr)
    if tr_name == name then return tr, i end
  end
  -- Fuzzy: case-insensitive substring
  local name_lower = name:lower()
  for i = 0, count - 1 do
    local tr = reaper.GetTrack(0, i)
    local _, tr_name = reaper.GetTrackName(tr)
    if tr_name:lower():find(name_lower, 1, true) then return tr, i end
  end
  return nil, nil
end

-- Strict track finder: errors if fuzzy match hits multiple tracks
-- Returns track, index, error_string
local function find_track_strict(name_or_index)
  -- Numeric index
  if type(name_or_index) == "number" then
    local idx = math.floor(name_or_index)
    local count = reaper.CountTracks(0)
    if idx < 0 or idx >= count then
      return nil, nil, "Track index " .. idx .. " out of range (0-" .. (count - 1) .. ")"
    end
    return reaper.GetTrack(0, idx), idx, nil
  end
  -- String: try exact first
  local name = tostring(name_or_index)
  if name:lower() == "master" then
    return reaper.GetMasterTrack(0), -1, nil
  end
  local count = reaper.CountTracks(0)
  for i = 0, count - 1 do
    local tr = reaper.GetTrack(0, i)
    local _, tr_name = reaper.GetTrackName(tr)
    if tr_name == name then return tr, i, nil end
  end
  -- Fuzzy: collect ALL substring matches
  local name_lower = name:lower()
  local matches = {}
  for i = 0, count - 1 do
    local tr = reaper.GetTrack(0, i)
    local _, tr_name = reaper.GetTrackName(tr)
    if tr_name:lower():find(name_lower, 1, true) then
      matches[#matches + 1] = { tr = tr, idx = i, name = tr_name }
    end
  end
  if #matches == 0 then
    return nil, nil, "Track not found: " .. name
  end
  if #matches == 1 then
    return matches[1].tr, matches[1].idx, nil
  end
  -- Ambiguous
  local names = {}
  for _, m in ipairs(matches) do
    names[#names + 1] = string.format("[%d] %s", m.idx, m.name)
  end
  return nil, nil, "Ambiguous track name '" .. name .. "' matches " .. #matches
    .. " tracks: " .. table.concat(names, ", ")
end

-- Resolve track from name (string) or index (number), using find_track_by_name
-- for backward compat (first-match fuzzy)
local function resolve_track(name_or_index)
  if type(name_or_index) == "number" then
    local idx = math.floor(name_or_index)
    local count = reaper.CountTracks(0)
    if idx < 0 or idx >= count then
      return nil, nil
    end
    return reaper.GetTrack(0, idx), idx
  end
  return find_track_by_name(tostring(name_or_index))
end

local function find_param_index(tr, fx_idx, param_name)
  local norm_target = normalize(param_name)
  local param_count = reaper.TrackFX_GetNumParams(tr, fx_idx)
  -- Exact normalized match first
  for pi = 0, param_count - 1 do
    local _, pname = reaper.TrackFX_GetParamName(tr, fx_idx, pi)
    if normalize(pname) == norm_target then return pi end
  end
  -- Substring match
  for pi = 0, param_count - 1 do
    local _, pname = reaper.TrackFX_GetParamName(tr, fx_idx, pi)
    if normalize(pname):find(norm_target, 1, true) then return pi end
  end
  return nil
end

-- ---------------------------------------------------------------------------
-- Unit conversion helpers
-- ---------------------------------------------------------------------------

-- MIDI note 0-127 -> normalized 0.0-1.0
local function midi_note_to_norm(note)
  return math.max(0, math.min(127, note)) / 127.0
end

-- Scan all RS5k instances across all tracks for used MIDI notes
local function scan_used_midi_notes()
  local used = {}
  local count = reaper.CountTracks(0)
  for i = 0, count - 1 do
    local tr = reaper.GetTrack(0, i)
    local fx_count = reaper.TrackFX_GetCount(tr)
    for fi = 0, fx_count - 1 do
      local _, fx_name = reaper.TrackFX_GetFXName(tr, fi)
      if fx_name:find("RS5K") or fx_name:find("ReaSamplOmatic5000") or fx_name:find("reasamplomatic") then
        -- Note range start param: typically named "Note range start"
        local pi = find_param_index(tr, fi, "Note range start")
        if pi then
          local val = reaper.TrackFX_GetParamNormalized(tr, fi, pi)
          local note = math.floor(val * 127 + 0.5)
          used[note] = true
        end
      end
    end
  end
  return used
end

-- General MIDI drum map defaults
local GM_DRUM_DEFAULTS = {
  kick = 36, snare = 38, hihat = 42, ["hi-hat"] = 42,
  ["hihat open"] = 46, ["hi-hat open"] = 46,
  ["tom low"] = 50, ["tom mid"] = 47, ["tom high"] = 52,
  crash = 49, ride = 51,
}

-- Pick a MIDI note: use default for drum type if available and unused, else find next free
local function pick_midi_note(drum_type, explicit_note)
  if explicit_note then return explicit_note end
  local used = scan_used_midi_notes()
  -- Try GM default
  if drum_type then
    local default_note = GM_DRUM_DEFAULTS[drum_type:lower()]
    if default_note and not used[default_note] then
      return default_note
    end
  end
  -- Find first unused note in drum range 35-81
  for n = 35, 81 do
    if not used[n] then return n end
  end
  -- Fallback: 60 (C4)
  return 60
end

local function op_get_context()
  local tracks = {}
  local count = reaper.CountTracks(0)
  for i = 0, count - 1 do
    local tr = reaper.GetTrack(0, i)
    local _, tr_name = reaper.GetTrackName(tr)
    local selected = reaper.IsTrackSelected(tr)
    local fx_count = reaper.TrackFX_GetCount(tr)
    local fx_list = {}
    for fi = 0, fx_count - 1 do
      local _, fx_name = reaper.TrackFX_GetFXName(tr, fi)
      fx_list[#fx_list + 1] = fx_name
    end
    tracks[#tracks + 1] = {
      index = i,
      name = tr_name,
      selected = selected,
      fx = fx_list
    }
  end

  -- Enumerate all installed FX (no arbitrary cap)
  local installed = {}
  local idx = 0
  while true do
    local retval, name = reaper.EnumInstalledFX(idx)
    if not retval or name == nil or name == "" then break end
    installed[#installed + 1] = name
    idx = idx + 1
  end

  return { tracks = tracks, installed_fx = installed }
end

local function op_get_track_fx(cmd)
  local track_name = cmd.track
  if not track_name then return { status = "error", errors = {"Missing track name"} } end

  local tr, _ = find_track_by_name(track_name)
  if not tr then return { status = "error", errors = {"Track not found: " .. track_name} } end

  local _, actual_name = reaper.GetTrackName(tr)
  local fx_chain = {}
  local fx_count = reaper.TrackFX_GetCount(tr)

  for fi = 0, fx_count - 1 do
    local _, fx_name = reaper.TrackFX_GetFXName(tr, fi)
    local params = {}
    local param_count = reaper.TrackFX_GetNumParams(tr, fi)
    for pi = 0, param_count - 1 do
      local _, pname = reaper.TrackFX_GetParamName(tr, fi, pi)
      local val = reaper.TrackFX_GetParamNormalized(tr, fi, pi)
      local _, display = reaper.TrackFX_GetFormattedParamValue(tr, fi, pi)
      params[#params + 1] = { index = pi, name = pname, value = val, display = display }
    end
    fx_chain[#fx_chain + 1] = {
      index = fi,
      name = fx_name,
      params = params
    }
  end

  return { status = "ok", track = actual_name, fx_chain = fx_chain }
end

local function op_apply_plan(cmd)
  local track_name = cmd.track
  local plan = cmd.plan
  if not track_name then return { status = "error", errors = {"Missing track name"} } end
  if not plan or not plan.steps then return { status = "error", errors = {"Missing plan or steps"} } end

  local tr, _ = find_track_by_name(track_name)
  if not tr then return { status = "error", errors = {"Track not found: " .. track_name} } end

  local _, actual_name = reaper.GetTrackName(tr)
  local title = plan.title or "AI FX Plan"
  local errors = {}
  local applied = 0

  reaper.Undo_BeginBlock()

  -- Track how many FX existed before, to compute indices for newly added FX
  local base_fx_count = reaper.TrackFX_GetCount(tr)
  local added_fx_offset = 0  -- how many FX we've added so far

  for _, step in ipairs(plan.steps) do
    if step.action == "add_fx" then
      local fx_name = step.fx_name
      if not fx_name then
        errors[#errors + 1] = "add_fx step missing fx_name"
      else
        local idx = reaper.TrackFX_AddByName(tr, fx_name, false, -1)
        if idx < 0 then
          errors[#errors + 1] = "Failed to add FX: " .. fx_name
        else
          applied = applied + 1
          added_fx_offset = added_fx_offset + 1
        end
      end

    elseif step.action == "set_param" then
      local fx_index = step.fx_index
      if fx_index == nil then
        errors[#errors + 1] = "set_param step missing fx_index"
      elseif step.params and #step.params > 0 then
        -- Array format: {"action":"set_param","fx_index":0,"params":[{"name":"Gain","value":0.5}]}
        local real_fx_idx = base_fx_count + fx_index
        local fx_count = reaper.TrackFX_GetCount(tr)
        if real_fx_idx >= fx_count then
          errors[#errors + 1] = "FX index " .. fx_index .. " (real: " .. real_fx_idx .. ") out of range"
        else
          for _, p in ipairs(step.params) do
            if not p.name or p.value == nil then
              errors[#errors + 1] = "set_param param entry missing name or value"
            else
              local pi = find_param_index(tr, real_fx_idx, p.name)
              if not pi then
                errors[#errors + 1] = "Param not found: " .. p.name .. " on FX " .. fx_index
              else
                reaper.TrackFX_SetParamNormalized(tr, real_fx_idx, pi, p.value)
                applied = applied + 1
              end
            end
          end
        end
      elseif step.param_name and step.value ~= nil then
        -- Flat format: {"action":"set_param","fx_index":0,"param_name":"Gain","value":0.5}
        local real_fx_idx = base_fx_count + fx_index
        local fx_count = reaper.TrackFX_GetCount(tr)
        if real_fx_idx >= fx_count then
          errors[#errors + 1] = "FX index " .. fx_index .. " (real: " .. real_fx_idx .. ") out of range"
        else
          local pi = find_param_index(tr, real_fx_idx, step.param_name)
          if not pi then
            errors[#errors + 1] = "Param not found: " .. step.param_name .. " on FX " .. fx_index
          else
            reaper.TrackFX_SetParamNormalized(tr, real_fx_idx, pi, step.value)
            applied = applied + 1
          end
        end
      else
        errors[#errors + 1] = "set_param step missing params array or param_name/value"
      end

    elseif step.action == "remove_fx" then
      local fx_index = step.fx_index
      if fx_index == nil then
        errors[#errors + 1] = "remove_fx step missing fx_index"
      else
        local real_fx_idx = base_fx_count + fx_index
        local fx_count = reaper.TrackFX_GetCount(tr)
        if real_fx_idx >= fx_count then
          errors[#errors + 1] = "FX index out of range for remove: " .. fx_index
        else
          reaper.TrackFX_Delete(tr, real_fx_idx)
          applied = applied + 1
        end
      end

    else
      errors[#errors + 1] = "Unknown action: " .. tostring(step.action)
    end
  end

  reaper.Undo_EndBlock(title, -1)

  local status = #errors > 0 and "partial" or "ok"
  if applied == 0 and #errors > 0 then status = "error" end

  return { status = status, applied = applied, errors = errors, track = actual_name }
end

local function op_remove_fx(cmd)
  local track_name = cmd.track
  local fx_index = cmd.fx_index
  if not track_name then return { status = "error", errors = {"Missing track name"} } end
  if fx_index == nil then return { status = "error", errors = {"Missing fx_index"} } end

  local tr, _ = find_track_by_name(track_name)
  if not tr then return { status = "error", errors = {"Track not found: " .. track_name} } end

  local _, actual_name = reaper.GetTrackName(tr)
  local fx_count = reaper.TrackFX_GetCount(tr)
  if fx_index >= fx_count then
    return { status = "error", errors = {"FX index out of range"}, track = actual_name }
  end

  reaper.Undo_BeginBlock()
  reaper.TrackFX_Delete(tr, fx_index)
  reaper.Undo_EndBlock("Remove FX " .. fx_index, -1)

  return { status = "ok", applied = 1, errors = {}, track = actual_name }
end

local function op_create_track(cmd)
  local name = cmd.name
  if not name then return { status = "error", errors = {"Missing track name"} } end

  reaper.Undo_BeginBlock()
  local idx = reaper.CountTracks(0)
  reaper.InsertTrackAtIndex(idx, true)
  local tr = reaper.GetTrack(0, idx)
  reaper.GetSetMediaTrackInfo_String(tr, "P_NAME", name, true)
  reaper.Undo_EndBlock("Create track: " .. name, -1)

  return { status = "ok", track = name, index = idx }
end

local function op_duplicate_track(cmd)
  local track_name = cmd.track
  local new_name = cmd.new_name
  if not track_name then return { status = "error", errors = {"Missing track name"} } end

  local tr, _ = find_track_by_name(track_name)
  if not tr then return { status = "error", errors = {"Track not found: " .. track_name} } end

  local _, actual_name = reaper.GetTrackName(tr)

  reaper.Undo_BeginBlock()

  -- Select only the source track, then duplicate via action
  local count = reaper.CountTracks(0)
  for i = 0, count - 1 do
    reaper.SetTrackSelected(reaper.GetTrack(0, i), false)
  end
  reaper.SetTrackSelected(tr, true)

  -- Action 40062 = "Track: Duplicate tracks"
  reaper.Main_OnCommand(40062, 0)

  -- The duplicate is inserted right after the original
  local new_count = reaper.CountTracks(0)
  if new_count > count then
    local new_tr = reaper.GetTrack(0, new_count - 1)
    if new_name then
      reaper.GetSetMediaTrackInfo_String(new_tr, "P_NAME", new_name, true)
    end
    local _, final_name = reaper.GetTrackName(new_tr)
    reaper.Undo_EndBlock("Duplicate track: " .. actual_name, -1)
    return { status = "ok", source = actual_name, track = final_name, index = new_count - 1 }
  end

  reaper.Undo_EndBlock("Duplicate track (failed)", -1)
  return { status = "error", errors = {"Duplicate action did not create a new track"} }
end

local function op_list_presets(cmd)
  local track_name = cmd.track
  local fx_index = cmd.fx_index
  if not track_name then return { status = "error", errors = {"Missing track name"} } end
  if fx_index == nil then return { status = "error", errors = {"Missing fx_index"} } end

  local tr, _ = find_track_by_name(track_name)
  if not tr then return { status = "error", errors = {"Track not found: " .. track_name} } end

  local _, actual_name = reaper.GetTrackName(tr)
  local fx_count = reaper.TrackFX_GetCount(tr)
  if fx_index >= fx_count then
    return { status = "error", errors = {"FX index out of range"}, track = actual_name }
  end

  local _, fx_name = reaper.TrackFX_GetFXName(tr, fx_index)
  local _, cur_preset = reaper.TrackFX_GetPreset(tr, fx_index)
  local cur_idx, num_presets = reaper.TrackFX_GetPresetIndex(tr, fx_index)

  -- Save all parameter values before enumerating (cycling presets changes them)
  local saved_params = {}
  local param_count = reaper.TrackFX_GetNumParams(tr, fx_index)
  for pi = 0, param_count - 1 do
    saved_params[pi] = reaper.TrackFX_GetParamNormalized(tr, fx_index, pi)
  end

  local presets = {}
  for i = 0, num_presets - 1 do
    reaper.TrackFX_SetPresetByIndex(tr, fx_index, i)
    local _, pname = reaper.TrackFX_GetPreset(tr, fx_index)
    presets[#presets + 1] = { index = i, name = pname }
  end

  -- Restore: set original preset if one was active, then restore exact param values
  if cur_idx >= 0 and cur_idx < num_presets then
    reaper.TrackFX_SetPresetByIndex(tr, fx_index, cur_idx)
  end
  for pi = 0, param_count - 1 do
    reaper.TrackFX_SetParamNormalized(tr, fx_index, pi, saved_params[pi])
  end

  return { status = "ok", track = actual_name, fx_name = fx_name, current_preset = cur_preset, presets = presets }
end

local function op_set_param(cmd)
  local track_name = cmd.track
  local fx_index = cmd.fx_index
  local params = cmd.params  -- array of {name, value}
  if not track_name then return { status = "error", errors = {"Missing track name"} } end
  if fx_index == nil then return { status = "error", errors = {"Missing fx_index"} } end
  if not params or #params == 0 then return { status = "error", errors = {"Missing params"} } end

  local tr, _ = find_track_by_name(track_name)
  if not tr then return { status = "error", errors = {"Track not found: " .. track_name} } end

  local _, actual_name = reaper.GetTrackName(tr)
  local fx_count = reaper.TrackFX_GetCount(tr)
  if fx_index >= fx_count then
    return { status = "error", errors = {"FX index out of range"}, track = actual_name }
  end

  reaper.Undo_BeginBlock()
  local applied = 0
  local errors = {}
  local confirmed = {}
  for _, p in ipairs(params) do
    local pi = find_param_index(tr, fx_index, p.name)
    if not pi then
      errors[#errors + 1] = "Param not found: " .. tostring(p.name)
    else
      reaper.TrackFX_SetParamNormalized(tr, fx_index, pi, p.value)
      local _, disp = reaper.TrackFX_GetFormattedParamValue(tr, fx_index, pi)
      applied = applied + 1
      confirmed[#confirmed + 1] = { name = p.name, value = p.value, display = disp }
    end
  end
  reaper.Undo_EndBlock("Set params on FX " .. fx_index, -1)

  local status = #errors > 0 and (applied > 0 and "partial" or "error") or "ok"
  return { status = status, applied = applied, errors = errors, track = actual_name, confirmed = confirmed }
end

local function op_set_preset(cmd)
  local track_name = cmd.track
  local fx_index = cmd.fx_index
  local preset_name = cmd.preset_name
  local preset_index = cmd.preset_index
  if not track_name then return { status = "error", errors = {"Missing track name"} } end
  if fx_index == nil then return { status = "error", errors = {"Missing fx_index"} } end
  if not preset_name and preset_index == nil then
    return { status = "error", errors = {"Missing preset_name or preset_index"} }
  end

  local tr, _ = find_track_by_name(track_name)
  if not tr then return { status = "error", errors = {"Track not found: " .. track_name} } end

  local _, actual_name = reaper.GetTrackName(tr)
  local fx_count = reaper.TrackFX_GetCount(tr)
  if fx_index >= fx_count then
    return { status = "error", errors = {"FX index out of range"}, track = actual_name }
  end

  local success
  if preset_index ~= nil then
    success = reaper.TrackFX_SetPresetByIndex(tr, fx_index, preset_index)
  else
    success = reaper.TrackFX_SetPreset(tr, fx_index, preset_name)
  end

  if not success then
    return { status = "error", errors = {"Failed to set preset"}, track = actual_name }
  end

  local _, applied_preset = reaper.TrackFX_GetPreset(tr, fx_index)
  return { status = "ok", track = actual_name, preset = applied_preset }
end

-- ---------------------------------------------------------------------------
-- Envelope helpers and operations
-- ---------------------------------------------------------------------------

local ENV_CHUNK_TAGS = {
  Volume = "VOLENV2",
  Pan    = "PANENV2",
  Mute   = "MUTEENV",
  Width  = "WIDTHENV2",
}

local MASTER_ENV_CHUNK_TAGS = {
  Volume = "MASTERVOLENV2",
  Pan    = "MASTERPANENV2",
  Mute   = "MASTERMUTEENV",
  Width  = "MASTERWIDTHENV2",
}

local function ensure_envelope_visible(track, env_name, is_master)
  local tags = is_master and MASTER_ENV_CHUNK_TAGS or ENV_CHUNK_TAGS
  local tag = tags[env_name]
  if not tag then return false end

  local _, chunk = reaper.GetTrackStateChunk(track, "", false)
  if not chunk then return false end

  -- Already has this envelope block — assume it's visible enough
  if chunk:find("<" .. tag .. "\n") or chunk:find("<" .. tag .. "\r") then
    return false
  end

  -- Inject a minimal envelope block before the final ">"
  local guid = reaper.genGuid("")
  local env_block = "<" .. tag .. "\n"
    .. "EGUID " .. guid .. "\n"
    .. "ACT 1 -1\n"
    .. "VIS 1 1 1.0\n"
    .. "LANEHEIGHT 0 0\n"
    .. "ARM 0\n"
    .. "DEFSHAPE 0 -1 -1\n"
    .. ">\n"

  -- Insert before the final ">" of the track chunk
  local last_close = chunk:match(".*()>")
  if not last_close then return false end
  local new_chunk = chunk:sub(1, last_close - 1) .. env_block .. chunk:sub(last_close)

  reaper.SetTrackStateChunk(track, new_chunk, false)
  return true
end

local function resolve_envelope(track, env_name, is_master)
  local env = reaper.GetTrackEnvelopeByName(track, env_name)
  if env then return env, nil end

  -- Try to auto-show the envelope
  if ensure_envelope_visible(track, env_name, is_master) then
    env = reaper.GetTrackEnvelopeByName(track, env_name)
    if env then return env, nil end
  end

  return nil, env_name .. " envelope not supported (use Volume, Pan, Mute, or Width)"
end

local function op_get_envelope(cmd)
  local track_name = cmd.track
  local env_name = cmd.envelope
  if not track_name then return { status = "error", errors = {"Missing track name"} } end
  if not env_name then return { status = "error", errors = {"Missing envelope name"} } end

  local tr, tr_idx = find_track_by_name(track_name)
  if not tr then return { status = "error", errors = {"Track not found: " .. track_name} } end

  local _, actual_name = reaper.GetTrackName(tr)
  local env, err = resolve_envelope(tr, env_name, tr_idx == -1)
  if not env then return { status = "error", errors = {err}, track = actual_name } end

  -- Read points from chunk for consistent scaling with write
  local _, chunk = reaper.GetEnvelopeStateChunk(env, "", false)
  local points = {}
  local idx = 0
  for line in chunk:gmatch("[^\r\n]+") do
    local time, value, shape = line:match("^PT ([%d%.%-e]+) ([%d%.%-e]+) (%d+)")
    if time then
      points[#points + 1] = {
        index = idx,
        time = tonumber(time),
        value = tonumber(value),
        shape = tonumber(shape),
        tension = 0
      }
      idx = idx + 1
    end
  end

  return { status = "ok", track = actual_name, envelope = env_name, points = points }
end

local function op_set_envelope_points(cmd)
  local track_name = cmd.track
  local env_name = cmd.envelope
  local pts = cmd.points
  local clear_first = cmd.clear_first
  if not track_name then return { status = "error", errors = {"Missing track name"} } end
  if not env_name then return { status = "error", errors = {"Missing envelope name"} } end
  if not pts or #pts == 0 then return { status = "error", errors = {"Missing points"} } end

  local tr, tr_idx = find_track_by_name(track_name)
  if not tr then return { status = "error", errors = {"Track not found: " .. track_name} } end

  local _, actual_name = reaper.GetTrackName(tr)
  local env, err = resolve_envelope(tr, env_name, tr_idx == -1)
  if not env then return { status = "error", errors = {err}, track = actual_name } end

  reaper.Undo_BeginBlock()

  -- Read current envelope chunk to preserve header settings
  local _, chunk = reaper.GetEnvelopeStateChunk(env, "", false)

  -- Build new point lines
  local pt_lines = {}
  for _, p in ipairs(pts) do
    local shape = p.shape or 0
    local tension = p.tension or 0
    pt_lines[#pt_lines + 1] = string.format("PT %.14g %.14g %d", p.time, p.value, shape)
  end

  -- Strip existing PT lines from chunk, keep everything else
  local header_lines = {}
  for line in chunk:gmatch("[^\r\n]+") do
    if not line:match("^PT ") then
      header_lines[#header_lines + 1] = line
    end
  end

  -- Remove the closing ">" so we can append points before it
  if header_lines[#header_lines] == ">" then
    table.remove(header_lines)
  end

  -- If not clearing, keep existing PT lines from chunk
  if not clear_first then
    for line in chunk:gmatch("[^\r\n]+") do
      if line:match("^PT ") then
        pt_lines[#pt_lines + 1] = line
      end
    end
  end

  -- Sort point lines by time
  table.sort(pt_lines, function(a, b)
    local ta = tonumber(a:match("^PT ([%d%.%-e]+)"))
    local tb = tonumber(b:match("^PT ([%d%.%-e]+)"))
    return (ta or 0) < (tb or 0)
  end)

  -- Rebuild chunk
  local new_chunk = table.concat(header_lines, "\n") .. "\n"
  for _, pl in ipairs(pt_lines) do
    new_chunk = new_chunk .. pl .. "\n"
  end
  new_chunk = new_chunk .. ">\n"

  reaper.SetEnvelopeStateChunk(env, new_chunk, false)

  reaper.Undo_EndBlock("Set envelope points: " .. env_name, -1)
  reaper.UpdateArrange()
  reaper.TrackList_AdjustWindows(false)

  return { status = "ok", track = actual_name, envelope = env_name, points_added = #pts }
end

local function op_clear_envelope(cmd)
  local track_name = cmd.track
  local env_name = cmd.envelope
  if not track_name then return { status = "error", errors = {"Missing track name"} } end
  if not env_name then return { status = "error", errors = {"Missing envelope name"} } end

  local tr, tr_idx = find_track_by_name(track_name)
  if not tr then return { status = "error", errors = {"Track not found: " .. track_name} } end

  local _, actual_name = reaper.GetTrackName(tr)
  local env, err = resolve_envelope(tr, env_name, tr_idx == -1)
  if not env then return { status = "error", errors = {err}, track = actual_name } end

  local count = reaper.CountEnvelopePoints(env)

  reaper.Undo_BeginBlock()
  reaper.DeleteEnvelopePointRange(env, -1, reaper.GetProjectLength(0) + 1000)
  reaper.Undo_EndBlock("Clear envelope: " .. env_name, -1)
  reaper.UpdateArrange()
  reaper.TrackList_AdjustWindows(false)

  return { status = "ok", track = actual_name, envelope = env_name, points_removed = count }
end

-- ---------------------------------------------------------------------------
-- Internal helpers (no undo block) for composability
-- ---------------------------------------------------------------------------

local function _create_send(src_tr, dst_tr, send_type, midi_channel, audio_volume)
  local send_idx = reaper.CreateTrackSend(src_tr, dst_tr)
  if send_idx < 0 then
    return nil, "Failed to create send"
  end

  send_type = send_type or "both"
  if send_type == "midi" then
    -- No audio: set source channel to -1 (None)
    reaper.SetTrackSendInfo_Value(src_tr, 0, send_idx, "I_SRCCHAN", -1)
    -- All MIDI channels
    local midi_flags = 0
    if midi_channel and midi_channel >= 0 and midi_channel <= 15 then
      -- Specific channel: low 5 bits = channel + 1
      midi_flags = midi_channel + 1
    end
    reaper.SetTrackSendInfo_Value(src_tr, 0, send_idx, "I_MIDIFLAGS", midi_flags)
  elseif send_type == "audio" then
    -- No MIDI: set MIDI flags to disable
    reaper.SetTrackSendInfo_Value(src_tr, 0, send_idx, "I_MIDIFLAGS", 31)
    if audio_volume then
      reaper.SetTrackSendInfo_Value(src_tr, 0, send_idx, "D_VOL", audio_volume)
    end
  else
    -- Both audio and MIDI
    if audio_volume then
      reaper.SetTrackSendInfo_Value(src_tr, 0, send_idx, "D_VOL", audio_volume)
    end
  end

  -- Round-trip verify
  local actual_dest = reaper.GetTrackSendInfo_Value(src_tr, 0, send_idx, "P_DESTTRACK")
  if not actual_dest then
    return nil, "Send created but verification failed"
  end

  return send_idx, nil
end

local function _load_rs5k(tr, sample_path, note, attack_ms, decay_ms, sustain, release_ms, volume_db, fx_index)
  -- Validate sample file
  if not file_exists(sample_path) then
    return nil, "Sample file not found: " .. sample_path
  end

  local fx_idx
  if fx_index then
    -- Verify existing FX is RS5k
    local fx_count = reaper.TrackFX_GetCount(tr)
    if fx_index >= fx_count then
      return nil, "FX index " .. fx_index .. " out of range"
    end
    local _, fx_name = reaper.TrackFX_GetFXName(tr, fx_index)
    if not (fx_name:find("RS5K") or fx_name:find("ReaSamplOmatic5000") or fx_name:find("reasamplomatic")) then
      return nil, "FX at index " .. fx_index .. " is not RS5k: " .. fx_name
    end
    fx_idx = fx_index
  else
    fx_idx = reaper.TrackFX_AddByName(tr, "ReaSamplOmatic5000", false, -1)
    if fx_idx < 0 then
      return nil, "Failed to add RS5k"
    end
  end

  -- Load sample via named config parm
  reaper.TrackFX_SetNamedConfigParm(tr, fx_idx, "FILE0", sample_path)
  reaper.TrackFX_SetNamedConfigParm(tr, fx_idx, "DONE", "")

  -- RS5k defaults to "Sample" mode which works for drum triggering
  -- (responds to MIDI note-on, plays sample at original pitch)

  -- Verify sample loaded
  local _, loaded_path = reaper.TrackFX_GetNamedConfigParm(tr, fx_idx, "FILE0")
  if not loaded_path or loaded_path == "" then
    return fx_idx, "RS5k added but sample may not have loaded"
  end

  -- Set note range (both start and end to the same note for single-note trigger)
  note = note or 60
  local note_norm = midi_note_to_norm(note)
  local pi_start = find_param_index(tr, fx_idx, "Note range start")
  local pi_end = find_param_index(tr, fx_idx, "Note range end")
  if pi_start then reaper.TrackFX_SetParamNormalized(tr, fx_idx, pi_start, note_norm) end
  if pi_end then reaper.TrackFX_SetParamNormalized(tr, fx_idx, pi_end, note_norm) end

  -- Set ADSR if provided (these are normalized 0-1; caller responsible for conversion)
  local param_map = {
    {"Attack", attack_ms},
    {"Decay", decay_ms},
    {"Sustain", sustain},
    {"Release", release_ms},
  }
  local errors = {}
  for _, pm in ipairs(param_map) do
    if pm[2] then
      local pi = find_param_index(tr, fx_idx, pm[1])
      if pi then
        reaper.TrackFX_SetParamNormalized(tr, fx_idx, pi, pm[2])
      else
        errors[#errors + 1] = "Param not found: " .. pm[1]
      end
    end
  end

  -- Volume/gain
  if volume_db then
    local pi = find_param_index(tr, fx_idx, "Volume")
    if pi then
      -- RS5k volume: normalize dB. Typically range is roughly -60 to +12 or similar.
      -- We pass through as normalized 0-1 for now.
      reaper.TrackFX_SetParamNormalized(tr, fx_idx, pi, volume_db)
    end
  end

  -- Enable "Obey note-offs" by default for clean triggering
  local pi_noteoff = find_param_index(tr, fx_idx, "Obey note-off")
  if pi_noteoff then
    reaper.TrackFX_SetParamNormalized(tr, fx_idx, pi_noteoff, 1.0)
  end

  return fx_idx, (#errors > 0) and table.concat(errors, "; ") or nil
end

local function _setup_reagate(tr, threshold_db, attack_ms, hold_ms, release_ms, midi_note, midi_channel, fx_index)
  local fx_idx
  if fx_index then
    local fx_count = reaper.TrackFX_GetCount(tr)
    if fx_index >= fx_count then
      return nil, "FX index " .. fx_index .. " out of range"
    end
    fx_idx = fx_index
  else
    fx_idx = reaper.TrackFX_AddByName(tr, "ReaGate", false, -1)
    if fx_idx < 0 then
      return nil, "Failed to add ReaGate"
    end
  end

  local errors = {}

  -- Enable MIDI output: "Send MIDI" is a checkbox (0=off, 1=on)
  local pi_midi = find_param_index(tr, fx_idx, "Send MIDI")
  if pi_midi then
    reaper.TrackFX_SetParamNormalized(tr, fx_idx, pi_midi, 1.0)
  else
    errors[#errors + 1] = "Could not find MIDI output param on ReaGate"
  end

  -- Set MIDI note for gate trigger
  if midi_note then
    local pi_note = find_param_index(tr, fx_idx, "MIDI note")
    if not pi_note then pi_note = find_param_index(tr, fx_idx, "Note") end
    if pi_note then
      reaper.TrackFX_SetParamNormalized(tr, fx_idx, pi_note, midi_note_to_norm(midi_note))
    else
      errors[#errors + 1] = "Could not find MIDI note param on ReaGate"
    end
  end

  -- Set MIDI channel
  if midi_channel then
    local pi_ch = find_param_index(tr, fx_idx, "MIDI channel")
    if pi_ch then
      reaper.TrackFX_SetParamNormalized(tr, fx_idx, pi_ch, midi_channel / 16.0)
    end
  end

  -- Threshold (normalized 0-1 for now; user can discover exact mapping)
  if threshold_db then
    local pi_thresh = find_param_index(tr, fx_idx, "Threshold")
    if pi_thresh then
      reaper.TrackFX_SetParamNormalized(tr, fx_idx, pi_thresh, threshold_db)
    else
      errors[#errors + 1] = "Could not find Threshold param"
    end
  end

  -- Attack
  if attack_ms then
    local pi = find_param_index(tr, fx_idx, "Attack")
    if pi then reaper.TrackFX_SetParamNormalized(tr, fx_idx, pi, attack_ms) end
  end

  -- Hold
  if hold_ms then
    local pi = find_param_index(tr, fx_idx, "Hold")
    if pi then reaper.TrackFX_SetParamNormalized(tr, fx_idx, pi, hold_ms) end
  end

  -- Release
  if release_ms then
    local pi = find_param_index(tr, fx_idx, "Release")
    if pi then reaper.TrackFX_SetParamNormalized(tr, fx_idx, pi, release_ms) end
  end

  -- Default Dry=1.0, Wet=0.0 so original audio passes through unaffected
  local pi_dry = find_param_index(tr, fx_idx, "Dry")
  if pi_dry then reaper.TrackFX_SetParamNormalized(tr, fx_idx, pi_dry, 1.0) end
  local pi_wet = find_param_index(tr, fx_idx, "Wet")
  if pi_wet then reaper.TrackFX_SetParamNormalized(tr, fx_idx, pi_wet, 0.0) end

  return fx_idx, (#errors > 0) and table.concat(errors, "; ") or nil
end

-- ---------------------------------------------------------------------------
-- New operations: sends, RS5k, ReaGate, folders, visibility, drum_augment
-- ---------------------------------------------------------------------------

local function op_add_send(cmd)
  local src_name = cmd.src_track
  local dst_name = cmd.dest_track
  if not src_name then return { status = "error", errors = {"Missing src_track"} } end
  if not dst_name then return { status = "error", errors = {"Missing dest_track"} } end

  local src_tr, _ = resolve_track(src_name)
  if not src_tr then return { status = "error", errors = {"Source track not found: " .. tostring(src_name)} } end
  local dst_tr, _ = resolve_track(dst_name)
  if not dst_tr then return { status = "error", errors = {"Dest track not found: " .. tostring(dst_name)} } end

  reaper.Undo_BeginBlock()
  local send_idx, err = _create_send(src_tr, dst_tr, cmd.send_type, cmd.midi_channel, cmd.audio_volume)
  reaper.Undo_EndBlock("Add send", -1)

  if not send_idx then
    return { status = "error", errors = {err} }
  end
  return { status = "ok", send_index = send_idx }
end

local function op_get_sends(cmd)
  local track_name = cmd.track
  if not track_name then return { status = "error", errors = {"Missing track"} } end

  local tr, _ = resolve_track(track_name)
  if not tr then return { status = "error", errors = {"Track not found: " .. tostring(track_name)} } end

  local _, actual_name = reaper.GetTrackName(tr)
  local num_sends = reaper.GetTrackNumSends(tr, 0)  -- category 0 = sends
  local sends = {}

  for si = 0, num_sends - 1 do
    local dest_tr = reaper.GetTrackSendInfo_Value(tr, 0, si, "P_DESTTRACK")
    local dest_name = ""
    if dest_tr then
      local _, dn = reaper.GetTrackName(dest_tr)
      dest_name = dn
    end
    local src_chan = reaper.GetTrackSendInfo_Value(tr, 0, si, "I_SRCCHAN")
    local midi_flags = reaper.GetTrackSendInfo_Value(tr, 0, si, "I_MIDIFLAGS")
    local vol = reaper.GetTrackSendInfo_Value(tr, 0, si, "D_VOL")

    sends[#sends + 1] = {
      index = si,
      dest_track = dest_name,
      src_chan = src_chan,
      midi_flags = midi_flags,
      volume = vol,
    }
  end

  return { status = "ok", track = actual_name, sends = sends }
end

local function op_load_sample_rs5k(cmd)
  local track_name = cmd.track
  local sample_path = cmd.sample_path
  if not track_name then return { status = "error", errors = {"Missing track"} } end
  if not sample_path then return { status = "error", errors = {"Missing sample_path"} } end

  local tr, _ = resolve_track(track_name)
  if not tr then return { status = "error", errors = {"Track not found: " .. tostring(track_name)} } end

  local _, actual_name = reaper.GetTrackName(tr)

  reaper.Undo_BeginBlock()
  local fx_idx, err = _load_rs5k(
    tr, sample_path, cmd.note,
    cmd.attack, cmd.decay, cmd.sustain, cmd.release,
    cmd.volume, cmd.fx_index
  )
  reaper.Undo_EndBlock("Load RS5k sample", -1)

  if not fx_idx then
    return { status = "error", errors = {err}, track = actual_name }
  end
  local result = { status = "ok", track = actual_name, fx_index = fx_idx }
  if err then result.warnings = {err} end
  return result
end

local function op_setup_reagate_midi(cmd)
  local track_name = cmd.track
  if not track_name then return { status = "error", errors = {"Missing track"} } end

  local tr, _ = resolve_track(track_name)
  if not tr then return { status = "error", errors = {"Track not found: " .. tostring(track_name)} } end

  local _, actual_name = reaper.GetTrackName(tr)

  reaper.Undo_BeginBlock()
  local fx_idx, err = _setup_reagate(
    tr, cmd.threshold, cmd.attack, cmd.hold, cmd.release,
    cmd.midi_note, cmd.midi_channel, cmd.fx_index
  )
  reaper.Undo_EndBlock("Setup ReaGate MIDI", -1)

  if not fx_idx then
    return { status = "error", errors = {err}, track = actual_name }
  end
  local result = { status = "ok", track = actual_name, fx_index = fx_idx }
  if err then result.warnings = {err} end
  return result
end

local function op_set_track_folder(cmd)
  local track_name = cmd.track
  local depth = cmd.depth
  if not track_name then return { status = "error", errors = {"Missing track"} } end
  if depth == nil then return { status = "error", errors = {"Missing depth"} } end

  local tr, _ = resolve_track(track_name)
  if not tr then return { status = "error", errors = {"Track not found: " .. tostring(track_name)} } end

  local _, actual_name = reaper.GetTrackName(tr)

  reaper.Undo_BeginBlock()
  reaper.SetMediaTrackInfo_Value(tr, "I_FOLDERDEPTH", depth)
  reaper.Undo_EndBlock("Set folder depth: " .. actual_name, -1)

  return { status = "ok", track = actual_name, depth = depth }
end

local function op_set_track_visible(cmd)
  local track_name = cmd.track
  if not track_name then return { status = "error", errors = {"Missing track"} } end

  local tr, _ = resolve_track(track_name)
  if not tr then return { status = "error", errors = {"Track not found: " .. tostring(track_name)} } end

  local _, actual_name = reaper.GetTrackName(tr)
  local tcp = cmd.tcp
  local mixer = cmd.mixer

  reaper.Undo_BeginBlock()
  if tcp ~= nil then
    reaper.SetMediaTrackInfo_Value(tr, "B_SHOWINTCP", tcp and 1 or 0)
  end
  if mixer ~= nil then
    reaper.SetMediaTrackInfo_Value(tr, "B_SHOWINMIXER", mixer and 1 or 0)
  end
  reaper.TrackList_AdjustWindows(false)
  reaper.Undo_EndBlock("Set track visibility: " .. actual_name, -1)

  return { status = "ok", track = actual_name }
end

local function op_drum_augment(cmd)
  local audio_track = cmd.audio_track
  local sample_path = cmd.sample_path
  if not audio_track then return { status = "error", errors = {"Missing audio_track"} } end
  if not sample_path then return { status = "error", errors = {"Missing sample_path"} } end

  -- Validate sample file before doing anything
  if not file_exists(sample_path) then
    return { status = "error", errors = {"Sample file not found: " .. sample_path} }
  end

  -- Use strict matching to avoid ambiguity
  local src_tr, src_idx, find_err = find_track_strict(audio_track)
  if not src_tr then
    return { status = "error", errors = {find_err} }
  end

  local _, src_name = reaper.GetTrackName(src_tr)

  -- Determine MIDI note
  local midi_note = pick_midi_note(cmd.drum_type, cmd.note)

  reaper.Undo_BeginBlock()

  -- 1. Create RS5k track (insert after audio track)
  local new_idx = src_idx + 1
  reaper.InsertTrackAtIndex(new_idx, true)
  local rs5k_tr = reaper.GetTrack(0, new_idx)
  if not rs5k_tr then
    reaper.Undo_EndBlock("Drum augment (failed)", -1)
    return { status = "error", errors = {"Failed to create RS5k track"} }
  end
  local rs5k_name = src_name .. " [RS5k]"
  reaper.GetSetMediaTrackInfo_String(rs5k_tr, "P_NAME", rs5k_name, true)

  -- 2. Load RS5k with sample
  -- Default release ~250ms (normalized ~0.016 based on RS5k's param range)
  -- Release must be >= Decay or the sound cuts off prematurely
  local rs5k_release = cmd.release or 0.02
  local fx_idx, rs5k_err = _load_rs5k(
    rs5k_tr, sample_path, midi_note,
    cmd.attack, cmd.decay, cmd.sustain, rs5k_release,
    cmd.volume, nil
  )
  if not fx_idx then
    reaper.Undo_EndBlock("Drum augment (failed)", -1)
    return { status = "error", errors = {"RS5k setup failed: " .. tostring(rs5k_err)} }
  end

  -- 3. Setup ReaGate on audio track for MIDI triggering
  -- Re-resolve audio track since indices shifted after insert
  src_tr = reaper.GetTrack(0, src_idx)
  -- Default threshold ~-12.5dB (normalized ~0.35) — reasonable for most drum recordings
  local threshold = cmd.threshold or 0.35
  local gate_idx, gate_err = _setup_reagate(
    src_tr, threshold, cmd.gate_attack, cmd.gate_hold, cmd.gate_release,
    midi_note, nil, nil
  )

  -- 4. Create MIDI-only send from audio track -> RS5k track
  -- Re-resolve rs5k track (index may be new_idx)
  rs5k_tr = reaper.GetTrack(0, new_idx)
  local send_idx, send_err = _create_send(src_tr, rs5k_tr, "midi", -1, nil)

  -- 5. Optional folder organization
  if cmd.create_folder then
    -- Make audio track a folder parent
    reaper.SetMediaTrackInfo_Value(src_tr, "I_FOLDERDEPTH", 1)
    -- Make RS5k track the last child (close folder)
    reaper.SetMediaTrackInfo_Value(rs5k_tr, "I_FOLDERDEPTH", -1)
  end

  -- 6. Refresh UI
  reaper.TrackList_AdjustWindows(false)

  reaper.Undo_EndBlock("Drum augment: " .. src_name, -1)

  -- Build result
  local warnings = {}
  if rs5k_err then warnings[#warnings + 1] = "RS5k: " .. rs5k_err end
  if gate_err then warnings[#warnings + 1] = "ReaGate: " .. gate_err end
  if send_err then warnings[#warnings + 1] = "Send: " .. send_err end

  return {
    status = #warnings > 0 and "partial" or "ok",
    audio_track = src_name,
    rs5k_track = rs5k_name,
    rs5k_track_index = new_idx,
    midi_note = midi_note,
    reagate_fx_index = gate_idx,
    rs5k_fx_index = fx_idx,
    send_index = send_idx,
    warnings = warnings,
  }
end

-- ---------------------------------------------------------------------------
-- Command dispatcher
-- ---------------------------------------------------------------------------

local function dispatch(cmd)
  local op = cmd.op
  if op == "get_context" then
    local result = op_get_context()
    result.status = "ok"
    return result
  elseif op == "get_track_fx" then
    return op_get_track_fx(cmd)
  elseif op == "apply_plan" then
    return op_apply_plan(cmd)
  elseif op == "remove_fx" then
    return op_remove_fx(cmd)
  elseif op == "create_track" then
    return op_create_track(cmd)
  elseif op == "duplicate_track" then
    return op_duplicate_track(cmd)
  elseif op == "list_presets" then
    return op_list_presets(cmd)
  elseif op == "set_param" then
    return op_set_param(cmd)
  elseif op == "set_preset" then
    return op_set_preset(cmd)
  elseif op == "get_envelope" then
    return op_get_envelope(cmd)
  elseif op == "set_envelope_points" then
    return op_set_envelope_points(cmd)
  elseif op == "clear_envelope" then
    return op_clear_envelope(cmd)
  elseif op == "add_send" then
    return op_add_send(cmd)
  elseif op == "get_sends" then
    return op_get_sends(cmd)
  elseif op == "load_sample_rs5k" then
    return op_load_sample_rs5k(cmd)
  elseif op == "setup_reagate_midi" then
    return op_setup_reagate_midi(cmd)
  elseif op == "set_track_folder" then
    return op_set_track_folder(cmd)
  elseif op == "set_track_visible" then
    return op_set_track_visible(cmd)
  elseif op == "drum_augment" then
    return op_drum_augment(cmd)
  else
    return { status = "error", errors = {"Unknown operation: " .. tostring(op)} }
  end
end

-- ---------------------------------------------------------------------------
-- Main poll loop
-- ---------------------------------------------------------------------------

QUEUE_BASE = resolve_queue_path()
local IN_DIR = QUEUE_BASE .. "/in"
local OUT_DIR = QUEUE_BASE .. "/out"

-- Ensure directories exist (native REAPER API — no cmd.exe subprocess)
reaper.RecursiveCreateDirectory(IN_DIR, 0)
reaper.RecursiveCreateDirectory(OUT_DIR, 0)

reaper.ShowConsoleMsg("[reaper-ai] Daemon started. Polling: " .. IN_DIR .. "\n")

local last_poll_time = 0

local function poll()
  -- Throttle: reaper.defer() fires every ~30ms, but we only scan every POLL_INTERVAL
  local now = reaper.time_precise()
  if now - last_poll_time >= POLL_INTERVAL then
    last_poll_time = now

    -- List JSON files in IN_DIR using native REAPER API (no subprocess)
    local idx = 0
    while true do
      local filename = reaper.EnumerateFiles(IN_DIR, idx)
      if not filename then break end
      if filename:match("%.json$") then
        local in_path = IN_DIR .. "/" .. filename
        local raw = read_file(in_path)
        if raw then
          local cmd = json_decode(raw)
          if cmd then
            local cmd_id = cmd.id or "unknown"
            reaper.ShowConsoleMsg("[reaper-ai] Processing: " .. cmd.op .. " (" .. cmd_id .. ")\n")

            local ok, result = pcall(dispatch, cmd)
            if not ok then
              result = { status = "error", errors = {"Lua error: " .. tostring(result)} }
            end
            result.id = cmd_id

            local out_path = OUT_DIR .. "/" .. cmd_id .. ".json"
            write_file(out_path, json_encode(result))
            delete_file(in_path)

            reaper.ShowConsoleMsg("[reaper-ai] Done: " .. result.status .. "\n")
          else
            reaper.ShowConsoleMsg("[reaper-ai] Failed to parse: " .. filename .. "\n")
            delete_file(in_path)
          end
        end
      end
      idx = idx + 1
    end
  end

  reaper.defer(poll)
end

poll()
