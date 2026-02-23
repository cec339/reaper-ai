# reaper-ai

AI-powered FX chain controller for [REAPER](https://www.reaper.fm/). Talk to your DAW in plain English — create tracks, tweak FX parameters, apply presets, and build entire FX chains through conversation.

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
reaper-ai track-info "Vocals"              # FX chain + params
reaper-ai set-param "Vocals" 0 "Gain=0.5"  # set a parameter
reaper-ai create-track "Bass DI"           # create a track
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
| `reaper_get_track_fx` | Get FX chain and parameter values for a track |
| `reaper_create_track` | Create a new empty track |
| `reaper_duplicate_track` | Duplicate a track with media items and FX |
| `reaper_set_param` | Set FX parameter values |
| `reaper_list_presets` | List available presets for an FX |
| `reaper_set_preset` | Activate a preset by name or index |
| `reaper_apply_plan` | Apply a multi-step FX plan to a track |

## Building the exe

```bash
pip install pyinstaller
pyinstaller --onefile --name reaper-mcp --collect-all mcp --add-data "reaper_daemon.lua;." bridge/mcp_server.py
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
  reaper_daemon.lua  # Lua daemon that runs inside REAPER
  pyproject.toml     # Package config, dependencies, entry points
  config.json        # Queue path and timeout (gitignored)
```

## License

MIT
