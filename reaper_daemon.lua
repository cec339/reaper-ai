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
          -- unicode escape — clamp to byte range, replace non-ASCII with '?'
          local hex = s:sub(i+1, i+4)
          local cp = tonumber(hex, 16) or 63
          if cp > 255 then cp = 63 end  -- '?' for non-Latin1 codepoints
          parts[#parts+1] = string.char(cp)
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

-- Unified track resolver: uses strict mode when cmd.strict is true,
-- otherwise falls back to fuzzy first-match (existing behavior).
local function resolve_track_from_cmd(cmd)
  local track = cmd.track
  if cmd.strict then
    local tr, idx, err = find_track_strict(track)
    if not tr then return nil, nil, err end
    return tr, idx, nil
  end
  local tr, idx = resolve_track(track)
  if not tr then return nil, nil, "Track not found: " .. tostring(track) end
  return tr, idx, nil
end

local function find_param_index(tr, fx_idx, param_name)
  local norm_target = normalize(param_name)
  local param_count = reaper.TrackFX_GetNumParams(tr, fx_idx)

  local function param_tokens(s)
    local toks = {}
    for tok in tostring(s):lower():gmatch("[a-z0-9]+") do
      toks[#toks + 1] = tok
    end
    return toks
  end

  local function token_subset(needle_tokens, hay_tokens)
    local counts = {}
    for _, tok in ipairs(hay_tokens) do
      counts[tok] = (counts[tok] or 0) + 1
    end
    for _, tok in ipairs(needle_tokens) do
      local c = counts[tok] or 0
      if c <= 0 then return false end
      counts[tok] = c - 1
    end
    return true
  end

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
  -- Reverse substring (helps when caller includes extra tokens)
  for pi = 0, param_count - 1 do
    local _, pname = reaper.TrackFX_GetParamName(tr, fx_idx, pi)
    local np = normalize(pname)
    if norm_target:find(np, 1, true) then return pi end
  end
  -- Token-subset fallback: handles variants like "Band (alt 2) 2".
  local target_tokens = param_tokens(param_name)
  for pi = 0, param_count - 1 do
    local _, pname = reaper.TrackFX_GetParamName(tr, fx_idx, pi)
    if token_subset(target_tokens, param_tokens(pname)) then return pi end
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
  local folder_stack = {} -- stack of open folder parent indices
  for i = 0, count - 1 do
    local tr = reaper.GetTrack(0, i)
    local _, tr_name = reaper.GetTrackName(tr)
    local selected = reaper.IsTrackSelected(tr)
    local muted = reaper.GetMediaTrackInfo_Value(tr, "B_MUTE") == 1
    local folder_depth = math.floor(reaper.GetMediaTrackInfo_Value(tr, "I_FOLDERDEPTH") or 0)
    local parent_index = (#folder_stack > 0) and folder_stack[#folder_stack] or -1
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
      muted = muted,
      folder_depth = folder_depth,
      parent_index = parent_index,
      is_folder = folder_depth > 0,
      fx = fx_list
    }

    -- Update folder stack after assigning this track's parent.
    if folder_depth > 0 then
      for _ = 1, folder_depth do
        folder_stack[#folder_stack + 1] = i
      end
    elseif folder_depth < 0 then
      for _ = 1, math.abs(folder_depth) do
        if #folder_stack > 0 then
          table.remove(folder_stack)
        end
      end
    end
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

  local tr, track_index, err = resolve_track_from_cmd(cmd)
  if not tr then return { status = "error", errors = {err} } end

  local _, actual_name = reaper.GetTrackName(tr)
  local fx_chain = {}
  local fx_count = reaper.TrackFX_GetCount(tr)

  for fi = 0, fx_count - 1 do
    local _, fx_name = reaper.TrackFX_GetFXName(tr, fi)
    local fx_enabled = reaper.TrackFX_GetEnabled(tr, fi)
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
      enabled = fx_enabled and true or false,
      params = params
    }
  end

  return { status = "ok", track = actual_name, index = track_index, fx_chain = fx_chain }
end

local function op_apply_plan(cmd)
  local track_name = cmd.track
  local plan = cmd.plan
  if not track_name then return { status = "error", errors = {"Missing track name"} } end
  if not plan or not plan.steps then return { status = "error", errors = {"Missing plan or steps"} } end

  local tr, _, resolve_err = resolve_track_from_cmd(cmd)
  if not tr then return { status = "error", errors = {resolve_err} } end

  local _, actual_name = reaper.GetTrackName(tr)
  local title = plan.title or "AI FX Plan"
  local errors = {}
  local applied = 0
  local added_fx_indices = {}

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
          added_fx_indices[#added_fx_indices + 1] = idx
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
        if real_fx_idx < 0 or real_fx_idx >= fx_count then
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
        if real_fx_idx < 0 or real_fx_idx >= fx_count then
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

    elseif step.action == "set_preset" then
      local fx_index = step.fx_index
      if fx_index == nil then
        errors[#errors + 1] = "set_preset step missing fx_index"
      else
        local real_fx_idx = base_fx_count + fx_index
        local fx_count = reaper.TrackFX_GetCount(tr)
        if real_fx_idx < 0 or real_fx_idx >= fx_count then
          errors[#errors + 1] = "FX index " .. fx_index .. " (real: " .. real_fx_idx .. ") out of range"
        else
          local ok = false
          if step.preset_index ~= nil then
            ok = reaper.TrackFX_SetPresetByIndex(tr, real_fx_idx, step.preset_index) and true or false
          elseif step.preset_name then
            ok = reaper.TrackFX_SetPreset(tr, real_fx_idx, step.preset_name) and true or false
          else
            errors[#errors + 1] = "set_preset step missing preset_name or preset_index"
          end
          if ok then
            applied = applied + 1
          elseif step.preset_index ~= nil or step.preset_name then
            errors[#errors + 1] = "Failed to set preset on FX " .. fx_index
          end
        end
      end

    elseif step.action == "remove_fx" then
      local fx_index = step.fx_index
      if fx_index == nil then
        errors[#errors + 1] = "remove_fx step missing fx_index"
      else
        local real_fx_idx = base_fx_count + fx_index
        local fx_count = reaper.TrackFX_GetCount(tr)
        if real_fx_idx < 0 or real_fx_idx >= fx_count then
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

  return { status = status, applied = applied, errors = errors, track = actual_name,
           added_fx_indices = added_fx_indices }
end

local function op_remove_fx(cmd)
  local track_name = cmd.track
  local fx_index = cmd.fx_index
  if not track_name then return { status = "error", errors = {"Missing track name"} } end
  if fx_index == nil then return { status = "error", errors = {"Missing fx_index"} } end
  if type(fx_index) ~= "number" then return { status = "error", errors = {"Invalid fx_index type"} } end
  fx_index = math.floor(fx_index)

  local tr, _, resolve_err = resolve_track_from_cmd(cmd)
  if not tr then return { status = "error", errors = {resolve_err} } end

  local _, actual_name = reaper.GetTrackName(tr)
  local fx_count = reaper.TrackFX_GetCount(tr)
  if fx_index < 0 or fx_index >= fx_count then
    return { status = "error", errors = {"FX index out of range"}, track = actual_name }
  end

  reaper.Undo_BeginBlock()
  reaper.TrackFX_Delete(tr, fx_index)
  reaper.Undo_EndBlock("Remove FX " .. fx_index, -1)

  return { status = "ok", applied = 1, errors = {}, track = actual_name }
end

local function op_reorder_fx(cmd)
  local track_name = cmd.track
  local order = cmd.order  -- array of current fx indices in desired order
  if not track_name then return { status = "error", errors = {"Missing track name"} } end
  if not order or type(order) ~= "table" then
    return { status = "error", errors = {"Missing or invalid 'order' array"} }
  end

  local tr, _, resolve_err = resolve_track_from_cmd(cmd)
  if not tr then return { status = "error", errors = {resolve_err} } end

  local _, actual_name = reaper.GetTrackName(tr)
  local fx_count = reaper.TrackFX_GetCount(tr)

  if #order ~= fx_count then
    return { status = "error", errors = {
      "order length (" .. #order .. ") != fx count (" .. fx_count .. ")"
    }, track = actual_name }
  end

  -- Validate all indices
  local seen = {}
  for i, idx in ipairs(order) do
    if type(idx) ~= "number" or idx < 0 or idx >= fx_count then
      return { status = "error", errors = {
        "Invalid FX index " .. tostring(idx) .. " at position " .. i
      }, track = actual_name }
    end
    if idx ~= math.floor(idx) then
      return { status = "error", errors = {
        "Non-integer FX index " .. tostring(idx) .. " at position " .. i
      }, track = actual_name }
    end
    if seen[idx] then
      return { status = "error", errors = {
        "Duplicate FX index " .. tostring(idx) .. " in order"
      }, track = actual_name }
    end
    seen[idx] = true
  end

  reaper.Undo_BeginBlock()
  reaper.PreventUIRefresh(1)

  -- Build the new order by moving FX one at a time to the correct slot.
  -- After placing slot i, the FX that was at order[i] is now at position i.
  -- We need to track where each original FX index currently sits.
  local cur_pos = {}  -- cur_pos[original_index] = current_position
  for i = 0, fx_count - 1 do cur_pos[i] = i end

  for target_slot = 0, fx_count - 1 do
    local wanted_orig = order[target_slot + 1]  -- Lua 1-based
    local from_slot = cur_pos[wanted_orig]
    if from_slot ~= target_slot then
      -- Move FX from from_slot to target_slot
      reaper.TrackFX_CopyToTrack(tr, from_slot, tr, target_slot, true)
      -- Update cur_pos: the FX that was at from_slot moved to target_slot.
      -- All FX between shifted by 1.
      if from_slot > target_slot then
        -- Shifted up: everything in [target_slot, from_slot-1] shifted +1
        for orig, pos in pairs(cur_pos) do
          if pos >= target_slot and pos < from_slot and orig ~= wanted_orig then
            cur_pos[orig] = pos + 1
          end
        end
      else
        -- Shifted down: everything in [from_slot+1, target_slot] shifted -1
        for orig, pos in pairs(cur_pos) do
          if pos > from_slot and pos <= target_slot and orig ~= wanted_orig then
            cur_pos[orig] = pos - 1
          end
        end
      end
      cur_pos[wanted_orig] = target_slot
    end
  end

  reaper.PreventUIRefresh(-1)
  reaper.Undo_EndBlock("Reorder FX chain", -1)

  -- Read back final order
  local final_chain = {}
  local new_count = reaper.TrackFX_GetCount(tr)
  for i = 0, new_count - 1 do
    local _, name = reaper.TrackFX_GetFXName(tr, i)
    final_chain[#final_chain + 1] = { index = i, name = name }
  end

  return { status = "ok", track = actual_name, fx_chain = final_chain }
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
  if track_name == nil then return { status = "error", errors = {"Missing track name"} } end
  if fx_index == nil then return { status = "error", errors = {"Missing fx_index"} } end
  if type(fx_index) ~= "number" then return { status = "error", errors = {"Invalid fx_index type"} } end
  fx_index = math.floor(fx_index)
  if not params or #params == 0 then return { status = "error", errors = {"Missing params"} } end

  -- Use resolve_track_from_cmd for strict index support
  local tr, _, resolve_err = resolve_track_from_cmd(cmd)
  if not tr then return { status = "error", errors = {resolve_err or "Track not found: " .. tostring(track_name)} } end

  local _, actual_name = reaper.GetTrackName(tr)
  local fx_count = reaper.TrackFX_GetCount(tr)
  if fx_index < 0 or fx_index >= fx_count then
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
      local actual_value = reaper.TrackFX_GetParamNormalized(tr, fx_index, pi)
      local _, disp = reaper.TrackFX_GetFormattedParamValue(tr, fx_index, pi)
      applied = applied + 1
      confirmed[#confirmed + 1] = {
        name = p.name,
        requested = p.value,
        value = actual_value,
        display = disp,
      }
    end
  end
  reaper.Undo_EndBlock("Set params on FX " .. fx_index, -1)

  local status = #errors > 0 and (applied > 0 and "partial" or "error") or "ok"
  return { status = status, applied = applied, errors = errors, track = actual_name, confirmed = confirmed }
end

-- ---------------------------------------------------------------------------
-- reorder_fx: rearrange the FX chain to a specified order
-- ---------------------------------------------------------------------------

local function op_reorder_fx(cmd)
  local track_name = cmd.track
  local order = cmd.order  -- array of current indices in desired order, e.g. [2,3,4,1,5,0]
  if not track_name then return { status = "error", errors = {"Missing track name"} } end
  if not order or #order == 0 then return { status = "error", errors = {"Missing order array"} } end

  local tr, _, resolve_err = resolve_track_from_cmd(cmd)
  if not tr then return { status = "error", errors = {resolve_err or "Track not found: " .. tostring(track_name)} } end

  local _, actual_name = reaper.GetTrackName(tr)
  local fx_count = reaper.TrackFX_GetCount(tr)

  if #order ~= fx_count then
    return { status = "error", errors = {"Order array length (" .. #order .. ") != FX count (" .. fx_count .. ")"}, track = actual_name }
  end

  -- Validate all indices
  local seen = {}
  for _, idx in ipairs(order) do
    if idx < 0 or idx >= fx_count then
      return { status = "error", errors = {"Invalid FX index in order: " .. idx}, track = actual_name }
    end
    if seen[idx] then
      return { status = "error", errors = {"Duplicate FX index in order: " .. idx}, track = actual_name }
    end
    seen[idx] = true
  end

  reaper.Undo_BeginBlock()
  -- Build a mapping from current positions, then move one at a time.
  -- After each move, indices shift, so we track current positions.
  local current = {}
  for i = 0, fx_count - 1 do current[i] = i end

  for target_pos = 0, #order - 1 do
    local wanted_original = order[target_pos + 1]  -- 1-based Lua array
    -- Find where that original FX currently is
    local current_pos = nil
    for pos, orig in pairs(current) do
      if orig == wanted_original then
        current_pos = pos
        break
      end
    end
    if current_pos and current_pos ~= target_pos then
      reaper.TrackFX_CopyToTrack(tr, current_pos, tr, target_pos, true)
      -- Update tracking: shift indices between target_pos and current_pos
      local new_current = {}
      for pos, orig in pairs(current) do
        if pos == current_pos then
          -- This one moved to target_pos
        elseif current_pos > target_pos then
          -- Moved backward: everything in [target_pos, current_pos-1] shifts +1
          if pos >= target_pos and pos < current_pos then
            new_current[pos + 1] = orig
          else
            new_current[pos] = orig
          end
        else
          -- Moved forward: everything in [current_pos+1, target_pos] shifts -1
          if pos > current_pos and pos <= target_pos then
            new_current[pos - 1] = orig
          else
            new_current[pos] = orig
          end
        end
      end
      new_current[target_pos] = wanted_original
      current = new_current
    end
  end
  reaper.Undo_EndBlock("Reorder FX chain", -1)

  -- Read back final order
  local final = {}
  local new_count = reaper.TrackFX_GetCount(tr)
  for i = 0, new_count - 1 do
    local _, name = reaper.TrackFX_GetFXName(tr, i)
    final[#final + 1] = { index = i, name = name }
  end

  return { status = "ok", track = actual_name, fx_chain = final }
end

-- ---------------------------------------------------------------------------
-- set_param_display: set params by target display value (e.g. "3.0" seconds)
-- Uses binary search with TrackFX_FormatParamValueNormalized to find the
-- right normalized value.  Falls back to sampled lookup for non-monotonic
-- or non-numeric params.
-- ---------------------------------------------------------------------------

local _display_solve_cache = {}  -- keyed by "fxname|paramidx" -> array of {norm, num}

local function parse_display_number(s)
  if not s then return nil end
  local n = tonumber(s:match("([%-%.%d]+)"))
  return n
end

local function format_param_at(tr, fx, pi, norm)
  local ok, buf = reaper.TrackFX_FormatParamValueNormalized(tr, fx, pi, norm, "", 256)
  if ok then return buf end
  return nil
end

local function build_sample_table(tr, fx, pi, steps)
  local samples = {}
  for i = 0, steps do
    local norm = i / steps
    local disp = format_param_at(tr, fx, pi, norm)
    local num = parse_display_number(disp)
    samples[#samples + 1] = { norm = norm, display = disp, num = num }
  end
  return samples
end

local function solve_display_binary(tr, fx, pi, target, ascending)
  local lo, hi = 0.0, 1.0
  if not ascending then lo, hi = 1.0, 0.0 end
  local best_norm = 0.5
  local best_err = math.huge
  for _ = 1, 32 do
    local mid = (lo + hi) / 2
    local disp = format_param_at(tr, fx, pi, mid)
    local num = parse_display_number(disp)
    if not num then return mid, disp end
    local err = math.abs(num - target)
    if err < best_err then
      best_err = err
      best_norm = mid
    end
    if err < 0.001 then break end
    if (ascending and num < target) or (not ascending and num > target) then
      lo = mid
    else
      hi = mid
    end
  end
  return best_norm, format_param_at(tr, fx, pi, best_norm)
end

local function solve_display_sampled(tr, fx, pi, target, samples)
  -- Find the two samples that bracket the target, then refine
  local best_idx = 1
  local best_err = math.huge
  for i, s in ipairs(samples) do
    if s.num then
      local err = math.abs(s.num - target)
      if err < best_err then
        best_err = err
        best_idx = i
      end
    end
  end
  -- Local refinement between neighboring samples
  local lo_idx = math.max(1, best_idx - 1)
  local hi_idx = math.min(#samples, best_idx + 1)
  local lo_norm = samples[lo_idx].norm
  local hi_norm = samples[hi_idx].norm
  local best_norm = samples[best_idx].norm
  for _ = 1, 24 do
    local mid = (lo_norm + hi_norm) / 2
    local disp = format_param_at(tr, fx, pi, mid)
    local num = parse_display_number(disp)
    if not num then break end
    local err = math.abs(num - target)
    if err < best_err then
      best_err = err
      best_norm = mid
    end
    if err < 0.001 then break end
    -- Try to narrow bracket
    local mid_lo = (lo_norm + mid) / 2
    local mid_hi = (mid + hi_norm) / 2
    local num_lo = parse_display_number(format_param_at(tr, fx, pi, mid_lo))
    local num_hi = parse_display_number(format_param_at(tr, fx, pi, mid_hi))
    if num_lo and math.abs(num_lo - target) < math.abs(num_hi - target) then
      hi_norm = mid
    else
      lo_norm = mid
    end
  end
  return best_norm, format_param_at(tr, fx, pi, best_norm)
end

local function solve_display_enum(tr, fx, pi, target_str, steps)
  -- Step through all values and find exact or closest text match
  local target_lower = tostring(target_str):lower()
  for i = 0, steps do
    local norm = i / steps
    local disp = format_param_at(tr, fx, pi, norm)
    if disp and disp:lower():find(target_lower, 1, true) then
      return norm, disp
    end
  end
  return nil, nil
end

local function solve_param_display(tr, fx, pi, target_display, fx_name)
  local target_num = tonumber(target_display)
  local cache_key = (fx_name or "") .. "|" .. tostring(pi)

  if target_num then
    -- Numeric target: probe monotonicity at 0, 0.5, 1
    local d0 = parse_display_number(format_param_at(tr, fx, pi, 0.0))
    local d5 = parse_display_number(format_param_at(tr, fx, pi, 0.5))
    local d1 = parse_display_number(format_param_at(tr, fx, pi, 1.0))

    if d0 and d5 and d1 then
      local ascending = (d0 <= d5 and d5 <= d1)
      local descending = (d0 >= d5 and d5 >= d1)
      if ascending or descending then
        -- Check target is within range
        local lo_val = math.min(d0, d1)
        local hi_val = math.max(d0, d1)
        if target_num < lo_val then target_num = lo_val end
        if target_num > hi_val then target_num = hi_val end
        return solve_display_binary(tr, fx, pi, target_num, ascending)
      end
    end
    -- Non-monotonic or couldn't probe: sampled fallback
    local samples = _display_solve_cache[cache_key]
    if not samples then
      samples = build_sample_table(tr, fx, pi, 100)
      _display_solve_cache[cache_key] = samples
    end
    return solve_display_sampled(tr, fx, pi, target_num, samples)
  else
    -- Text/enum target: step through possible values
    local norm, disp = solve_display_enum(tr, fx, pi, target_display, 200)
    if norm then return norm, disp end
    return nil, nil
  end
end

local function op_set_param_display(cmd)
  local track_name = cmd.track
  local fx_index = cmd.fx_index
  local params = cmd.params  -- array of {name, display_value}
  if track_name == nil then return { status = "error", errors = {"Missing track name"} } end
  if fx_index == nil then return { status = "error", errors = {"Missing fx_index"} } end
  if type(fx_index) ~= "number" then return { status = "error", errors = {"Invalid fx_index type"} } end
  fx_index = math.floor(fx_index)
  if not params or #params == 0 then return { status = "error", errors = {"Missing params"} } end

  local tr, _, resolve_err = resolve_track_from_cmd(cmd)
  if not tr then return { status = "error", errors = {resolve_err or "Track not found: " .. tostring(track_name)} } end

  local _, actual_name = reaper.GetTrackName(tr)
  local fx_count = reaper.TrackFX_GetCount(tr)
  if fx_index < 0 or fx_index >= fx_count then
    return { status = "error", errors = {"FX index out of range"}, track = actual_name }
  end

  local _, fx_name = reaper.TrackFX_GetFXName(tr, fx_index)

  reaper.Undo_BeginBlock()
  local applied = 0
  local errors = {}
  local confirmed = {}
  for _, p in ipairs(params) do
    local pi = find_param_index(tr, fx_index, p.name)
    if not pi then
      errors[#errors + 1] = "Param not found: " .. tostring(p.name)
    else
      local solved_norm, solved_disp = solve_param_display(tr, fx_index, pi, p.display_value, fx_name)
      if not solved_norm then
        errors[#errors + 1] = "Could not solve display value for: " .. tostring(p.name) .. " = " .. tostring(p.display_value)
      else
        reaper.TrackFX_SetParamNormalized(tr, fx_index, pi, solved_norm)
        local actual_norm = reaper.TrackFX_GetParamNormalized(tr, fx_index, pi)
        local _, actual_disp = reaper.TrackFX_GetFormattedParamValue(tr, fx_index, pi)
        applied = applied + 1
        confirmed[#confirmed + 1] = {
          name = p.name,
          requested_display = tostring(p.display_value),
          actual_display = actual_disp,
          normalized = actual_norm,
          error = math.abs(actual_norm - solved_norm),
        }
      end
    end
  end
  reaper.Undo_EndBlock("Set params (display) on FX " .. fx_index, -1)

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
  local env_name = cmd.envelope
  if not cmd.track then return { status = "error", errors = {"Missing track name"} } end
  if not env_name then return { status = "error", errors = {"Missing envelope name"} } end

  local tr, tr_idx, find_err = resolve_track_from_cmd(cmd)
  if not tr then return { status = "error", errors = {find_err or ("Track not found: " .. tostring(cmd.track))} } end

  local _, actual_name = reaper.GetTrackName(tr)

  -- peek mode: return empty points if envelope doesn't exist (no side effects)
  if cmd.peek then
    local env = reaper.GetTrackEnvelopeByName(tr, env_name)
    if not env then
      return { status = "ok", track = actual_name, envelope = env_name, points = {}, exists = false }
    end
  end

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

-- ---------------------------------------------------------------------------
-- Check if an FX has any parameter envelopes with points
-- ---------------------------------------------------------------------------
local function op_has_fx_envelopes(cmd)
  local tr, _, find_err = resolve_track_from_cmd(cmd)
  if not tr then return { status = "error", errors = {find_err or "Track not found"} } end
  local fx_index = cmd.fx_index
  if fx_index == nil then return { status = "error", errors = {"Missing fx_index"} } end
  local num_params = reaper.TrackFX_GetNumParams(tr, fx_index)
  if num_params == 0 then
    return { status = "ok", has_envelopes = false, point_count = 0 }
  end
  for pi = 0, num_params - 1 do
    local env = reaper.GetFXEnvelope(tr, fx_index, pi, false)
    if env then
      local num_pts = reaper.CountEnvelopePoints(env)
      if num_pts > 0 then
        return { status = "ok", has_envelopes = true, point_count = num_pts }
      end
    end
  end
  return { status = "ok", has_envelopes = false, point_count = 0 }
end

-- ---------------------------------------------------------------------------
-- Set FX parameter automation envelopes (batched: multiple params per call)
-- ---------------------------------------------------------------------------
local function op_set_fx_envelopes(cmd)
  local envelopes = cmd.envelopes
  if not envelopes or #envelopes == 0 then
    return { status = "error", errors = {"Missing envelopes array"} }
  end

  local tr, tr_idx, tr_err = resolve_track_from_cmd(cmd)
  if not tr then return { status = "error", errors = {tr_err} } end

  local _, actual_name = reaper.GetTrackName(tr)
  local fx_index = cmd.fx_index
  if fx_index == nil then
    return { status = "error", errors = {"Missing fx_index"}, track = actual_name }
  end
  local fx_count = reaper.TrackFX_GetCount(tr)
  if fx_index < 0 or fx_index >= fx_count then
    return { status = "error", errors = {"FX index out of range: " .. fx_index}, track = actual_name }
  end

  reaper.Undo_BeginBlock()

  local written = 0
  local total_points = 0
  local errors = {}

  for _, entry in ipairs(envelopes) do
    -- Resolve param index: prefer numeric param_index, fallback to name
    local pi = entry.param_index
    if pi == nil and entry.param_name then
      pi = find_param_index(tr, fx_index, entry.param_name)
    end
    if pi == nil then
      errors[#errors + 1] = "Param not resolved: " .. tostring(entry.param_name or entry.param_index)
      goto continue_env
    end

    local pts = entry.points
    if not pts or #pts == 0 then
      errors[#errors + 1] = "No points for param " .. tostring(entry.param_name or pi)
      goto continue_env
    end

    -- Get or create the FX parameter envelope
    local env = reaper.GetFXEnvelope(tr, fx_index, pi, true)
    if not env then
      errors[#errors + 1] = "Failed to get envelope for param " .. tostring(entry.param_name or pi)
      goto continue_env
    end

    -- Read PARMENV center from chunk to convert API-normalized values to
    -- envelope space. By default we assume API neutral is 0.5, but callers
    -- may provide entry.api_center for parameters whose neutral is elsewhere
    -- (e.g. Volume sliders where 0 dB is not at 0.5).
    local ok_pre, pre_chunk = reaper.GetEnvelopeStateChunk(env, "", false)
    if not ok_pre or not pre_chunk or pre_chunk == "" then
      errors[#errors + 1] = "Failed to read envelope chunk for param " .. tostring(entry.param_name or pi)
      goto continue_env
    end
    local center = tonumber(pre_chunk:match("<PARMENV [^ ]+ [^ ]+ [^ ]+ ([%d%.%-]+)"))
    if not center or center <= 0 or center >= 1 then
      center = 0.5  -- fallback: assume 1:1 mapping
    end

    local api_center = tonumber(entry.api_center)
    if not api_center or api_center <= 0 or api_center >= 1 then
      api_center = 0.5
    end

    -- Convert API-normalized value (api_center = neutral) to envelope value
    -- (center = neutral). Piecewise linear preserving endpoints and neutral.
    local function api_to_env(v)
      if v < 0 then v = 0 end
      if v > 1 then v = 1 end

      -- Fast path: identical neutral points => identity mapping.
      if math.abs(api_center - center) < 1e-9 then
        return v
      end

      if v <= api_center then
        return v * (center / api_center)
      else
        return center + (v - api_center) * ((1.0 - center) / (1.0 - api_center))
      end
    end

    -- Clear existing points if requested
    if entry.clear_first then
      reaper.DeleteEnvelopePointRange(env, -1, 1e18)
    elseif entry.clear_range_start ~= nil and entry.clear_range_end ~= nil then
      reaper.DeleteEnvelopePointRange(env, entry.clear_range_start, entry.clear_range_end)
    end

    -- Insert converted points
    for _, p in ipairs(pts) do
      local shape = p.shape or 0
      local tension = p.tension or 0
      local env_val = api_to_env(p.value)
      reaper.InsertEnvelopePoint(env, p.time, env_val, shape, tension, false, true)
    end
    reaper.Envelope_SortPoints(env)

    -- Re-read chunk AFTER points are inserted (avoids stale-chunk overwrite).
    -- Force ACT 1 -1 (active, track default) regardless of prior state —
    -- clears any leftover ACT 1 1 from earlier runs.
    local ok_post, post_chunk = reaper.GetEnvelopeStateChunk(env, "", false)
    if not ok_post or not post_chunk or post_chunk == "" then
      errors[#errors + 1] = "Failed to re-read envelope chunk for param " .. tostring(entry.param_name or pi)
      goto continue_env
    end
    if not post_chunk:match("ACT 1 %-1") then
      local updated, replaced = post_chunk:gsub("ACT %d[^\r\n]*", "ACT 1 -1", 1)
      if replaced == 0 then
        -- ACT line missing; insert before closing >
        updated, replaced = updated:gsub("\r\n>", "\r\nACT 1 -1\r\n>", 1)
        if replaced == 0 then
          updated, replaced = updated:gsub("\n>", "\nACT 1 -1\n>", 1)
        end
      end
      if replaced == 0 then
        errors[#errors + 1] = "Could not set ACT for param " .. tostring(entry.param_name or pi)
        goto continue_env
      end
      if not reaper.SetEnvelopeStateChunk(env, updated, false) then
        errors[#errors + 1] = "Failed to write envelope chunk for param " .. tostring(entry.param_name or pi)
        goto continue_env
      end
    end

    -- Readback verification: confirm envelope is active and point count
    local ok_rb, rb_chunk = reaper.GetEnvelopeStateChunk(env, "", false)
    if ok_rb and rb_chunk then
      if rb_chunk:match("ACT 0") then
        errors[#errors + 1] = "VERIFY FAIL: envelope inactive after write for param " .. tostring(entry.param_name or pi)
        goto continue_env
      end
      -- Count PT lines (only meaningful when we cleared first)
      if entry.clear_first then
        local pt_count = 0
        for _ in rb_chunk:gmatch("PT [^\r\n]+") do pt_count = pt_count + 1 end
        if pt_count ~= #pts then
          errors[#errors + 1] = "VERIFY FAIL: expected " .. #pts .. " points but found " .. pt_count .. " for param " .. tostring(entry.param_name or pi)
          goto continue_env
        end
      end
    else
      errors[#errors + 1] = "VERIFY FAIL: could not readback chunk for param " .. tostring(entry.param_name or pi)
      goto continue_env
    end

    written = written + 1
    total_points = total_points + #pts

    ::continue_env::
  end

  reaper.Undo_EndBlock("Set FX envelopes on " .. actual_name, -1)
  reaper.UpdateArrange()
  reaper.TrackList_AdjustWindows(false)

  local status = #errors > 0 and (written > 0 and "partial" or "error") or "ok"
  return {
    status = status,
    track = actual_name,
    envelopes_written = written,
    points_total = total_points,
    errors = errors,
  }
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

local function op_set_send_volume(cmd)
  local track_name = cmd.track
  if not track_name then return { status = "error", errors = {"Missing track"} } end

  local send_index = cmd.send_index
  if send_index == nil then return { status = "error", errors = {"Missing send_index"} } end

  local tr, _ = resolve_track(track_name)
  if not tr then return { status = "error", errors = {"Track not found: " .. tostring(track_name)} } end

  local num_sends = reaper.GetTrackNumSends(tr, 0)
  if send_index < 0 or send_index >= num_sends then
    return { status = "error", errors = {"send_index " .. send_index .. " out of range (track has " .. num_sends .. " sends)"} }
  end

  local linear
  if cmd.volume_db ~= nil then
    linear = 10 ^ (cmd.volume_db / 20)
  elseif cmd.volume ~= nil then
    linear = cmd.volume
  else
    return { status = "error", errors = {"Provide volume_db (dB) or volume (linear)"} }
  end

  reaper.SetTrackSendInfo_Value(tr, 0, send_index, "D_VOL", linear)

  -- read back
  local actual_vol = reaper.GetTrackSendInfo_Value(tr, 0, send_index, "D_VOL")
  local actual_db = 20 * math.log(actual_vol, 10)
  local _, actual_name = reaper.GetTrackName(tr)
  local dest_tr = reaper.GetTrackSendInfo_Value(tr, 0, send_index, "P_DESTTRACK")
  local dest_name = ""
  if dest_tr then
    local _, dn = reaper.GetTrackName(dest_tr)
    dest_name = dn
  end

  return {
    status = "ok",
    track = actual_name,
    send_index = send_index,
    dest_track = dest_name,
    volume = actual_vol,
    volume_db = actual_db,
  }
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

local function resolve_track_index_only(cmd)
  local track = cmd.track
  if type(track) ~= "number" then
    return nil, nil, "track must be a numeric index"
  end
  local idx = math.floor(track)
  local count = reaper.CountTracks(0)
  if idx < 0 or idx >= count then
    return nil, nil, "Track index " .. idx .. " out of range (0-" .. (count - 1) .. ")"
  end
  return reaper.GetTrack(0, idx), idx, nil
end

local function parse_hex_color(color_str)
  if type(color_str) ~= "string" then
    return nil, "color must be a string in #RRGGBB format"
  end
  local hex = color_str:gsub("^#", "")
  if not hex:match("^%x%x%x%x%x%x$") then
    return nil, "Invalid color '" .. tostring(color_str) .. "' (expected #RRGGBB)"
  end
  local r = tonumber(hex:sub(1, 2), 16)
  local g = tonumber(hex:sub(3, 4), 16)
  local b = tonumber(hex:sub(5, 6), 16)
  local native = reaper.ColorToNative(r, g, b) | 0x1000000
  return native, "#" .. hex:upper()
end

local function op_rename_track(cmd)
  local new_name = cmd.name
  if not new_name or tostring(new_name):match("^%s*$") then
    return { status = "error", errors = {"Missing non-empty name"} }
  end

  local tr, idx, err = resolve_track_index_only(cmd)
  if not tr then return { status = "error", errors = {err} } end

  local _, old_name = reaper.GetTrackName(tr)

  reaper.Undo_BeginBlock()
  reaper.GetSetMediaTrackInfo_String(tr, "P_NAME", tostring(new_name), true)
  reaper.Undo_EndBlock("Rename track: " .. old_name, -1)

  local _, actual_name = reaper.GetTrackName(tr)
  return {
    status = "ok",
    index = idx,
    old_name = old_name,
    track = actual_name,
  }
end

local function op_set_track_color(cmd)
  local tr, idx, err = resolve_track_index_only(cmd)
  if not tr then return { status = "error", errors = {err} } end

  local _, actual_name = reaper.GetTrackName(tr)
  local clear = cmd.clear == true
  local color_value = 0
  local normalized_color = nil

  if not clear then
    local color = cmd.color
    if color == nil then
      return { status = "error", errors = {"Missing color (use #RRGGBB) or set clear=true"} }
    end
    local parsed, parse_err_or_norm = parse_hex_color(color)
    if not parsed then
      return { status = "error", errors = {parse_err_or_norm}, track = actual_name, index = idx }
    end
    color_value = parsed
    normalized_color = parse_err_or_norm
  end

  reaper.Undo_BeginBlock()
  reaper.SetTrackColor(tr, color_value)
  reaper.Undo_EndBlock("Set track color: " .. actual_name, -1)
  reaper.TrackList_AdjustWindows(false)
  reaper.UpdateArrange()

  return {
    status = "ok",
    track = actual_name,
    index = idx,
    color = normalized_color,
    cleared = clear,
  }
end

local function op_reorder_track(cmd)
  local tr, from_idx, err = resolve_track_index_only(cmd)
  if not tr then return { status = "error", errors = {err} } end

  local to_index = cmd.to_index
  if type(to_index) ~= "number" then
    return { status = "error", errors = {"to_index must be a numeric index"} }
  end
  to_index = math.floor(to_index)

  local count = reaper.CountTracks(0)
  if to_index < 0 or to_index >= count then
    return { status = "error", errors = {"to_index out of range (0-" .. (count - 1) .. ")"} }
  end

  local _, actual_name = reaper.GetTrackName(tr)
  if from_idx == to_index then
    return {
      status = "ok",
      track = actual_name,
      from_index = from_idx,
      to_index = to_index,
      moved = false,
    }
  end

  -- Preserve current track selection while performing the move.
  local selected_before = {}
  for i = 0, count - 1 do
    local t = reaper.GetTrack(0, i)
    if reaper.IsTrackSelected(t) then
      selected_before[#selected_before + 1] = t
    end
  end

  reaper.PreventUIRefresh(1)
  reaper.Undo_BeginBlock()

  local ok, move_err = pcall(function()
    reaper.SetOnlyTrackSelected(tr)
    local moved_ok = reaper.ReorderSelectedTracks(to_index, 0)
    if moved_ok == false then
      error("ReorderSelectedTracks returned false")
    end

    -- Restore previous selection snapshot.
    for i = 0, reaper.CountTracks(0) - 1 do
      reaper.SetTrackSelected(reaper.GetTrack(0, i), false)
    end
    for _, t in ipairs(selected_before) do
      reaper.SetTrackSelected(t, true)
    end
  end)

  reaper.Undo_EndBlock("Reorder track: " .. actual_name, -1)
  reaper.PreventUIRefresh(-1)
  reaper.TrackList_AdjustWindows(false)
  reaper.UpdateArrange()

  if not ok then
    return {
      status = "error",
      errors = {"Failed to reorder track: " .. tostring(move_err)},
      track = actual_name,
      from_index = from_idx,
      to_index = to_index,
    }
  end

  local final_index = -1
  for i = 0, reaper.CountTracks(0) - 1 do
    if reaper.GetTrack(0, i) == tr then
      final_index = i
      break
    end
  end

  return {
    status = "ok",
    track = actual_name,
    from_index = from_idx,
    to_index = final_index >= 0 and final_index or to_index,
    moved = true,
  }
end

local function op_insert_media(cmd)
  local file_path = cmd.file_path
  if not file_path then return { status = "error", errors = {"Missing file_path"} } end
  if not file_exists(file_path) then
    return { status = "error", errors = {"File not found: " .. file_path} }
  end

  -- Resolve target track
  local tr, track_idx, err = find_track_strict(cmd.track)
  if not tr then return { status = "error", errors = {err} } end

  local position = cmd.position or 0.0

  reaper.Undo_BeginBlock()

  -- Create media item on track
  local item = reaper.AddMediaItemToTrack(tr)
  if not item then
    reaper.Undo_EndBlock("Insert media (failed)", -1)
    return { status = "error", errors = {"Failed to create media item"} }
  end

  -- Create PCM source from file
  local source = reaper.PCM_Source_CreateFromFile(file_path)
  if not source then
    reaper.DeleteTrackMediaItem(tr, item)
    reaper.Undo_EndBlock("Insert media (failed)", -1)
    return { status = "error", errors = {"Failed to create PCM source from: " .. file_path} }
  end

  -- Get source length
  local source_length = reaper.GetMediaSourceLength(source)

  -- Add take and assign source
  local take = reaper.AddTakeToMediaItem(item)
  reaper.SetMediaItemTake_Source(take, source)

  -- Set item position and length
  reaper.SetMediaItemInfo_Value(item, "D_POSITION", position)
  reaper.SetMediaItemInfo_Value(item, "D_LENGTH", source_length)

  -- Refresh UI
  reaper.UpdateArrange()

  local _, track_name = reaper.GetTrackName(tr)
  reaper.Undo_EndBlock("Insert media: " .. track_name, -1)

  return {
    status = "ok",
    track = track_name,
    track_index = track_idx,
    file_path = file_path,
    position = position,
    length = source_length,
  }
end

local function op_set_item_rate(cmd)
  -- Set playback rate on a media item, optionally by BPM conversion.
  -- Params: track (name/index), item_index (0-based, default 0),
  --         rate (direct playback rate) OR from_bpm + to_bpm (auto-calculates rate).
  --         preserve_pitch (bool, default true)
  local tr, track_idx, err = find_track_strict(cmd.track)
  if not tr then return { status = "error", errors = {err} } end

  local item_index = cmd.item_index or 0
  local item_count = reaper.CountTrackMediaItems(tr)
  if item_index < 0 or item_index >= item_count then
    return { status = "error", errors = {"Item index " .. item_index .. " out of range (0-" .. (item_count - 1) .. ")"} }
  end

  local item = reaper.GetTrackMediaItem(tr, item_index)
  if not item then
    return { status = "error", errors = {"Failed to get media item"} }
  end

  -- Calculate rate
  local rate
  if cmd.from_bpm and cmd.to_bpm then
    rate = cmd.to_bpm / cmd.from_bpm
  elseif cmd.rate then
    rate = cmd.rate
  else
    return { status = "error", errors = {"Provide 'rate' or 'from_bpm' + 'to_bpm'"} }
  end

  local preserve_pitch = true
  if cmd.preserve_pitch == false then preserve_pitch = false end

  reaper.Undo_BeginBlock()

  -- Get current length before rate change
  local old_length = reaper.GetMediaItemInfo_Value(item, "D_LENGTH")

  -- Set playback rate on the active take
  local take = reaper.GetActiveTake(item)
  if take then
    reaper.SetMediaItemTakeInfo_Value(take, "D_PLAYRATE", rate)
    -- Preserve pitch: 1 = on, 0 = off
    reaper.SetMediaItemTakeInfo_Value(take, "B_PPITCH", preserve_pitch and 1 or 0)
  end

  -- Adjust item length to match new rate
  local new_length = old_length / rate
  reaper.SetMediaItemInfo_Value(item, "D_LENGTH", new_length)

  reaper.UpdateArrange()

  local _, track_name = reaper.GetTrackName(tr)
  reaper.Undo_EndBlock("Set item rate: " .. track_name, -1)

  return {
    status = "ok",
    track = track_name,
    track_index = track_idx,
    item_index = item_index,
    rate = rate,
    preserve_pitch = preserve_pitch,
    old_length = old_length,
    new_length = new_length,
  }
end

local function op_drum_augment(cmd)
  local audio_track = cmd.audio_track
  local sample_path = cmd.sample_path
  if not audio_track then return { status = "error", errors = {"Missing audio_track"} } end

  -- Validate sample file if provided
  if sample_path and not file_exists(sample_path) then
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

  -- 2. Load RS5k with sample (if provided)
  local fx_idx, rs5k_err
  if sample_path then
    -- Drum defaults: short attack, moderate decay, no sustain, release >= decay
    -- so the sample rings out naturally instead of cutting off at note-off.
    local rs5k_attack  = cmd.attack  or 0.0
    local rs5k_decay   = cmd.decay   or 0.05   -- ~500ms normalized
    local rs5k_sustain = cmd.sustain or 0.0
    local rs5k_release = cmd.release or rs5k_decay  -- match decay so tail isn't cut short
    -- Ensure release is never shorter than decay
    if rs5k_release < rs5k_decay then rs5k_release = rs5k_decay end
    fx_idx, rs5k_err = _load_rs5k(
      rs5k_tr, sample_path, midi_note,
      rs5k_attack, rs5k_decay, rs5k_sustain, rs5k_release,
      cmd.volume, nil
    )
    if not fx_idx then
      reaper.Undo_EndBlock("Drum augment (failed)", -1)
      return { status = "error", errors = {"RS5k setup failed: " .. tostring(rs5k_err)} }
    end
  else
    -- No sample — just add empty RS5k
    fx_idx = reaper.TrackFX_AddByName(rs5k_tr, "ReaSamplOmatic5000", false, -1)
    if fx_idx < 0 then
      reaper.Undo_EndBlock("Drum augment (failed)", -1)
      return { status = "error", errors = {"Failed to add RS5k"} }
    end
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
-- ReaEQ calibration: maps normalized gain → dB for accurate auto-EQ
-- ---------------------------------------------------------------------------

local function op_calibrate_reaeq()
  reaper.Undo_BeginBlock()
  reaper.PreventUIRefresh(1)

  local tr = nil
  local ok_inner, result_or_err = pcall(function()
    -- Create temp track, add ReaEQ
    reaper.InsertTrackAtIndex(reaper.CountTracks(0), false)
    tr = reaper.GetTrack(0, reaper.CountTracks(0) - 1)
    local fx_idx = reaper.TrackFX_AddByName(tr, "ReaEQ", false, -1)
    if fx_idx < 0 then error("Failed to add ReaEQ") end

    -- Parse display strings like "1.25 kHz", "300.0 Hz", "-3.2 dB"
    local function parse_display_number(disp)
      if not disp then return nil, "" end
      local s = tostring(disp):lower():gsub(",", ".")
      local num_str = s:match("([%+%-]?%d+%.?%d*)")
      return tonumber(num_str), s
    end

    -- Discover ordered ReaEQ band names by intersecting Freq-/Gain- params.
    local function discover_band_names()
      local gain_names = {}
      local freq_names = {}
      local freq_norm = {}
      local param_count = reaper.TrackFX_GetNumParams(tr, fx_idx)
      for pi = 0, param_count - 1 do
        local _, pname = reaper.TrackFX_GetParamName(tr, fx_idx, pi)
        local gname = pname:match("^Gain%-(.+)$")
        if gname then gain_names[gname] = true end
        local fname = pname:match("^Freq%-(.+)$")
        if fname then
          freq_names[fname] = true
          freq_norm[fname] = reaper.TrackFX_GetParamNormalized(tr, fx_idx, pi)
        end
      end

      local names = {}
      for name, _ in pairs(gain_names) do
        if freq_names[name] then
          names[#names + 1] = name
        end
      end

      table.sort(names, function(a, b)
        local na = tonumber((a:match("(%d+)%s*$") or ""))
        local nb = tonumber((b:match("(%d+)%s*$") or ""))
        if na and nb and na ~= nb then return na < nb end
        if na and not nb then return false end
        if nb and not na then return true end
        local fa = freq_norm[a] or 0
        local fb = freq_norm[b] or 0
        if fa ~= fb then return fa < fb end
        return a < b
      end)

      return names
    end

    -- Pick a stable parametric band name for calibration sweeps.
    -- Prefers "Band" types (non-shelf/pass) and picks the middle one.
    local function pick_calibration_band(names)
      if not names or #names == 0 then return nil end

      local candidates = {}
      for _, name in ipairs(names) do
        local n = tostring(name):lower()
        if n:find("band", 1, true)
          and not n:find("shelf", 1, true)
          and not n:find("pass", 1, true)
          and not n:find("notch", 1, true)
          and not n:find("all", 1, true) then
          candidates[#candidates + 1] = name
        end
      end

      local pool = (#candidates > 0) and candidates or names
      local idx = math.floor((#pool + 1) / 2)
      if idx < 1 then idx = 1 end
      return pool[idx]
    end

    -- Try to apply an 11-band stock preset when available.
    local function try_apply_11_band_preset()
      local cur_idx, num_presets = reaper.TrackFX_GetPresetIndex(tr, fx_idx)
      if not num_presets or num_presets <= 0 then return nil end

      local best_idx, best_name = nil, nil
      local best_score = -1
      for i = 0, num_presets - 1 do
        reaper.TrackFX_SetPresetByIndex(tr, fx_idx, i)
        local _, pname = reaper.TrackFX_GetPreset(tr, fx_idx)
        local pl = tostring(pname or ""):lower()
        local score = 0
        if pl:find("11", 1, true) then score = score + 2 end
        if pl:find("band", 1, true) then score = score + 2 end
        if pl:find("basic", 1, true) then score = score + 1 end
        if pl:find("stock", 1, true) then score = score + 1 end
        if score > best_score and score >= 4 then
          best_score = score
          best_idx = i
          best_name = pname
        end
      end

      if best_idx ~= nil then
        reaper.TrackFX_SetPresetByIndex(tr, fx_idx, best_idx)
        return best_name
      end

      if cur_idx and cur_idx >= 0 and cur_idx < num_presets then
        reaper.TrackFX_SetPresetByIndex(tr, fx_idx, cur_idx)
      end
      return nil
    end

    local visible_bands_norm = nil
    local layout_mode = "default"
    local layout_preset_name = nil
    local band_names = discover_band_names()

    -- Expand via Visible bands if available and needed.
    if #band_names < 10 then
      local visible_pi = find_param_index(tr, fx_idx, "Visible bands")
      if visible_pi then
        for n = 0, 100 do
          local norm = n * 0.01
          reaper.TrackFX_SetParamNormalized(tr, fx_idx, visible_pi, norm)
          local _, disp = reaper.TrackFX_GetFormattedParamValue(tr, fx_idx, visible_pi)
          local bands_val = select(1, parse_display_number(disp))
          if bands_val and bands_val >= 10 then
            visible_bands_norm = norm
            break
          end
        end
        if not visible_bands_norm then
          visible_bands_norm = 1.0
        end
        reaper.TrackFX_SetParamNormalized(tr, fx_idx, visible_pi, visible_bands_norm)
        band_names = discover_band_names()
        if #band_names >= 10 then
          layout_mode = "visible_bands"
        end
      end
    end

    -- Fallback: expand via stock 11-band preset when Visible bands is unavailable.
    if #band_names < 10 then
      local preset_name = try_apply_11_band_preset()
      if preset_name then
        layout_mode = "preset_11band"
        layout_preset_name = preset_name
        band_names = discover_band_names()
      end
    end

    if #band_names >= 10 and layout_mode == "default" then
      layout_mode = "expanded_existing"
    end

    local cal_band = pick_calibration_band(band_names) or "Band 2"

    -- === Gain calibration: sweep chosen calibration band gain ===
    local gain_pi = find_param_index(tr, fx_idx, "Gain-" .. cal_band)
    if not gain_pi then
      -- Backward-compatible fallback for unusual naming layouts.
      gain_pi = find_param_index(tr, fx_idx, "Gain-Band 2")
    end
    if not gain_pi then error("Could not find gain param for calibration band: " .. tostring(cal_band)) end

    local gain_mapping = {}
    for n = 0, 100 do
      local norm = n * 0.01
      reaper.TrackFX_SetParamNormalized(tr, fx_idx, gain_pi, norm)
      local _, disp = reaper.TrackFX_GetFormattedParamValue(tr, fx_idx, gain_pi)
      local db_val = select(1, parse_display_number(disp))
      if db_val then
        gain_mapping[#gain_mapping + 1] = {normalized = norm, db = db_val}
      end
    end

    -- === Freq calibration: sweep chosen calibration band freq ===
    local freq_pi = find_param_index(tr, fx_idx, "Freq-" .. cal_band)
    if not freq_pi then
      -- Backward-compatible fallback for unusual naming layouts.
      freq_pi = find_param_index(tr, fx_idx, "Freq-Band 2")
    end
    if not freq_pi then error("Could not find freq param for calibration band: " .. tostring(cal_band)) end

    local freq_mapping = {}
    for n = 0, 100 do
      local norm = n * 0.01
      reaper.TrackFX_SetParamNormalized(tr, fx_idx, freq_pi, norm)
      local _, disp = reaper.TrackFX_GetFormattedParamValue(tr, fx_idx, freq_pi)
      local num, disp_l = parse_display_number(disp)
      local hz_val = nil
      if num then
        if disp_l:find("khz", 1, true) then
          hz_val = num * 1000
        elseif disp_l:find("mhz", 1, true) then
          hz_val = num * 1000000
        else
          -- Covers explicit Hz and unitless numeric displays.
          hz_val = num
        end
      end
      if hz_val then
        freq_mapping[#freq_mapping + 1] = {normalized = norm, hz = hz_val}
      end
    end

    -- === Band type calibration: sweep chosen band type to find "Band" (parametric) value ===
    local type_pi = find_param_index(tr, fx_idx, "Type-" .. cal_band)
    if not type_pi then
      type_pi = find_param_index(tr, fx_idx, "Type-Band 2")
    end
    local band_type_norm = nil
    if type_pi then
      for n = 0, 100 do
        local norm = n * 0.01
        reaper.TrackFX_SetParamNormalized(tr, fx_idx, type_pi, norm)
        local _, disp = reaper.TrackFX_GetFormattedParamValue(tr, fx_idx, type_pi)
        local d = disp and disp:lower() or ""
        if d:find("band", 1, true) or d:find("peak", 1, true) or d:find("bell", 1, true) then
          band_type_norm = norm
          break
        end
      end
    end

    return {
      gain = gain_mapping,
      freq = freq_mapping,
      band_type_norm = band_type_norm,
      band_names = band_names,
      visible_bands_norm = visible_bands_norm,
      layout_mode = layout_mode,
      layout_preset_name = layout_preset_name,
    }
  end)

  -- Cleanup always runs
  if tr then reaper.DeleteTrack(tr) end
  reaper.PreventUIRefresh(-1)
  -- Undo label documents the harmless temp entry
  reaper.Undo_EndBlock("ReaEQ calibration (temp)", -1)

  if ok_inner then
    return {
      status = "ok",
      mapping = result_or_err.gain,
      freq_mapping = result_or_err.freq,
      band_type_norm = result_or_err.band_type_norm,
      band_names = result_or_err.band_names,
      visible_bands_norm = result_or_err.visible_bands_norm,
      layout_mode = result_or_err.layout_mode,
      layout_preset_name = result_or_err.layout_preset_name,
    }
  else
    return {status = "error", errors = {"Calibration failed: " .. tostring(result_or_err)}}
  end
end

-- ---------------------------------------------------------------------------
-- FX rename: tag an FX with a custom name (e.g. "[AutoEQ]")
-- ---------------------------------------------------------------------------

local function op_rename_fx(cmd)
  local track = cmd.track
  local fx_index = cmd.fx_index
  local new_name = cmd.name
  if not track then return {status = "error", errors = {"Missing track"}} end
  if fx_index == nil then return {status = "error", errors = {"Missing fx_index"}} end
  if type(fx_index) ~= "number" then return {status = "error", errors = {"Invalid fx_index type"}} end
  fx_index = math.floor(fx_index)
  if not new_name then return {status = "error", errors = {"Missing name"}} end

  local tr, _, err = resolve_track_from_cmd(cmd)
  if not tr then return {status = "error", errors = {err}} end

  local _, actual_name = reaper.GetTrackName(tr)
  local fx_count = reaper.TrackFX_GetCount(tr)
  if fx_index < 0 or fx_index >= fx_count then
    return {status = "error", errors = {"FX index out of range"}, track = actual_name}
  end

  reaper.TrackFX_SetNamedConfigParm(tr, fx_index, "renamed_name", new_name)

  -- Verify rename
  local _, actual_fx = reaper.TrackFX_GetFXName(tr, fx_index)
  return {status = "ok", track = actual_name, fx_index = fx_index,
          requested_name = new_name, actual_name = actual_fx}
end

-- ---------------------------------------------------------------------------
-- Bulk FX enable/disable by name pattern
-- ---------------------------------------------------------------------------

local function op_set_fx_enabled(cmd)
  local pattern = cmd.pattern
  local enabled = cmd.enabled
  if not pattern then return {status = "error", errors = {"Missing pattern"}} end
  if enabled == nil then return {status = "error", errors = {"Missing enabled (true/false)"}} end

  local count = reaper.CountTracks(0)
  local toggled = 0
  local details = {}

  for i = 0, count - 1 do
    local tr = reaper.GetTrack(0, i)
    local _, tr_name = reaper.GetTrackName(tr)
    local fx_count = reaper.TrackFX_GetCount(tr)
    for fi = 0, fx_count - 1 do
      local _, fx_name = reaper.TrackFX_GetFXName(tr, fi)
      if fx_name:find(pattern, 1, true) then
        reaper.TrackFX_SetEnabled(tr, fi, enabled)
        toggled = toggled + 1
        details[#details + 1] = {track = tr_name, track_index = i, fx_index = fi, fx_name = fx_name}
      end
    end
  end

  return {status = "ok", pattern = pattern, enabled = enabled, toggled = toggled, details = details}
end

-- Restore exact FX enabled states by strict track/fx indices.
-- cmd.states = [{track=<index>, fx_index=<index>, enabled=<bool>, expected_name=<optional>}]
local function op_set_fx_enabled_exact(cmd)
  local states = cmd.states
  if type(states) ~= "table" then
    return {status = "error", errors = {"Missing states array"}}
  end

  local applied = 0
  local errors = {}
  local details = {}
  local track_count = reaper.CountTracks(0)

  for i, st in ipairs(states) do
    local track_idx = st.track
    local fx_index = st.fx_index
    local enabled = st.enabled
    local expected_name = st.expected_name

    if type(track_idx) ~= "number" or type(fx_index) ~= "number" or enabled == nil then
      errors[#errors + 1] = "Invalid state at index " .. i .. " (track, fx_index, enabled required)"
    else
      track_idx = math.floor(track_idx)
      fx_index = math.floor(fx_index)
      if track_idx < 0 or track_idx >= track_count then
        errors[#errors + 1] = "Track index out of range at state " .. i
      else
        local tr = reaper.GetTrack(0, track_idx)
        local _, tr_name = reaper.GetTrackName(tr)
        local fx_count = reaper.TrackFX_GetCount(tr)
        if fx_index < 0 or fx_index >= fx_count then
          errors[#errors + 1] = "FX index out of range at state " .. i
        else
          local _, fx_name = reaper.TrackFX_GetFXName(tr, fx_index)
          if expected_name and fx_name ~= expected_name then
            errors[#errors + 1] = "FX mismatch at state " .. i
          else
            reaper.TrackFX_SetEnabled(tr, fx_index, enabled and true or false)
            applied = applied + 1
            details[#details + 1] = {
              track = tr_name,
              track_index = track_idx,
              fx_index = fx_index,
              fx_name = fx_name,
              enabled = enabled and true or false,
            }
          end
        end
      end
    end
  end

  local status = "ok"
  if #errors > 0 then
    status = applied > 0 and "partial" or "error"
  end
  return {status = status, applied = applied, errors = errors, details = details}
end

-- ---------------------------------------------------------------------------
-- FFT helpers for spectral analysis
-- ---------------------------------------------------------------------------

local FFT_SIZE = 1024
local HOP_SIZE = 512

-- Precomputed Hann window
local HANN_1024 = {}
for i = 1, FFT_SIZE do
  HANN_1024[i] = 0.5 * (1 - math.cos(2 * math.pi * (i - 1) / (FFT_SIZE - 1)))
end

-- 10 frequency bands: Sub, Bass, Low-Mid, Mid, Upper-Mid, Presence, High-Presence, Brilliance, Air, Ultra
local BAND_EDGES = {20, 40, 80, 160, 320, 640, 1250, 2500, 5000, 10000, 20000}

-- In-place radix-2 Cooley-Tukey FFT
-- real[1..n] and imag[1..n] are 1-indexed Lua arrays
local function fft(real, imag, n)
  -- Bit-reversal permutation
  local j = 1
  for i = 1, n do
    if i < j then
      real[i], real[j] = real[j], real[i]
      imag[i], imag[j] = imag[j], imag[i]
    end
    local m = n / 2
    while m >= 1 and j > m do
      j = j - m
      m = m / 2
    end
    j = j + m
  end

  -- Cooley-Tukey butterfly stages
  local stage = 2
  while stage <= n do
    local half = stage / 2
    local angle = -2 * math.pi / stage
    local wr = math.cos(angle)
    local wi = math.sin(angle)
    for k = 1, n, stage do
      local tw_r = 1
      local tw_i = 0
      for m = 0, half - 1 do
        local idx1 = k + m
        local idx2 = idx1 + half
        local tr = tw_r * real[idx2] - tw_i * imag[idx2]
        local ti = tw_r * imag[idx2] + tw_i * real[idx2]
        real[idx2] = real[idx1] - tr
        imag[idx2] = imag[idx1] - ti
        real[idx1] = real[idx1] + tr
        imag[idx1] = imag[idx1] + ti
        local new_tw_r = tw_r * wr - tw_i * wi
        tw_i = tw_r * wi + tw_i * wr
        tw_r = new_tw_r
      end
    end
    stage = stage * 2
  end
end

-- Compute per-band energy from FFT output
-- Returns array of energy values (one per band)
local function band_energy(real, imag, n, sr, edges)
  local num_bands = #edges - 1
  local bands = {}
  for b = 1, num_bands do bands[b] = 0 end

  local bin_hz = sr / n  -- frequency resolution per bin
  -- Only use first n/2 bins (Nyquist)
  for k = 1, n / 2 do
    local freq = (k - 1) * bin_hz
    local mag_sq = real[k] * real[k] + imag[k] * imag[k]
    -- Find which band this bin belongs to
    for b = 1, num_bands do
      if freq >= edges[b] and freq < edges[b + 1] then
        bands[b] = bands[b] + mag_sq
        break
      end
    end
  end

  return bands
end

-- Benchmark command: time 1000 FFT + band_energy calls
local function op_benchmark_fft()
  local frames = 1000
  local real_buf = {}
  local imag_buf = {}
  -- Generate random-ish data
  for i = 1, FFT_SIZE do
    real_buf[i] = math.sin(i * 0.1) * HANN_1024[i]
    imag_buf[i] = 0
  end

  local start = reaper.time_precise()
  for f = 1, frames do
    -- Reset imag to 0 each frame
    for i = 1, FFT_SIZE do imag_buf[i] = 0 end
    fft(real_buf, imag_buf, FFT_SIZE)
    band_energy(real_buf, imag_buf, FFT_SIZE, 44100, BAND_EDGES)
    -- Re-populate real for next frame (avoid optimizing away)
    for i = 1, FFT_SIZE do
      real_buf[i] = math.sin(i * 0.1 + f) * HANN_1024[i]
    end
  end
  local elapsed = reaper.time_precise() - start
  local ms_per_frame = (elapsed * 1000) / frames

  if ms_per_frame > 0.2 then
    reaper.ShowConsoleMsg(string.format(
      "[reaper-ai] WARNING: FFT benchmark %.3f ms/frame (>0.2ms threshold)\n", ms_per_frame))
  else
    reaper.ShowConsoleMsg(string.format(
      "[reaper-ai] FFT benchmark OK: %.3f ms/frame\n", ms_per_frame))
  end

  return {
    status = "ok",
    frames = frames,
    elapsed_ms = elapsed * 1000,
    ms_per_frame = ms_per_frame,
  }
end

-- FFT validation command: synthetic sine + stereo checks.
-- This validates band mapping math independently of project audio content.
local function op_validate_fft_bands(cmd)
  local sample_rate = tonumber(cmd and cmd.sample_rate) or 44100
  if sample_rate <= 0 then sample_rate = 44100 end

  local function expected_band(freq)
    for b = 1, #BAND_EDGES - 1 do
      if freq >= BAND_EDGES[b] and freq < BAND_EDGES[b + 1] then
        return b
      end
    end
    return nil
  end

  local function dominant_band(bands)
    local idx, val = 1, bands[1] or 0
    for i = 2, #bands do
      if bands[i] > val then
        idx, val = i, bands[i]
      end
    end
    return idx, val
  end

  local function make_windowed_sine(freq, sr)
    local real, imag = {}, {}
    local w = 2 * math.pi * freq / sr
    for i = 1, FFT_SIZE do
      real[i] = math.sin(w * (i - 1)) * HANN_1024[i]
      imag[i] = 0
    end
    return real, imag
  end

  local function make_windowed_silence()
    local real, imag = {}, {}
    for i = 1, FFT_SIZE do
      real[i] = 0
      imag[i] = 0
    end
    return real, imag
  end

  local tone_tests = {}
  local tone_failures = 0

  -- Skip sub band (20-40Hz): at FFT_SIZE=1024 bin spacing is too coarse.
  for b = 2, #BAND_EDGES - 1 do
    local lo = BAND_EDGES[b]
    local hi = BAND_EDGES[b + 1]
    local freq = math.sqrt(lo * hi) -- geometric center

    local real, imag = make_windowed_sine(freq, sample_rate)
    fft(real, imag, FFT_SIZE)
    local bands = band_energy(real, imag, FFT_SIZE, sample_rate, BAND_EDGES)
    local observed = dominant_band(bands)
    local expected = expected_band(freq)
    local pass = (observed == expected)
    if not pass then tone_failures = tone_failures + 1 end

    tone_tests[#tone_tests + 1] = {
      freq_hz = freq,
      expected_band = expected and (expected - 1) or -1, -- 0-based for JSON clients
      observed_band = observed and (observed - 1) or -1,
      pass = pass,
    }
  end

  -- Informational low-band probe (not part of pass/fail gate).
  local low_probe_freq = 30.0
  local low_real, low_imag = make_windowed_sine(low_probe_freq, sample_rate)
  fft(low_real, low_imag, FFT_SIZE)
  local low_bands = band_energy(low_real, low_imag, FFT_SIZE, sample_rate, BAND_EDGES)
  local low_observed = dominant_band(low_bands)
  local low_expected = expected_band(low_probe_freq)

  -- Stereo separation checks in analysis-relevant frequency ranges.
  local function channel_band_energy(freq, is_tone)
    if is_tone then
      local r, i = make_windowed_sine(freq, sample_rate)
      fft(r, i, FFT_SIZE)
      return band_energy(r, i, FFT_SIZE, sample_rate, BAND_EDGES)
    end
    local r, i = make_windowed_silence()
    fft(r, i, FFT_SIZE)
    return band_energy(r, i, FFT_SIZE, sample_rate, BAND_EDGES)
  end

  local stereo_tests = {}
  local stereo_failures = 0
  local eps = 1e-18

  -- Left-only 1kHz should dominate left channel in 640-1250 band.
  do
    local freq = 1000.0
    local exp = expected_band(freq)
    local bands_l = channel_band_energy(freq, true)
    local bands_r = channel_band_energy(freq, false)
    local ratio = (bands_l[exp] or 0) / ((bands_r[exp] or 0) + eps)
    local pass = ratio > 10
    if not pass then stereo_failures = stereo_failures + 1 end
    stereo_tests[#stereo_tests + 1] = {
      name = "left_only_1k",
      expected_band = exp and (exp - 1) or -1,
      lr_ratio = ratio,
      pass = pass,
    }
  end

  -- Right-only 3.5kHz should dominate right channel in 2.5k-5k band.
  do
    local freq = 3500.0
    local exp = expected_band(freq)
    local bands_l = channel_band_energy(freq, false)
    local bands_r = channel_band_energy(freq, true)
    local ratio = (bands_r[exp] or 0) / ((bands_l[exp] or 0) + eps)
    local pass = ratio > 10
    if not pass then stereo_failures = stereo_failures + 1 end
    stereo_tests[#stereo_tests + 1] = {
      name = "right_only_3k5",
      expected_band = exp and (exp - 1) or -1,
      rl_ratio = ratio,
      pass = pass,
    }
  end

  local failures = tone_failures + stereo_failures
  local status = failures == 0 and "ok" or "partial"

  return {
    status = status,
    sample_rate = sample_rate,
    fft_size = FFT_SIZE,
    hop_size = HOP_SIZE,
    bin_hz = sample_rate / FFT_SIZE,
    tone_tests = tone_tests,
    stereo_tests = stereo_tests,
    low_band_probe = {
      freq_hz = low_probe_freq,
      expected_band = low_expected and (low_expected - 1) or -1,
      observed_band = low_observed and (low_observed - 1) or -1,
      note = "Informational only: 20-40Hz is under-resolved at FFT_SIZE=1024",
    },
    summary = {
      tone_failures = tone_failures,
      stereo_failures = stereo_failures,
      total_failures = failures,
    },
  }
end

-- ---------------------------------------------------------------------------
-- Deferred analysis state (single-flight: only one at a time)
-- ---------------------------------------------------------------------------

local active_analysis = nil  -- set by op_analyze_track, consumed by analyze_chunk

-- ---------------------------------------------------------------------------
-- Deferred spectral analysis: op_analyze_track + analyze_chunk
-- ---------------------------------------------------------------------------

local CHUNK_BUDGET_S = 0.025  -- 25ms wall-time budget per defer tick

-- Forward declarations so deferred analysis closures can write responses safely
-- even though queue paths are initialized near the main poll loop.
local IN_DIR, OUT_DIR

local function op_analyze_track(cmd)
  -- Single-flight guard
  if active_analysis then
    return {status = "error", errors = {"Analysis already in progress for '" .. active_analysis.track_name .. "'"}}
  end

  local track = cmd.track
  if not track then return {status = "error", errors = {"Missing track"}} end

  local tr, idx, err = resolve_track_from_cmd(cmd)
  if not tr then return {status = "error", errors = {err}} end

  local _, actual_name = reaper.GetTrackName(tr)

  -- Determine time range: default to full project
  local time_start = cmd.time_start
  if time_start == nil then time_start = 0 end
  local time_end = cmd.time_end
  if time_end == nil or time_end == -1 then
    time_end = reaper.GetProjectLength(0)
  end
  if time_end <= time_start then
    return {status = "error", errors = {"Invalid time range: " .. time_start .. " to " .. time_end}}
  end

  -- Create audio accessor for this track
  local accessor = reaper.CreateTrackAudioAccessor(tr)
  if not accessor then
    return {status = "error", errors = {"Failed to create audio accessor for track: " .. actual_name}}
  end

  -- Get project sample rate; fall back to audio device rate, then 44.1k.
  local sample_rate = reaper.GetSetProjectInfo(0, "PROJECT_SRATE", 0, false)
  if sample_rate == 0 then
    -- Project follows device rate — query the audio hardware.
    local ok, dev_rate = reaper.GetAudioDeviceInfo("SRATE", "")
    if ok then sample_rate = tonumber(dev_rate) or 44100
    else sample_rate = 44100 end
  end

  local num_bands = #BAND_EDGES - 1

  active_analysis = {
    cmd_id = cmd.id,
    accessor = accessor,
    track_name = actual_name,
    track_index = idx,
    sr = sample_rate,
    position = time_start,
    time_start = time_start,
    time_end = time_end,
    frame_count = 0,
    band_accum_l = {},
    band_accum_r = {},
    buf = reaper.new_array(FFT_SIZE * 2),  -- stereo interleaved
    real_l = {}, imag_l = {},
    real_r = {}, imag_r = {},
    last_logged_pct = -1,
    mono_fallback = false,
    stereo_silent_streak = 0,
  }

  -- Initialize accumulators and FFT working arrays
  for b = 1, num_bands do
    active_analysis.band_accum_l[b] = 0
    active_analysis.band_accum_r[b] = 0
  end
  for i = 1, FFT_SIZE do
    active_analysis.real_l[i] = 0; active_analysis.imag_l[i] = 0
    active_analysis.real_r[i] = 0; active_analysis.imag_r[i] = 0
  end

  reaper.ShowConsoleMsg("[reaper-ai] Starting analysis: " .. actual_name .. "\n")
  return {status = "pending"}
end

-- Forward reference for analyze_chunk (used by poll loop)
local analyze_chunk

analyze_chunk = function()
  local a = active_analysis
  if not a then return end

  local tick_start = reaper.time_precise()

  while (reaper.time_precise() - tick_start) < CHUNK_BUDGET_S and a.position < a.time_end do
    -- Read FFT_SIZE samples in stereo interleaved
    a.buf.clear()
    reaper.GetAudioAccessorSamples(a.accessor, a.sr, 2, a.position, FFT_SIZE, a.buf)

    -- De-interleave and apply window
    for i = 1, FFT_SIZE do
      local l = a.buf[(i - 1) * 2 + 1]
      local r = a.buf[(i - 1) * 2 + 2]
      a.real_l[i] = l * HANN_1024[i]; a.imag_l[i] = 0
      a.real_r[i] = r * HANN_1024[i]; a.imag_r[i] = 0
    end

    -- Stereo fallback: avoid false positives on silent intros by requiring
    -- several consecutive silent stereo frames and confirming mono has signal.
    if a.mono_fallback then
      a.buf.clear()
      reaper.GetAudioAccessorSamples(a.accessor, a.sr, 1, a.position, FFT_SIZE, a.buf)
      for i = 1, FFT_SIZE do
        local mono = a.buf[i] * HANN_1024[i]
        a.real_l[i] = mono; a.imag_l[i] = 0
        a.real_r[i] = mono; a.imag_r[i] = 0
      end
    else
      local all_zero = true
      for i = 1, FFT_SIZE do
        if a.real_l[i] ~= 0 or a.real_r[i] ~= 0 then all_zero = false; break end
      end
      if all_zero then
        a.stereo_silent_streak = a.stereo_silent_streak + 1
        if a.stereo_silent_streak >= 4 then
          a.buf.clear()
          reaper.GetAudioAccessorSamples(a.accessor, a.sr, 1, a.position, FFT_SIZE, a.buf)
          local mono_has_signal = false
          for i = 1, FFT_SIZE do
            if a.buf[i] ~= 0 then
              mono_has_signal = true
              break
            end
          end
          if mono_has_signal then
            a.mono_fallback = true
            for i = 1, FFT_SIZE do
              local mono = a.buf[i] * HANN_1024[i]
              a.real_l[i] = mono; a.imag_l[i] = 0
              a.real_r[i] = mono; a.imag_r[i] = 0
            end
          end
        end
      else
        a.stereo_silent_streak = 0
      end
    end

    fft(a.real_l, a.imag_l, FFT_SIZE)
    fft(a.real_r, a.imag_r, FFT_SIZE)
    local bands_l = band_energy(a.real_l, a.imag_l, FFT_SIZE, a.sr, BAND_EDGES)
    local bands_r = band_energy(a.real_r, a.imag_r, FFT_SIZE, a.sr, BAND_EDGES)

    -- Accumulate per-channel
    for b = 1, #bands_l do
      a.band_accum_l[b] = a.band_accum_l[b] + bands_l[b]
      a.band_accum_r[b] = a.band_accum_r[b] + bands_r[b]
    end
    a.frame_count = a.frame_count + 1
    a.position = a.position + (HOP_SIZE / a.sr)
  end

  -- Check if done
  if a.position >= a.time_end then
    local num_bands = #BAND_EDGES - 1
    local result_bands = {}
    for b = 1, num_bands do
      local avg_l = a.band_accum_l[b] / math.max(a.frame_count, 1)
      local avg_r = a.band_accum_r[b] / math.max(a.frame_count, 1)
      local avg_mono = (avg_l + avg_r) * 0.5
      result_bands[b] = {
        lo = BAND_EDGES[b], hi = BAND_EDGES[b + 1],
        avg_db = avg_mono > 0 and (10 * math.log(avg_mono, 10)) or -120,
        avg_db_l = avg_l > 0 and (10 * math.log(avg_l, 10)) or -120,
        avg_db_r = avg_r > 0 and (10 * math.log(avg_r, 10)) or -120,
      }
    end

    local result = {
      status = "ok", id = a.cmd_id,
      track = a.track_name, index = a.track_index,
      bands = result_bands, frames = a.frame_count,
      sample_rate = a.sr, fft_size = FFT_SIZE,
    }
    write_file(OUT_DIR .. "/" .. a.cmd_id .. ".json", json_encode(result))

    reaper.DestroyAudioAccessor(a.accessor)
    active_analysis = nil
    reaper.ShowConsoleMsg("[reaper-ai] Analysis complete: " .. a.track_name .. "\n")
  else
    -- Progress logging (every 20%)
    local total = a.time_end - a.time_start
    local pct = total > 0 and math.floor((a.position - a.time_start) / total * 100) or 100
    local log_at = math.floor(pct / 20) * 20
    if log_at > (a.last_logged_pct or -1) then
      a.last_logged_pct = log_at
      reaper.ShowConsoleMsg("[reaper-ai] Analyzing " .. a.track_name .. ": " .. log_at .. "%\n")
    end
  end
end

-- ---------------------------------------------------------------------------
-- Enable/disable individual ReaEQ bands via BANDENABLED named config parm
-- ---------------------------------------------------------------------------

local function op_enable_reaeq_band(cmd)
  local track_name = cmd.track
  local fx_index = cmd.fx_index
  local band = cmd.band           -- 0-based band index (0..4)
  local enabled = cmd.enabled      -- true/false
  if not track_name then return {status = "error", errors = {"Missing track"}} end
  if fx_index == nil then return {status = "error", errors = {"Missing fx_index"}} end
  if band == nil then return {status = "error", errors = {"Missing band (0-4)"}} end
  if enabled == nil then return {status = "error", errors = {"Missing enabled (true/false)"}} end

  local tr, _, resolve_err = resolve_track_from_cmd(cmd)
  if not tr then return {status = "error", errors = {resolve_err or "Track not found"}} end

  local _, actual_name = reaper.GetTrackName(tr)
  local fx_count = reaper.TrackFX_GetCount(tr)
  if fx_index < 0 or fx_index >= fx_count then
    return {status = "error", errors = {"FX index out of range"}, track = actual_name}
  end

  local enable_val = enabled and "1" or "0"
  -- BANDENABLED<N> is the only working key (tested 8 alternatives, none work).
  -- REAPER always returns false for this call but it does take effect.
  reaper.TrackFX_SetNamedConfigParm(tr, fx_index, "BANDENABLED" .. band, enable_val)
  return {status = "ok", track = actual_name, fx_index = fx_index, band = band, enabled = enabled}
end

-- ---------------------------------------------------------------------------
-- Generic FX named-config access (for plugin-specific state/chunks)
-- ---------------------------------------------------------------------------

local function op_get_fx_named_config(cmd)
  local track_name = cmd.track
  local fx_index = cmd.fx_index
  local names = cmd.names or cmd.params

  if not track_name then return {status = "error", errors = {"Missing track"}} end
  if fx_index == nil then return {status = "error", errors = {"Missing fx_index"}} end
  if type(fx_index) ~= "number" then return {status = "error", errors = {"Invalid fx_index type"}} end
  fx_index = math.floor(fx_index)

  if type(names) == "string" then names = { names } end
  if not names or type(names) ~= "table" or #names == 0 then
    return {status = "error", errors = {"Missing names (array of key names)"}}
  end

  local tr, _, resolve_err = resolve_track_from_cmd(cmd)
  if not tr then return {status = "error", errors = {resolve_err or "Track not found"}} end

  local _, actual_name = reaper.GetTrackName(tr)
  local fx_count = reaper.TrackFX_GetCount(tr)
  if fx_index < 0 or fx_index >= fx_count then
    return {status = "error", errors = {"FX index out of range"}, track = actual_name}
  end

  local values = {}
  local errors = {}
  local found_count = 0
  for _, name in ipairs(names) do
    local key = tostring(name)
    local ok, value = reaper.TrackFX_GetNamedConfigParm(tr, fx_index, key)
    if ok then
      found_count = found_count + 1
      values[#values + 1] = {name = key, value = value, found = true}
    else
      values[#values + 1] = {name = key, value = value or "", found = false}
      errors[#errors + 1] = "Named config key not found/readable: " .. key
    end
  end

  local status = "ok"
  if #errors > 0 then
    status = found_count > 0 and "partial" or "error"
  end

  return {
    status = status,
    track = actual_name,
    fx_index = fx_index,
    values = values,
    found = found_count,
    errors = errors,
  }
end

local function op_set_fx_named_config(cmd)
  local track_name = cmd.track
  local fx_index = cmd.fx_index
  local params = cmd.params

  if not track_name then return {status = "error", errors = {"Missing track"}} end
  if fx_index == nil then return {status = "error", errors = {"Missing fx_index"}} end
  if type(fx_index) ~= "number" then return {status = "error", errors = {"Invalid fx_index type"}} end
  fx_index = math.floor(fx_index)
  if not params or type(params) ~= "table" or #params == 0 then
    return {status = "error", errors = {"Missing params"}}
  end

  local tr, _, resolve_err = resolve_track_from_cmd(cmd)
  if not tr then return {status = "error", errors = {resolve_err or "Track not found"}} end

  local _, actual_name = reaper.GetTrackName(tr)
  local fx_count = reaper.TrackFX_GetCount(tr)
  if fx_index < 0 or fx_index >= fx_count then
    return {status = "error", errors = {"FX index out of range"}, track = actual_name}
  end

  reaper.Undo_BeginBlock()
  local applied = 0
  local errors = {}
  local confirmed = {}

  for _, p in ipairs(params) do
    local name = p.name
    local value = p.value
    if not name or value == nil then
      errors[#errors + 1] = "Param entry missing name or value"
    else
      local key = tostring(name)
      local value_str = tostring(value)
      local set_ok = reaper.TrackFX_SetNamedConfigParm(tr, fx_index, key, value_str)
      local get_ok, readback = reaper.TrackFX_GetNamedConfigParm(tr, fx_index, key)
      local verified = get_ok and tostring(readback) == value_str

      if set_ok or verified then
        applied = applied + 1
      else
        errors[#errors + 1] = "Failed to set named config key: " .. key
      end

      confirmed[#confirmed + 1] = {
        name = key,
        requested = value_str,
        value = readback or "",
        set_ok = set_ok and true or false,
        get_ok = get_ok and true or false,
        verified = verified and true or false,
      }
    end
  end

  reaper.Undo_EndBlock("Set named config on FX " .. fx_index, -1)

  local status = #errors > 0 and (applied > 0 and "partial" or "error") or "ok"
  return {
    status = status,
    applied = applied,
    errors = errors,
    track = actual_name,
    fx_index = fx_index,
    confirmed = confirmed,
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
  elseif op == "reorder_fx" then
    return op_reorder_fx(cmd)
  elseif op == "create_track" then
    return op_create_track(cmd)
  elseif op == "duplicate_track" then
    return op_duplicate_track(cmd)
  elseif op == "list_presets" then
    return op_list_presets(cmd)
  elseif op == "set_param" then
    return op_set_param(cmd)
  elseif op == "reorder_fx" then
    return op_reorder_fx(cmd)
  elseif op == "set_param_display" then
    return op_set_param_display(cmd)
  elseif op == "set_preset" then
    return op_set_preset(cmd)
  elseif op == "get_envelope" then
    return op_get_envelope(cmd)
  elseif op == "set_envelope_points" then
    return op_set_envelope_points(cmd)
  elseif op == "clear_envelope" then
    return op_clear_envelope(cmd)
  elseif op == "set_fx_envelopes" then
    return op_set_fx_envelopes(cmd)
  elseif op == "has_fx_envelopes" then
    return op_has_fx_envelopes(cmd)
  elseif op == "add_send" then
    return op_add_send(cmd)
  elseif op == "get_sends" then
    return op_get_sends(cmd)
  elseif op == "set_send_volume" then
    return op_set_send_volume(cmd)
  elseif op == "load_sample_rs5k" then
    return op_load_sample_rs5k(cmd)
  elseif op == "setup_reagate_midi" then
    return op_setup_reagate_midi(cmd)
  elseif op == "set_track_folder" then
    return op_set_track_folder(cmd)
  elseif op == "set_track_visible" then
    return op_set_track_visible(cmd)
  elseif op == "rename_track" then
    return op_rename_track(cmd)
  elseif op == "set_track_color" then
    return op_set_track_color(cmd)
  elseif op == "reorder_track" then
    return op_reorder_track(cmd)
  elseif op == "insert_media" then
    return op_insert_media(cmd)
  elseif op == "set_item_rate" then
    return op_set_item_rate(cmd)
  elseif op == "drum_augment" then
    return op_drum_augment(cmd)
  elseif op == "analyze_track" then
    return op_analyze_track(cmd)
  elseif op == "calibrate_reaeq" then
    return op_calibrate_reaeq()
  elseif op == "rename_fx" then
    return op_rename_fx(cmd)
  elseif op == "set_fx_enabled" then
    return op_set_fx_enabled(cmd)
  elseif op == "set_fx_enabled_exact" then
    return op_set_fx_enabled_exact(cmd)
  elseif op == "enable_reaeq_band" then
    return op_enable_reaeq_band(cmd)
  elseif op == "get_fx_named_config" then
    return op_get_fx_named_config(cmd)
  elseif op == "set_fx_named_config" then
    return op_set_fx_named_config(cmd)
  elseif op == "log" then
    reaper.ShowConsoleMsg("[reaper-ai] " .. (cmd.message or "") .. "\n")
    return {status = "ok"}
  elseif op == "benchmark_fft" then
    return op_benchmark_fft()
  elseif op == "validate_fft_bands" then
    return op_validate_fft_bands(cmd)
  else
    return { status = "error", errors = {"Unknown operation: " .. tostring(op)} }
  end
end

-- ---------------------------------------------------------------------------
-- Main poll loop
-- ---------------------------------------------------------------------------

QUEUE_BASE = resolve_queue_path()
IN_DIR = QUEUE_BASE .. "/in"
OUT_DIR = QUEUE_BASE .. "/out"

-- Ensure directories exist (native REAPER API — no cmd.exe subprocess)
reaper.RecursiveCreateDirectory(IN_DIR, 0)
reaper.RecursiveCreateDirectory(OUT_DIR, 0)

reaper.ShowConsoleMsg("[reaper-ai] Daemon started. Polling: " .. IN_DIR .. "\n")

local last_poll_time = 0

local function poll()
  -- Process deferred analysis work (if any), protected by pcall
  if active_analysis then
    local ok, err = pcall(analyze_chunk)
    if not ok then
      -- Analysis crashed — write error response, clean up
      local a = active_analysis
      local result = {status = "error", id = a.cmd_id, errors = {"Analysis error: " .. tostring(err)}}
      write_file(OUT_DIR .. "/" .. a.cmd_id .. ".json", json_encode(result))
      if a.accessor then reaper.DestroyAudioAccessor(a.accessor) end
      active_analysis = nil
      reaper.ShowConsoleMsg("[reaper-ai] Analysis failed: " .. tostring(err) .. "\n")
    end
  end

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

            -- Guard: pcall failure returns string, not table
            if not ok then
              result = { status = "error", errors = {"Lua error: " .. tostring(result)} }
            end

            -- Guard: nil or non-table result (defensive)
            if result == nil or type(result) ~= "table" then
              result = { status = "error", errors = {"Dispatch returned invalid result"} }
            end

            -- Deferred commands (like analyze_track) return {status="pending"}.
            -- Their response will be written later by analyze_chunk().
            if result.status ~= "pending" then
              result.id = cmd_id
              local out_path = OUT_DIR .. "/" .. cmd_id .. ".json"
              write_file(out_path, json_encode(result))
            end
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
