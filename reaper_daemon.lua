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
      params[#params + 1] = { index = pi, name = pname, value = val }
    end
    fx_chain[#fx_chain + 1] = {
      index = fi,
      name = fx_name,
      params = params
    }
  end

  return { status = "ok", track = actual_name, fx_chain = fx_chain }
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
      local param_name = step.param_name
      local value = step.value
      if fx_index == nil or not param_name or value == nil then
        errors[#errors + 1] = "set_param step missing required fields"
      else
        -- fx_index in the plan is relative to the plan's own FX additions
        -- The actual REAPER fx index = base_fx_count + fx_index
        local real_fx_idx = base_fx_count + fx_index
        local fx_count = reaper.TrackFX_GetCount(tr)
        if real_fx_idx >= fx_count then
          errors[#errors + 1] = "FX index " .. fx_index .. " (real: " .. real_fx_idx .. ") out of range"
        else
          local pi = find_param_index(tr, real_fx_idx, param_name)
          if not pi then
            errors[#errors + 1] = "Param not found: " .. param_name .. " on FX " .. fx_index
          else
            reaper.TrackFX_SetParamNormalized(tr, real_fx_idx, pi, value)
            applied = applied + 1
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
  for _, p in ipairs(params) do
    local pi = find_param_index(tr, fx_index, p.name)
    if not pi then
      errors[#errors + 1] = "Param not found: " .. tostring(p.name)
    else
      reaper.TrackFX_SetParamNormalized(tr, fx_index, pi, p.value)
      applied = applied + 1
    end
  end
  reaper.Undo_EndBlock("Set params on FX " .. fx_index, -1)

  local status = #errors > 0 and (applied > 0 and "partial" or "error") or "ok"
  return { status = status, applied = applied, errors = errors, track = actual_name }
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
