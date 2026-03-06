"""MCP server for REAPER AI — exposes reaper-ai tools over stdio transport."""

import json

from mcp.server.fastmcp import FastMCP

from bridge.ipc import send_command
from bridge.reaper_state import (
    format_apply_result,
    format_auto_eq_result,
    format_context,
    format_drum_augment,
    format_envelope,
    format_envelope_result,
    format_presets,
    format_sends,
    format_spectral_analysis,
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
def reaper_set_param_display(track: str, fx_index: int, params: str) -> str:
    """Set FX parameters by target display value instead of normalized 0-1.

    The daemon uses binary search with REAPER's FormatParamValueNormalized
    to find the correct normalized value for the requested display value.
    This is more accurate than guessing normalized values for unknown scales.

    Use this when you know the target in real units (Hz, dB, ms, seconds, %)
    but not the normalized mapping. Falls back to sampled lookup for
    non-monotonic or enum/text parameters.

    Args:
        track: Track name or substring to match
        fx_index: Index of the FX plugin on the track (0-based)
        params: JSON string of parameter name/display_value pairs,
                e.g. '{"Decay Time": 3.0, "High Frequency": 8000}'
                Values can be numeric (for Hz, dB, ms, etc.) or strings (for enum params).
    """
    try:
        param_dict = json.loads(params)
    except json.JSONDecodeError as e:
        return f"Error: Invalid JSON for params: {e}"

    param_list = [{"name": k, "display_value": v} for k, v in param_dict.items()]
    result = send_command("set_param_display", track=track, fx_index=fx_index, params=param_list)
    if result.get("status") in ("ok", "partial"):
        lines = [f"Status: {result['status']}, applied: {result.get('applied', 0)}"]
        for c in result.get("confirmed", []):
            lines.append(f"  {c['name']}: requested={c.get('requested_display','?')}, "
                         f"actual={c.get('actual_display','?')}, norm={c.get('normalized',0):.4f}")
        for e in result.get("errors", []):
            lines.append(f"  Error: {e}")
        return "\n".join(lines)
    return _error_text(result)


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
def reaper_get_fx_named_config(track: str, fx_index: int, keys: str) -> str:
    """Read one or more plugin named-config keys from an FX instance.

    Useful for plugin-specific state that is not exposed as regular automatable
    parameters (for example some VST chunk-backed settings).

    Args:
        track: Track name or substring to match
        fx_index: FX index on the track (0-based)
        keys: JSON array of key names, e.g. '["fx_ident","vst_chunk"]'
    """
    try:
        key_list = json.loads(keys)
    except json.JSONDecodeError as e:
        return f"Error: Invalid JSON for keys: {e}"
    if not isinstance(key_list, list) or not key_list:
        return "Error: keys must be a non-empty JSON array"

    result = send_command("get_fx_named_config", track=track, fx_index=fx_index, names=key_list)
    status = result.get("status", "unknown")
    lines = [f"Status: {status}", f"Track: {result.get('track', track)}", f"FX index: {result.get('fx_index', fx_index)}"]
    for entry in result.get("values", []):
        key = entry.get("name", "?")
        found = entry.get("found", False)
        value = str(entry.get("value", ""))
        if len(value) > 180:
            value = value[:180] + f"... (len={len(str(entry.get('value', '')))})"
        lines.append(f"  {key} [{'ok' if found else 'missing'}]: {value}")
    for err in result.get("errors", []):
        lines.append(f"  Error: {err}")
    return "\n".join(lines)


@mcp.tool()
def reaper_set_fx_named_config(track: str, fx_index: int, params: str) -> str:
    """Set one or more plugin named-config keys on an FX instance.

    Args:
        track: Track name or substring to match
        fx_index: FX index on the track (0-based)
        params: JSON object of key/value pairs, e.g. '{"vst_chunk":"..."}'
    """
    try:
        param_dict = json.loads(params)
    except json.JSONDecodeError as e:
        return f"Error: Invalid JSON for params: {e}"
    if not isinstance(param_dict, dict) or not param_dict:
        return "Error: params must be a non-empty JSON object"

    param_list = [{"name": str(k), "value": str(v)} for k, v in param_dict.items()]
    result = send_command("set_fx_named_config", track=track, fx_index=fx_index, params=param_list)
    status = result.get("status", "unknown")
    lines = [f"Status: {status}", f"Track: {result.get('track', track)}", f"FX index: {result.get('fx_index', fx_index)}",
             f"Applied: {result.get('applied', 0)}"]
    for entry in result.get("confirmed", []):
        key = entry.get("name", "?")
        value = str(entry.get("value", ""))
        if len(value) > 120:
            value = value[:120] + f"... (len={len(str(entry.get('value', '')))})"
        lines.append(f"  {key}: {value} ({'verified' if entry.get('verified') else 'unverified'})")
    for err in result.get("errors", []):
        lines.append(f"  Error: {err}")
    return "\n".join(lines)


@mcp.tool()
def reaper_apply_plan(track: str, plan: str) -> str:
    """Apply a multi-step FX plan to a track. The plan is a JSON object with a
    title and an array of steps. Each step can add FX, set parameters, or set presets.
    All steps execute in a single undo block.

    IMPORTANT: When a plan boosts or cuts gain at any stage, include compensating
    gain staging to maintain proper headroom. Inform the user of all level changes.

    IMPORTANT: ReaEQ High Pass 5 — only set Freq-High Pass 5. Always leave
    Gain-High Pass 5 at 0.5 (0.0 dB). The gain param does NOT control slope;
    any other value adds an unwanted boost/cut.

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
def reaper_set_send_volume(
    track: str,
    send_index: int,
    volume_db: float | None = None,
    volume: float | None = None,
) -> str:
    """Set the volume of a send on a track.

    Provide either volume_db (in dB, e.g. -12.0) or volume (linear, 1.0 = 0 dB).

    Args:
        track: Track name/substring or index
        send_index: Send index (0-based, from reaper_get_sends)
        volume_db: Volume in dB (preferred)
        volume: Volume as linear scalar (1.0 = 0 dB)
    """
    kwargs: dict = {"track": track, "send_index": send_index}
    if volume_db is not None:
        kwargs["volume_db"] = volume_db
    elif volume is not None:
        kwargs["volume"] = volume
    else:
        return "Error: provide volume_db or volume"
    result = send_command("set_send_volume", **kwargs)
    if result.get("status") == "ok":
        db = result.get("volume_db", 0)
        dest = result.get("dest_track", "?")
        return f"Send {send_index} -> '{dest}' set to {db:.1f} dB"
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
def reaper_enable_reaeq_band(
    track: str,
    fx_index: int,
    band: int,
    enabled: bool,
) -> str:
    """Enable or disable a specific ReaEQ band. ReaEQ band enable state is NOT
    exposed as a standard parameter — this uses BANDENABLED via SetNamedConfigParm.

    Band indices: 0=Low Shelf, 1=Band 2, 2=Band 3, 3=High Shelf 4, 4=High Pass 5.

    Args:
        track: Track name/substring or index
        fx_index: ReaEQ FX index on the track
        band: Band index (0-4)
        enabled: True to enable, False to disable
    """
    result = send_command(
        "enable_reaeq_band", track=track, fx_index=fx_index,
        band=band, enabled=enabled,
    )
    if result.get("status") == "ok":
        state = "enabled" if enabled else "disabled"
        return f"Band {result.get('band')} {state} on '{result.get('track')}' fx {result.get('fx_index')}"
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
def reaper_rename_track(track_index: int, name: str) -> str:
    """Rename a track by strict numeric index.

    Args:
        track_index: Track index (0-based)
        name: New track name
    """
    result = send_command("rename_track", track=track_index, name=name)
    if result.get("status") == "ok":
        return (
            f"Renamed track [{result.get('index')}] "
            f"'{result.get('old_name')}' -> '{result.get('track')}'"
        )
    return _error_text(result)


@mcp.tool()
def reaper_set_track_color(
    track_index: int,
    color: str | None = None,
    clear: bool = False,
) -> str:
    """Set or clear a track color by strict numeric index.

    Args:
        track_index: Track index (0-based)
        color: Hex color in #RRGGBB format (ignored when clear=True)
        clear: If true, clear custom color
    """
    kwargs: dict = {"track": track_index}
    if clear:
        kwargs["clear"] = True
    else:
        if not color:
            return "Error: Provide color (#RRGGBB) or set clear=true"
        kwargs["color"] = color
    result = send_command("set_track_color", **kwargs)
    if result.get("status") == "ok":
        if result.get("cleared"):
            return f"Cleared color on track [{result.get('index')}] '{result.get('track')}'"
        return (
            f"Set color on track [{result.get('index')}] "
            f"'{result.get('track')}' -> {result.get('color')}"
        )
    return _error_text(result)


@mcp.tool()
def reaper_reorder_track(track_index: int, to_index: int) -> str:
    """Move a track to a destination index.

    Args:
        track_index: Source track index (0-based)
        to_index: Destination track index (0-based)
    """
    result = send_command("reorder_track", track=track_index, to_index=to_index)
    if result.get("status") == "ok":
        if result.get("moved", True):
            return (
                f"Moved '{result.get('track')}' from "
                f"[{result.get('from_index')}] to [{result.get('to_index')}]"
            )
        return (
            f"No move needed for '{result.get('track')}' "
            f"(already at [{result.get('to_index')}])"
        )
    return _error_text(result)


@mcp.tool()
def reaper_insert_media(
    track: str,
    file_path: str,
    position: float | None = None,
) -> str:
    """Insert a media file (wav, mp3, etc.) onto a track as a new media item.

    Args:
        track: Track name or index to insert onto.
        file_path: Absolute path to the media file.
        position: Position in seconds (default 0, start of project).
    """
    kwargs: dict = {"track": track, "file_path": file_path}
    if position is not None:
        kwargs["position"] = position
    result = send_command("insert_media", **kwargs)
    if result.get("status") == "ok":
        return (
            f"Inserted '{result.get('file_path')}' on '{result.get('track')}' "
            f"at {result.get('position', 0):.1f}s ({result.get('length', 0):.1f}s long)"
        )
    return _error_text(result)


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


@mcp.tool()
def reaper_analyze_tracks(
    tracks: str,
    time_start: float | None = None,
    time_end: float | None = None,
) -> str:
    """Analyze the spectral content (frequency energy) of one or more tracks.

    Returns per-band average energy in dB for 10 frequency bands (20Hz-20kHz),
    with separate L/R channel data for stereo-aware mixing decisions.

    This is a slow operation — each track takes several seconds to analyze.
    The REAPER UI stays responsive during analysis.

    Args:
        tracks: Comma-separated track names to analyze (e.g. "Vocals,Guitar,Bass")
        time_start: Analysis start time in seconds (default: project start)
        time_end: Analysis end time in seconds (default: project end)
    """
    from bridge.auto_eq import analyze_track

    track_list = [t.strip() for t in tracks.split(",") if t.strip()]
    if not track_list:
        return "Error: No track names provided"

    results = []
    for name in track_list:
        result = analyze_track(name, time_start, time_end)
        if result.get("status") == "ok":
            results.append(format_spectral_analysis(result))
        else:
            errors = result.get("errors", ["Unknown error"])
            results.append(f"Error analyzing '{name}': {'; '.join(str(e) for e in errors)}")

    return "\n\n".join(results)


@mcp.tool()
def reaper_auto_eq(
    priority_track: str,
    yield_tracks: str,
    max_cut_db: float = -6.0,
    aggressiveness: float = 1.5,
    max_cuts: int = 5,
    makeup_mode: str = "auto",
    time_start: float | None = None,
    time_end: float | None = None,
) -> str:
    """Automatically EQ tracks to reduce frequency masking.

    Analyzes spectral content of the priority track and yield tracks, then
    applies corrective EQ cuts (via ReaEQ) to the yield tracks so the
    priority track is heard more clearly.

    Example: Make vocals cut through a guitar and keys mix:
      priority_track="Vocals", yield_tracks="Guitar,Keys"

    The applied ReaEQ is tagged [AutoEQ] for easy identification. Running
    again replaces the previous auto-EQ (idempotent).

    Args:
        priority_track: Track name that should be heard clearly (e.g. "Vocals")
        yield_tracks: Comma-separated track names to EQ (e.g. "Guitar,Keys,Bass")
        max_cut_db: Maximum EQ cut in dB, negative (default -6.0, range -1 to -12)
        aggressiveness: How aggressive to cut (0.5=gentle, 1.0=normal, 1.5=strong, 2.0=aggressive)
        max_cuts: Maximum number of EQ cuts applied per yield track (default 5)
        makeup_mode: "off" or "auto" selective makeup on uncut bands (default "auto")
        time_start: Analysis start time in seconds (default: project start)
        time_end: Analysis end time in seconds (default: project end)
    """
    from bridge.auto_eq import auto_eq

    yield_list = [t.strip() for t in yield_tracks.split(",") if t.strip()]
    if not yield_list:
        return "Error: No yield track names provided"

    result = auto_eq(
        priority_track=priority_track,
        yield_tracks=yield_list,
        max_cut_db=max_cut_db,
        aggressiveness=aggressiveness,
        max_cuts=max_cuts,
        makeup_mode=makeup_mode,
        time_start=time_start,
        time_end=time_end,
    )
    return format_auto_eq_result(result)


@mcp.tool()
def reaper_auto_eq_all(
    max_cut_db: float = -3.0,
    aggressiveness: float = 1.0,
    max_cuts: int = 2,
    makeup_mode: str = "off",
    level: str = "leaf",
    time_start: float | None = None,
    time_end: float | None = None,
    strategy: str = "subtractive",
    family: str = "all",
    max_boost_db: float = 6.0,
    role_overrides_json: str | None = None,
    role_hints_json: str | None = None,
) -> str:
    """Automatically EQ all tracks in the project using priority hierarchy.

    Assigns priority scores to tracks based on their names (e.g. vocals=highest,
    kick/snare=high, bass=medium-high, guitar/keys/synth=medium, pads=low).
    Each track yields spectral space to all tracks with higher priority.

    This is a slow operation — analyzes every track pair.

    Args:
        max_cut_db: Maximum EQ cut in dB, negative (default -3.0, range -1 to -12)
        aggressiveness: How aggressive to cut (0.5=gentle, 1.0=normal, 2.0=aggressive)
        max_cuts: Maximum number of EQ cuts applied per track (default 2)
        makeup_mode: "off" or "auto" selective makeup on uncut bands (default "off")
        level: Target selection level: "auto", "leaf", or "bus" (default "leaf")
        time_start: Analysis start time in seconds (default: project start)
        time_end: Analysis end time in seconds (default: project end)
        strategy: "subtractive" (cuts only) or "hybrid" (cuts + family boosts)
        family: Restrict hybrid boosts to family: "all", "guitar", "keys-synth", "vocal"
        max_boost_db: Maximum complementary boost in dB (default 6.0, hybrid only)
        role_overrides_json: JSON object mapping track name -> role (hard lock)
        role_hints_json: JSON object mapping track name -> role (soft nudge)
    """
    from bridge.auto_eq import auto_eq_all

    overrides: dict[str, str] = {}
    if role_overrides_json:
        try:
            parsed = json.loads(role_overrides_json)
            if isinstance(parsed, dict):
                overrides = {str(k): str(v) for k, v in parsed.items()}
            else:
                return "Error: role_overrides_json must be a JSON object mapping track->role"
        except json.JSONDecodeError as e:
            return f"Error: Invalid JSON for role_overrides_json: {e}"

    hints: dict[str, str] = {}
    if role_hints_json:
        try:
            parsed = json.loads(role_hints_json)
            if isinstance(parsed, dict):
                hints = {str(k): str(v) for k, v in parsed.items()}
            else:
                return "Error: role_hints_json must be a JSON object mapping track->role"
        except json.JSONDecodeError as e:
            return f"Error: Invalid JSON for role_hints_json: {e}"

    result = auto_eq_all(
        max_cut_db=max_cut_db,
        aggressiveness=aggressiveness,
        max_cuts=max_cuts,
        makeup_mode=makeup_mode,
        level=level,
        time_start=time_start,
        time_end=time_end,
        strategy=strategy,
        family=family,
        max_boost_db=max_boost_db,
        role_overrides=overrides or None,
        role_hints=hints or None,
    )
    return format_auto_eq_result(result)


@mcp.tool()
def reaper_auto_eq_compl(
    family: str = "all",
    level: str = "leaf",
    max_cut_db: float = -3.0,
    max_boost_db: float = 6.0,
    max_moves: int = 4,
    role_overrides: str | None = None,
    role_hints_json: str | None = None,
    time_start: float | None = None,
    time_end: float | None = None,
) -> str:
    """Complementary Auto-EQ for same-family layering (guitar / keys-synth).

    This mode does lane-shaping inside instrument families rather than only
    ducking tracks against higher-priority instruments.

    Args:
        family: "all", "guitar", or "keys-synth" (default "all")
        level: Target selection level: "auto", "leaf", or "bus" (default "leaf")
        max_cut_db: Maximum cut in dB (negative, default -3)
        max_boost_db: Maximum boost in dB (default +6)
        max_moves: Maximum total EQ moves per track (default 4)
        role_overrides: Optional JSON object mapping track name -> role
                        (anchor|presence|texture|support)
        role_hints_json: Optional JSON object mapping track name -> role (soft nudge)
        time_start: Analysis start time in seconds (default: project start)
        time_end: Analysis end time in seconds (default: project end)
    """
    from bridge.auto_eq import auto_eq_compl

    overrides: dict[str, str] = {}
    if role_overrides:
        try:
            parsed = json.loads(role_overrides)
            if isinstance(parsed, dict):
                overrides = {str(k): str(v) for k, v in parsed.items()}
            else:
                return "Error: role_overrides must be a JSON object mapping track->role"
        except json.JSONDecodeError as e:
            return f"Error: Invalid JSON for role_overrides: {e}"

    hints: dict[str, str] = {}
    if role_hints_json:
        try:
            parsed = json.loads(role_hints_json)
            if isinstance(parsed, dict):
                hints = {str(k): str(v) for k, v in parsed.items()}
            else:
                return "Error: role_hints_json must be a JSON object mapping track->role"
        except json.JSONDecodeError as e:
            return f"Error: Invalid JSON for role_hints_json: {e}"

    result = auto_eq_compl(
        family=family,
        level=level,
        max_cut_db=max_cut_db,
        max_boost_db=max_boost_db,
        max_moves=max_moves,
        role_overrides=overrides,
        role_hints=hints or None,
        time_start=time_start,
        time_end=time_end,
    )
    return format_auto_eq_result(result)


@mcp.tool()
def reaper_auto_eq_sections(
    sections: str = "[]",
    max_cut_db: float = -6.0,
    aggressiveness: float = 1.5,
    max_cuts: int = 5,
    level: str = "leaf",
    strategy: str = "hybrid",
    family: str = "all",
    max_boost_db: float = 6.0,
    write_mode: str = "auto",
    hybrid_selective_makeup: bool = False,
) -> str:
    """Per-section auto-EQ with automation envelopes.

    Analyzes different song sections independently and writes gain automation
    envelopes on ReaEQ so the EQ curve changes automatically at section
    boundaries. Each section is a time window analyzed separately.

    Args:
        sections: JSON array of [label, start, end] tuples, e.g.
                  '[["verse",0,32],["chorus",32,64],["bridge",180,210]]'
                  Maximum 20 sections, 600s total analyzed duration.
        max_cut_db: Maximum cut depth in dB (negative, default -6)
        aggressiveness: Cut aggressiveness factor (default 1.5)
        max_cuts: Maximum cuts per section per track (default 5)
        level: Target selection: "auto", "leaf", or "bus" (default "leaf")
        strategy: EQ strategy — "subtractive" (cuts only) or "hybrid" (cuts + family boosts, default)
        family: Restrict hybrid boosts to family ("guitar", "keys-synth", or "all")
        max_boost_db: Maximum boost in dB for hybrid mode (default 6)
        write_mode: "replace" (wipe existing), "merge" (incremental), "auto" (detect, default)
        hybrid_selective_makeup: Allow selective makeup in hybrid strategy (default false)
    """
    from bridge.auto_eq import auto_eq_sections

    if write_mode not in ("replace", "merge", "auto"):
        return f"Error: write_mode must be 'replace', 'merge', or 'auto' (got {write_mode!r})"

    try:
        parsed = json.loads(sections)
    except json.JSONDecodeError as e:
        return f"Error: Invalid JSON for sections: {e}"

    if not isinstance(parsed, list):
        return "Error: sections must be a JSON array"

    section_tuples: list[tuple[str, float, float]] = []
    for i, item in enumerate(parsed):
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            return f"Error: section {i} must be [label, start, end]"
        label, start, end = item
        if not isinstance(label, str):
            return f"Error: section {i} label must be a string"
        try:
            start = float(start)
            end = float(end)
        except (TypeError, ValueError):
            return f"Error: section {i} start/end must be numbers"
        section_tuples.append((label, start, end))

    result = auto_eq_sections(
        sections=section_tuples,
        max_cut_db=max_cut_db,
        aggressiveness=aggressiveness,
        max_cuts=max_cuts,
        level=level,
        strategy=strategy,
        family=family,
        max_boost_db=max_boost_db,
        write_mode=write_mode,
        hybrid_selective_makeup=hybrid_selective_makeup,
    )
    return format_auto_eq_result(result)


@mcp.tool()
def reaper_recover_autoeq() -> str:
    """Re-enable AutoEQ FX after a crash or interruption left them bypassed.

    Checks for a persisted preflight snapshot and restores exact FX states,
    or falls back to enabling all [AutoEQ] and [AutoEQ-Comp] instances.
    Run this after a BSOD, power loss, or any interrupted auto-eq session.
    """
    from bridge.auto_eq import recover_autoeq

    result = recover_autoeq()
    method = result.get("method", "unknown")
    errors = result.get("errors", [])
    lines = []
    if method == "snapshot_restore":
        lines.append(f"Restored {result.get('restored', 0)} FX state(s) from crash-recovery snapshot")
    else:
        lines.append(f"Re-enabled {result.get('toggled', 0)} AutoEQ instance(s) via tag scan")
        for d in result.get("details", []):
            lines.append(f"  Track {d['track_index']}: {d['track']} [{d['fx_index']}] {d['fx_name']}")
    if errors:
        lines.append("Warnings:")
        for e in errors:
            lines.append(f"  {e}")
    return "\n".join(lines)


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
