# reaper-ai

AI-powered FX chain controller for [REAPER](https://www.reaper.fm/). Talk to your DAW in plain English — create tracks, tweak FX parameters, apply presets, and build entire FX chains through conversation.

## What's new in v0.3.1

### Live anti-mask graph (real-time mixing assistant)

A new live system that watches your mix as it plays and helps an instrument poke through when it's being masked, with no offline analysis pass. JSFX workers run on each tracked source, publishing FFT energy through REAPER's shared `gmem`; a graph of priority relationships then drives anti-masking JSFX inserts on the lower-priority tracks. Stereo-aware (L/R analysis), with drum super-family caps and ancestor-chain inheritance for buses.

```bash
reaper-ai live-graph-setup            # auto-detect tracks, install workers + anti-mask FX
reaper-ai live-graph-status           # check what's wired up
reaper-ai live-graph-stats            # sample real-time pressure values for tuning
reaper-ai live-graph-remove           # tear it all down
```

The toggle script `toggle_antimask.lua` (loaded as a REAPER Action) bypasses the whole system in one click for A/B comparison. Note: gmem state is lost when REAPER closes, so re-run `live-graph-setup` after reopening a project.

### Auto-EQ structural guards (hybrid mode)

Real-mix usage exposed two structural problems with the complementary boost pass: cuts being undone immediately by boosts on the same band, and whole instrument families losing identity after subtractive cuts. Three new guards:

- **Support cap** — support tracks can no longer reach the anchor's +6 dB ceiling. Hard cap at 3 dB enforced in both deficit-driven and ownership-driven boost paths.
- **Anti-refill guard** — if multiple tracks were cut in a band (e.g. snare and vocal both carved out at 320–640 Hz), the boost pass can no longer refill that band with another track. Aggregates `[AutoEQ]` cuts across the entire selection (including non-family/singleton tracks).
- **Starvation guard with fallback** — when a track's role-target bands are all blocked, the system picks one non-target band where the track has high family-relative energy and applies a small recovery boost (≤ 2.5 dB). Refuses if no qualifying band exists — no participation-trophy boosts.

### Auto-EQ sampling for long sessions

Hour-long arrangements now analyze in seconds, not minutes. Sampled analysis takes short windows at fixed intervals across the project instead of reading every sample. Defaults auto-tune from project length, with manual overrides:

```bash
reaper-ai auto-eq-all --no-sample                 # force full analysis
reaper-ai auto-eq-all --sample-dur 2 --sample-interval 20
```

A silence-fallback retry kicks in automatically if a sampled track's windows happened to all land on quiet sections.

### FX parameter envelopes

Auto-EQ-sections writes time-varying ReaEQ moves directly to FX parameter envelopes, so EQ adjustments follow the song rather than just snapshotting one section. Daemon ops `set_fx_envelopes` / `has_fx_envelopes` handle the API/PARMENV value-space conversion (api space ≠ envelope space — getting this wrong silently doubles your gain).

### Auto-EQ tuning from real-mix iterations

- **Energy-weighted selective makeup** — makeup gain on uncut bands weighted by the yield track's own spectral energy. Bands with no real energy get no makeup.
- **Family map with bus inheritance** — leaf tracks inherit family classification (guitar / keys-synth / vocal / cymbal) from their parent bus when the leaf name is unrecognised.
- **Intra-family frequency spreading** — same-family tracks boosting in the same band get distinct sub-lane Hz via log spacing.
- **Priority reorder** — piano > keys > guitar > synth > hihat > cymbal, with cymbal kept above the 0.5 default boundary.
- **Makeup retuning** — `MAKEUP_BIAS` 1.5 → 1.2, `MAKEUP_FLOOR_DB` 1.2 → 0.6, `MAKEUP_PER_BAND_CAP_DB` 2.5 → 1.8.
- **Audit JSON artifacts** — every auto-eq run writes a per-band decision audit including lane assignments, anti-refill summary, starvation moves, and family-mean spectra. Useful for understanding why the system did or didn't act on a given track.

### ReaGate / drum augment gate refinements

`setup-reagate` and `drum-augment` now expose pre-open lookahead, sidechain highpass, and hysteresis. Important when triggering a sample from a bleedy live snare track:

```bash
reaper-ai drum-augment "Snare Top" --drum-type snare \
  --gate-pre-open 1.0 --gate-highpass 200 --gate-hysteresis 4.0
```

### Smaller additions

- **`set-item-rate`** — time-stretch a media item by ratio or by from/to BPM.
- **`toggle-autoeq`** — bypass/restore all `[AutoEQ]` and `[AutoEQ-Comp]` FX in one click for A/B.
- **`benchmark-fft` / `validate-fft-bands`** — daemon-side FFT diagnostics for tuning analyzer accuracy.
- **`calibrate-reaeq`** — capability cascade across visible-bands → 11-band preset → 5-band fallback, cached at `queue/calibration/reaeq_calibration.json`.
- **Track-ref coercion** — CLI commands now accept either a track name or a numeric index; numeric strings get coerced to ints automatically.
- **Daemon ops for transport, peak metering, track delete, main-send routing, and namespace-restricted gmem** — the daemon side is wired even where there's no top-level CLI/MCP yet.

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
| `reaper_live_graph_setup` | Install live anti-mask graph (workers + JSFX inserts) |
| `reaper_live_graph_status` | Report what live anti-mask is currently wired to |
| `reaper_live_graph_stats` | Sample real-time pressure values from gmem for tuning |
| `reaper_live_graph_remove` | Tear down the live anti-mask graph |

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
    live_graph.py    # Live anti-mask graph builder (priority + ancestor chain)
  jsfx/              # JSFX plugins for live anti-mask (FFT workers, anti-mask insert)
  install_jsfx.py    # Installer for the JSFX plugins (copies to REAPER's Effects folder)
  reaper_daemon.lua  # Lua daemon that runs inside REAPER
  toggle_antimask.lua  # REAPER Action: bypass/restore live anti-mask
  tests/             # pytest unit tests (lane assignment, family map, live graph)
  docs/              # Design notes and AI handoff status
  samples/           # Sample audio files for drum augment
  sync-daemon.ps1    # Dev helper: sync daemon to REAPER Scripts folder
  pyproject.toml     # Package config, dependencies, entry points
  config.json        # Queue path and timeout (gitignored)
```

## License

MIT
