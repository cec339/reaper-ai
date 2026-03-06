# reaper-ai

AI-powered FX chain controller for [REAPER](https://www.reaper.fm/). Talk to your DAW in plain English — create tracks, tweak FX parameters, apply presets, and build entire FX chains through conversation.

## What's new in v0.3.0

- **Auto-EQ system** — four modes of automatic frequency masking correction:
  - **Pair mode** (`auto-eq`): surgical cuts on yield tracks so a priority track is heard clearly
  - **Hierarchy mode** (`auto-eq-all`): priority-based masking correction across all tracks
  - **Complementary mode** (`auto-eq-compl`): lane-shaping within instrument families (guitar, keys/synth)
  - **Sections mode** (`auto-eq-sections`): time-varying EQ automation across song sections via FX envelopes
- **Spectral analysis** — 10-band frequency energy analysis for any track
- **Display-value parameter setting** — set FX params by real-world units (Hz, dB, ms) via binary search
- **ReaEQ band enable/disable** — toggle individual bands via `BANDENABLED` config
- **Named FX config read/write** — access plugin chunk/config state not exposed as parameters
- **Send volume control** — adjust send levels in dB
- **Track organization** — folder/bus creation, reorder, rename, color, visibility
- **Media insertion** — insert audio files onto tracks
- **Enhanced daemon** — folder-aware context, strict track resolution, token-subset param search

## How it works

```
Claude (Desktop/Code/CLI)
        |
   Python bridge  ──  file-based IPC (JSON queue)
        |
   Lua daemon running inside REAPER
```

A Lua script runs inside REAPER and polls a queue folder for JSON commands. The Python bridge writes commands and reads responses through this queue. No network, no OSC, no python-reapy — just files on disk.

## Three ways to use it

### 1. Claude Desktop (for musicians)

Download `reaper-mcp.exe` and double-click it. The installer will:

- Copy the Lua daemon to your REAPER Scripts folder
- Register the MCP server with Claude Desktop
- Set up the shared queue path

Then:

1. Open REAPER > Actions > Show action list > Load ReaScript > select `reaper_daemon.lua` > Run
2. Open Claude Desktop and start chatting: *"Show me my tracks"*, *"Add a compressor to the vocals"*

### 2. Claude Code (for developers)

```bash
git clone https://github.com/cec339/reaper-ai.git
cd reaper-ai
pip install -e .
```

Use the CLI directly:

```bash
reaper-ai context                          # list tracks and FX
reaper-ai track-info "Vocals"              # FX chain + params (with display values: Hz, dB, ms, %)
reaper-ai set-param "Vocals" 0 "Gain=0.5"  # set a parameter (confirms with display units)
reaper-ai create-track "Bass DI"           # create a track
reaper-ai get-envelope master Volume       # read master volume envelope
reaper-ai set-envelope master Volume '[{"time":176,"value":1.0},{"time":180,"value":0.0}]'  # fade out
reaper-ai auto-eq "Vocals" "Guitar,Keys"   # surgical masking correction
reaper-ai auto-eq-all                      # priority-based EQ across all tracks
reaper-ai analyze-tracks "Vocals,Guitar"   # spectral analysis
reaper-ai rename-track 5 "Lead Guitar"     # rename track by index
reaper-ai set-track-color 5 "#FF4444"      # set track color
```

Or add the MCP server to Claude Code's config for tool-based access.

### 3. Other MCP clients (Cursor, Windsurf, Cline, etc.)

Point any MCP-compatible client at the `reaper-mcp` command (or `reaper-mcp.exe`). It runs on stdio transport.

## Setup

### Requirements

- **REAPER** (any recent version)
- **Python 3.10+** (for development/CLI usage — not needed for the standalone exe)

### REAPER daemon

The Lua daemon must be running in REAPER for any interface to work:

1. Actions > Show action list > **Load ReaScript**
2. Select `reaper_daemon.lua` (in REAPER's Scripts folder if installed, or in the repo root for dev)
3. Run the action — you'll see *"Daemon started"* in the REAPER console

### Configuration

`config.json` controls the queue path and timeout. It's looked for next to the exe/script:

```json
{
  "queue_path": "C:/Users/you/AppData/Local/reaper-ai/queue",
  "timeout": 10
}
```

Both the Lua daemon and Python bridge need to point at the same queue path.

## Available tools

| Tool | Description |
|------|-------------|
| `reaper_get_context` | List all tracks and installed FX plugins |
| `reaper_get_track_fx` | Get FX chain and parameter values with display units (Hz, dB, ms, %) |
| `reaper_create_track` | Create a new empty track |
| `reaper_duplicate_track` | Duplicate a track with media items and FX |
| `reaper_set_param` | Set FX parameter values — confirms with display units |
| `reaper_set_param_display` | Set FX params by display value (Hz, dB, ms) via binary search |
| `reaper_list_presets` | List available presets for an FX |
| `reaper_set_preset` | Activate a preset by name or index |
| `reaper_apply_plan` | Apply a multi-step FX plan to a track |
| `reaper_get_envelope` | Read automation envelope points from a track |
| `reaper_set_envelope_points` | Insert automation points (volume fades, pan sweeps, etc.) |
| `reaper_clear_envelope` | Remove all automation points from an envelope |
| `reaper_add_send` | Create a send (routing) between two tracks |
| `reaper_get_sends` | List all sends from a track with routing config |
| `reaper_set_send_volume` | Adjust send volume in dB |
| `reaper_load_sample_rs5k` | Load a sample into ReaSamplOmatic5000 |
| `reaper_setup_reagate_midi` | Add/configure ReaGate for MIDI triggering |
| `reaper_drum_augment` | SSD-style drum augment: sample trigger from audio track |
| `reaper_insert_media` | Insert audio file on a track |
| `reaper_set_track_folder` | Set folder depth on a track |
| `reaper_set_track_visible` | Show or hide a track in TCP and/or mixer |
| `reaper_rename_track` | Rename a track |
| `reaper_set_track_color` | Set track color |
| `reaper_reorder_track` | Move a track to a new position |
| `reaper_get_fx_named_config` | Read plugin chunk/config state |
| `reaper_set_fx_named_config` | Write plugin chunk/config state |
| `reaper_enable_reaeq_band` | Enable/disable specific ReaEQ bands |
| `reaper_analyze_tracks` | Spectral analysis (10-band) for tracks |
| `reaper_auto_eq` | Surgical masking correction between two tracks |
| `reaper_auto_eq_all` | Priority-based masking correction across all tracks |
| `reaper_auto_eq_compl` | Complementary EQ within instrument families |
| `reaper_auto_eq_sections` | Time-varying EQ automation across song sections |
| `reaper_recover_autoeq` | Recover auto-EQ state after interruption |

## Building the exe

```bash
pip install pyinstaller
pyinstaller reaper-mcp.spec
```

Output: `dist/reaper-mcp.exe` — standalone, no Python required.

## Project structure

```
reaper-ai/
  bridge/
    cli.py           # CLI entrypoint (reaper-ai command)
    mcp_server.py    # MCP server entrypoint (reaper-mcp command)
    installer.py     # Setup wizard for double-click install
    ipc.py           # File-based IPC with the Lua daemon
    reaper_state.py  # Response formatters
    auto_eq.py       # Spectral analysis, masking detection, auto-EQ engine
  reaper_daemon.lua  # Lua daemon that runs inside REAPER
  samples/           # Sample audio files for drum augment
  sync-daemon.ps1    # Dev helper: sync daemon to REAPER Scripts folder
  pyproject.toml     # Package config, dependencies, entry points
  config.json        # Queue path and timeout (gitignored)
```

## License

MIT
