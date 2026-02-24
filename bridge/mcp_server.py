"""MCP server for REAPER AI — exposes reaper-ai tools over stdio transport."""

import json

from mcp.server.fastmcp import FastMCP

from bridge.ipc import send_command
from bridge.reaper_state import (
    format_apply_result,
    format_context,
    format_drum_augment,
    format_envelope,
    format_envelope_result,
    format_presets,
    format_sends,
    format_track_fx,
)

mcp = FastMCP(
    "reaper-ai",
    instructions=(
        "Control REAPER DAW via AI. Use reaper_get_context first to see "
        "available tracks and FX, then manipulate them with the other tools. "
        "GAIN STAGING: Whenever you boost EQ, add saturation, or increase any "
        "gain parameter, always compensate output levels to avoid clipping. "
        "Tell the user what gain changes you made and how you compensated."
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

    Each parameter includes its normalized value (0-1) and formatted display
    value with real-world units (Hz, dB, ms, %, etc.).

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

    Returns confirmed values with formatted display units (Hz, dB, ms, %, etc.)
    so you can verify what was actually set.

    IMPORTANT: When boosting any gain/level parameter above 0.5 (unity), always
    compensate by trimming the plugin's output gain or a later stage to avoid
    clipping. When cutting significantly, consider making up gain to maintain
    level. Inform the user of all gain staging decisions.

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
    return format_apply_result(result)


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
    """Apply a multi-step FX plan to a track. The plan is a JSON object with a
    title and an array of steps. Each step can add FX, set parameters, or set presets.
    All steps execute in a single undo block.

    IMPORTANT: When a plan boosts or cuts gain at any stage, include compensating
    gain staging to maintain proper headroom. Inform the user of all level changes.

    Step types:
      add_fx:    {"action":"add_fx", "fx_name":"ReaEQ"}
      set_param: {"action":"set_param", "fx_index":0, "params":[{"name":"Band 1 Gain","value":0.7}, {"name":"Band 1 Freq","value":0.5}]}
                 fx_index is relative to FX added by this plan (0 = first added FX).
                 params is an array of {"name": <param_name>, "value": <normalized 0-1>}.
      remove_fx: {"action":"remove_fx", "fx_index":0}

    Args:
        track: Track name or substring to match
        plan: JSON string — a plan object, e.g.
              '{"title":"Add EQ","steps":[{"action":"add_fx","fx_name":"ReaEQ"},{"action":"set_param","fx_index":0,"params":[{"name":"Band 1 Gain","value":0.7}]}]}'
    """
    try:
        plan_data = json.loads(plan)
    except json.JSONDecodeError as e:
        return f"Error: Invalid JSON for plan: {e}"

    result = send_command("apply_plan", track=track, plan=plan_data)
    return format_apply_result(result)


@mcp.tool()
def reaper_get_envelope(track: str, envelope: str) -> str:
    """Read automation envelope points from a track.

    Args:
        track: Track name or substring to match, or "master" for the master track
        envelope: Envelope name — one of "Volume", "Pan", "Mute", "Width".
                  The envelope must be visible on the track (press V on the track to show envelopes).
                  Value ranges: Volume 0.0–2.0 (1.0 = 0dB), Pan -1.0 to 1.0 (0 = center).
    """
    result = send_command("get_envelope", track=track, envelope=envelope)
    if result.get("status") == "ok":
        return format_envelope(result)
    return _error_text(result)


@mcp.tool()
def reaper_set_envelope_points(
    track: str,
    envelope: str,
    points: str,
    clear_first: bool = False,
) -> str:
    """Insert automation points on a track envelope.

    Args:
        track: Track name or substring to match, or "master" for the master track
        envelope: Envelope name — one of "Volume", "Pan", "Mute", "Width".
                  The envelope must be visible on the track (press V on the track to show envelopes).
                  Value ranges: Volume 0.0–2.0 (1.0 = 0dB), Pan -1.0 to 1.0 (0 = center).
        points: JSON array of point objects. Each point has:
                - time (float): position in seconds
                - value (float): envelope value
                - shape (int, optional): 0=Linear, 1=Square, 2=Slow start/end, 3=Fast start, 4=Fast end, 5=Bezier. Default 0.
                - tension (float, optional): -1.0 to 1.0, used with Bezier shape. Default 0.
                Example: '[{"time": 176, "value": 1.0}, {"time": 180, "value": 0.0}]'
        clear_first: If true, remove all existing points before inserting new ones
    """
    try:
        points_list = json.loads(points)
    except json.JSONDecodeError as e:
        return f"Error: Invalid JSON for points: {e}"

    result = send_command(
        "set_envelope_points",
        track=track,
        envelope=envelope,
        points=points_list,
        clear_first=clear_first,
    )
    if result.get("status") == "ok":
        return format_envelope_result(result)
    return _error_text(result)


@mcp.tool()
def reaper_clear_envelope(track: str, envelope: str) -> str:
    """Remove all automation points from a track envelope.

    Args:
        track: Track name or substring to match, or "master" for the master track
        envelope: Envelope name — one of "Volume", "Pan", "Mute", "Width".
                  The envelope must be visible on the track (press V on the track to show envelopes).
    """
    result = send_command("clear_envelope", track=track, envelope=envelope)
    if result.get("status") == "ok":
        return format_envelope_result(result)
    return _error_text(result)


@mcp.tool()
def reaper_add_send(
    src_track: str,
    dest_track: str,
    send_type: str = "both",
    midi_channel: int = -1,
    audio_volume: float = 1.0,
) -> str:
    """Create a send (routing) between two tracks.

    Args:
        src_track: Source track name/substring or index
        dest_track: Destination track name/substring or index
        send_type: "audio", "midi", or "both" (default "both")
        midi_channel: MIDI channel (-1=all, 0-15 for specific). Default -1.
        audio_volume: Audio send volume in linear (1.0 = 0dB). Default 1.0.
    """
    result = send_command(
        "add_send",
        src_track=src_track,
        dest_track=dest_track,
        send_type=send_type,
        midi_channel=midi_channel,
        audio_volume=audio_volume,
    )
    if result.get("status") == "ok":
        return f"Send created (index {result.get('send_index')})"
    return _error_text(result)


@mcp.tool()
def reaper_get_sends(track: str) -> str:
    """List all sends from a track, showing destinations and routing config.

    Args:
        track: Track name/substring or index
    """
    result = send_command("get_sends", track=track)
    if result.get("status") == "ok":
        return format_sends(result)
    return _error_text(result)


@mcp.tool()
def reaper_load_sample_rs5k(
    track: str,
    sample_path: str,
    note: int = 60,
    attack: float | None = None,
    decay: float | None = None,
    sustain: float | None = None,
    release: float | None = None,
    volume: float | None = None,
    fx_index: int | None = None,
) -> str:
    """Load a sample into ReaSamplOmatic5000 on a track.

    Adds RS5k if fx_index is not provided, or updates an existing RS5k instance.
    Uses FILE0 API for reliable programmatic sample loading.

    Args:
        track: Track name/substring or index
        sample_path: Absolute path to the sample file (wav, mp3, etc.)
        note: MIDI note to trigger this sample (0-127, default 60/C4).
              GM drum defaults: Kick=36, Snare=38, HiHat=42, Crash=49, Ride=51
        attack: Attack normalized 0-1 (optional)
        decay: Decay normalized 0-1 (optional)
        sustain: Sustain normalized 0-1 (optional)
        release: Release normalized 0-1 (optional)
        volume: Volume normalized 0-1 (optional)
        fx_index: Index of existing RS5k to update (optional; adds new if omitted)
    """
    kwargs: dict = {"track": track, "sample_path": sample_path, "note": note}
    for key, val in [("attack", attack), ("decay", decay), ("sustain", sustain),
                     ("release", release), ("volume", volume), ("fx_index", fx_index)]:
        if val is not None:
            kwargs[key] = val
    result = send_command("load_sample_rs5k", **kwargs)
    status = result.get("status")
    if status == "ok" or status == "partial":
        text = f"RS5k loaded on '{result.get('track')}' (FX index {result.get('fx_index')})"
        warnings = result.get("warnings", [])
        if warnings:
            text += "\nWarnings: " + "; ".join(warnings)
        return text
    return _error_text(result)


@mcp.tool()
def reaper_setup_reagate_midi(
    track: str,
    threshold: float | None = None,
    attack: float | None = None,
    hold: float | None = None,
    release: float | None = None,
    midi_note: int | None = None,
    midi_channel: int | None = None,
    fx_index: int | None = None,
) -> str:
    """Add/configure ReaGate for MIDI triggering on a track.

    Sets up ReaGate to send MIDI notes when the gate opens (transient detection).
    All numeric params are normalized 0-1. Use reaper_get_track_fx to discover
    exact parameter ranges if needed.

    Args:
        track: Track name/substring or index
        threshold: Gate threshold normalized 0-1 (optional)
        attack: Gate attack normalized 0-1 (optional)
        hold: Gate hold normalized 0-1 (optional)
        release: Gate release normalized 0-1 (optional)
        midi_note: MIDI note to send (0-127, optional)
        midi_channel: MIDI channel (0-15, optional)
        fx_index: Index of existing ReaGate to update (optional; adds new if omitted)
    """
    kwargs: dict = {"track": track}
    for key, val in [("threshold", threshold), ("attack", attack), ("hold", hold),
                     ("release", release), ("midi_note", midi_note),
                     ("midi_channel", midi_channel), ("fx_index", fx_index)]:
        if val is not None:
            kwargs[key] = val
    result = send_command("setup_reagate_midi", **kwargs)
    status = result.get("status")
    if status == "ok" or status == "partial":
        text = f"ReaGate configured on '{result.get('track')}' (FX index {result.get('fx_index')})"
        warnings = result.get("warnings", [])
        if warnings:
            text += "\nWarnings: " + "; ".join(warnings)
        return text
    return _error_text(result)


@mcp.tool()
def reaper_set_track_folder(track: str, depth: int) -> str:
    """Set folder depth on a track. +1 = folder start, 0 = normal, -1 = last in folder.

    WARNING: Caller must ensure balanced folder depths across tracks.
    Use drum_augment for automatic folder setup.

    Args:
        track: Track name/substring or index
        depth: Folder depth value (1=folder parent, 0=normal, -1=end of folder)
    """
    result = send_command("set_track_folder", track=track, depth=depth)
    if result.get("status") == "ok":
        return f"Folder depth set to {depth} on '{result.get('track')}'"
    return _error_text(result)


@mcp.tool()
def reaper_set_track_visible(
    track: str,
    tcp: bool | None = None,
    mixer: bool | None = None,
) -> str:
    """Show or hide a track in the TCP (track control panel) and/or mixer.

    Args:
        track: Track name/substring or index
        tcp: Show in TCP (true/false, optional)
        mixer: Show in mixer (true/false, optional)
    """
    kwargs: dict = {"track": track}
    if tcp is not None:
        kwargs["tcp"] = tcp
    if mixer is not None:
        kwargs["mixer"] = mixer
    result = send_command("set_track_visible", **kwargs)
    if result.get("status") == "ok":
        return f"Visibility updated for '{result.get('track')}'"
    return _error_text(result)


@mcp.tool()
def reaper_drum_augment(
    audio_track: str,
    sample_path: str,
    note: int | None = None,
    drum_type: str | None = None,
    threshold: float | None = None,
    gate_attack: float | None = None,
    gate_hold: float | None = None,
    gate_release: float | None = None,
    attack: float | None = None,
    decay: float | None = None,
    sustain: float | None = None,
    release: float | None = None,
    volume: float | None = None,
    create_folder: bool = False,
) -> str:
    """SSD-style drum augment/replace: add a sample trigger to an audio drum track.

    Creates a complete signal chain in one atomic undo step:
    1. Adds ReaGate on the audio track (MIDI trigger on transients)
    2. Creates a new RS5k track with the sample loaded
    3. Routes MIDI from audio track -> RS5k track
    4. Optionally organizes into a folder

    The original audio is never modified — ReaGate just listens to the signal.
    Uses strict track matching (errors if name is ambiguous).

    Args:
        audio_track: Track with drum audio (name or index). Must match exactly one track.
        sample_path: Absolute path to the replacement/augment sample file
        note: Explicit MIDI note (0-127). If omitted, auto-picks based on drum_type or first unused.
        drum_type: Drum type hint for auto-note selection: "kick", "snare", "hihat", "crash", "ride", etc.
        threshold: ReaGate threshold normalized 0-1 (optional)
        gate_attack: ReaGate attack normalized 0-1 (optional)
        gate_hold: ReaGate hold normalized 0-1 (optional)
        gate_release: ReaGate release normalized 0-1 (optional)
        attack: RS5k attack normalized 0-1 (optional)
        decay: RS5k decay normalized 0-1 (optional)
        sustain: RS5k sustain normalized 0-1 (optional)
        release: RS5k release normalized 0-1 (optional)
        volume: RS5k volume normalized 0-1 (optional)
        create_folder: If true, organize audio + RS5k tracks into a folder (default false)
    """
    kwargs: dict = {"audio_track": audio_track, "sample_path": sample_path}
    for key, val in [
        ("note", note), ("drum_type", drum_type),
        ("threshold", threshold), ("gate_attack", gate_attack),
        ("gate_hold", gate_hold), ("gate_release", gate_release),
        ("attack", attack), ("decay", decay), ("sustain", sustain),
        ("release", release), ("volume", volume), ("create_folder", create_folder),
    ]:
        if val is not None:
            kwargs[key] = val
    result = send_command("drum_augment", **kwargs)
    status = result.get("status")
    if status in ("ok", "partial"):
        return format_drum_augment(result)
    return _error_text(result)


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
