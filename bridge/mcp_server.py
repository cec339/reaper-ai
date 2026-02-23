"""MCP server for REAPER AI — exposes reaper-ai tools over stdio transport."""

import json

from mcp.server.fastmcp import FastMCP

from bridge.ipc import send_command
from bridge.reaper_state import (
    format_apply_result,
    format_context,
    format_presets,
    format_track_fx,
)

mcp = FastMCP(
    "reaper-ai",
    instructions=(
        "Control REAPER DAW via AI. Use reaper_get_context first to see "
        "available tracks and FX, then manipulate them with the other tools."
    ),
)


def _error_text(result: dict) -> str:
    errors = result.get("errors", ["Unknown error"])
    return "Error: " + "; ".join(errors)


@mcp.tool()
def reaper_get_context() -> str:
    """Get all tracks in the current REAPER project and the list of installed FX plugins."""
    result = send_command("get_context")
    if result.get("status") == "ok":
        return format_context(result)
    return _error_text(result)


@mcp.tool()
def reaper_get_track_fx(track: str) -> str:
    """Get the FX chain and all parameter values for a track.

    Args:
        track: Track name or substring to match (e.g. "Vocals", "Bass")
    """
    result = send_command("get_track_fx", track=track)
    if result.get("status") == "ok":
        return format_track_fx(result)
    return _error_text(result)


@mcp.tool()
def reaper_create_track(name: str) -> str:
    """Create a new empty track in REAPER.

    Args:
        name: Name for the new track
    """
    result = send_command("create_track", name=name)
    if result.get("status") == "ok":
        return f"Created track '{result.get('track')}' at index {result.get('index')}"
    return _error_text(result)


@mcp.tool()
def reaper_duplicate_track(track: str, new_name: str | None = None) -> str:
    """Duplicate a track including its media items, FX chain, and routing.

    Args:
        track: Source track name or substring to match
        new_name: Optional name for the duplicated track
    """
    kwargs: dict = {"track": track}
    if new_name is not None:
        kwargs["new_name"] = new_name
    result = send_command("duplicate_track", **kwargs)
    if result.get("status") == "ok":
        return f"Duplicated '{result.get('source')}' -> '{result.get('track')}' at index {result.get('index')}"
    return _error_text(result)


@mcp.tool()
def reaper_set_param(track: str, fx_index: int, params: str) -> str:
    """Set one or more parameters on an FX plugin.

    Args:
        track: Track name or substring to match
        fx_index: Index of the FX plugin on the track (0-based)
        params: JSON string of parameter name/value pairs, e.g. '{"Gain": 0.5, "Mix": 0.75}'
    """
    try:
        param_dict = json.loads(params)
    except json.JSONDecodeError as e:
        return f"Error: Invalid JSON for params: {e}"

    param_list = [{"name": k, "value": float(v)} for k, v in param_dict.items()]
    result = send_command("set_param", track=track, fx_index=fx_index, params=param_list)
    status = result.get("status", "unknown")
    applied = result.get("applied", 0)
    errors = result.get("errors", [])
    text = f"Status: {status}, applied: {applied}"
    if errors:
        text += "\nErrors: " + "; ".join(errors)
    return text


@mcp.tool()
def reaper_list_presets(track: str, fx_index: int) -> str:
    """List all available presets for an FX plugin on a track.

    Args:
        track: Track name or substring to match
        fx_index: Index of the FX plugin on the track (0-based)
    """
    result = send_command("list_presets", track=track, fx_index=fx_index)
    if result.get("status") == "ok":
        return format_presets(result)
    return _error_text(result)


@mcp.tool()
def reaper_set_preset(
    track: str,
    fx_index: int,
    preset_name: str | None = None,
    preset_index: int | None = None,
) -> str:
    """Set a preset on an FX plugin by name or index. Provide either preset_name or preset_index.

    Args:
        track: Track name or substring to match
        fx_index: Index of the FX plugin on the track (0-based)
        preset_name: Name of the preset to activate
        preset_index: Index of the preset to activate (0-based)
    """
    if preset_name is None and preset_index is None:
        return "Error: Provide either preset_name or preset_index"

    kwargs: dict = {"track": track, "fx_index": fx_index}
    if preset_index is not None:
        kwargs["preset_index"] = preset_index
    else:
        kwargs["preset_name"] = preset_name
    result = send_command("set_preset", **kwargs)
    if result.get("status") == "ok":
        return f"Preset set: {result.get('preset', '?')}"
    return _error_text(result)


@mcp.tool()
def reaper_apply_plan(track: str, plan: str) -> str:
    """Apply a multi-step FX plan to a track. The plan is a JSON array of steps,
    where each step can add FX, set parameters, or set presets.

    Args:
        track: Track name or substring to match
        plan: JSON string — an array of plan steps, e.g.
              '[{"action":"add_fx","fx_name":"ReaEQ"},{"action":"set_param","fx_index":0,"params":[{"name":"Gain","value":0.5}]}]'
    """
    try:
        plan_data = json.loads(plan)
    except json.JSONDecodeError as e:
        return f"Error: Invalid JSON for plan: {e}"

    result = send_command("apply_plan", track=track, plan=plan_data)
    return format_apply_result(result)


def _is_interactive() -> bool:
    """True when launched by a human (double-click / terminal), not by an MCP client."""
    import sys

    # Explicit subcommand always wins
    if len(sys.argv) > 1:
        return sys.argv[1] == "install"
    # MCP clients pipe stdin; double-click or terminal gives a real TTY
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def main():
    import sys

    if _is_interactive():
        from bridge.installer import run_install

        run_install()
        return

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
