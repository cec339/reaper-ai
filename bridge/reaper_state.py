"""Format REAPER state responses for display."""


def format_context(data: dict) -> str:
    """Format get_context response into readable text."""
    lines = []

    tracks = data.get("tracks", [])
    if not tracks:
        lines.append("No tracks found in REAPER project.")
    else:
        lines.append(f"Tracks ({len(tracks)}):")
        lines.append("-" * 50)
        for t in tracks:
            sel = " [SELECTED]" if t.get("selected") else ""
            fx_list = t.get("fx", [])
            fx_str = f"  FX: {', '.join(fx_list)}" if fx_list else "  FX: (none)"
            lines.append(f"  {t['index']:>2}. {t['name']}{sel}")
            lines.append(fx_str)

    installed = data.get("installed_fx", [])
    if installed:
        lines.append("")
        lines.append(f"Installed FX ({len(installed)}):")
        lines.append("-" * 50)
        for name in installed:
            lines.append(f"  {name}")

    return "\n".join(lines)


def format_track_fx(data: dict) -> str:
    """Format get_track_fx response into readable text."""
    lines = []
    track = data.get("track", "?")
    chain = data.get("fx_chain", [])

    if not chain:
        lines.append(f"Track '{track}' has no FX.")
        return "\n".join(lines)

    lines.append(f"FX Chain for '{track}' ({len(chain)} plugins):")
    lines.append("=" * 60)

    for fx in chain:
        lines.append(f"\n  [{fx['index']}] {fx['name']}")
        lines.append("  " + "-" * 40)
        params = fx.get("params", [])
        for p in params:
            disp = p.get("display", "")
            if disp:
                lines.append(f"    {p['index']:>3}. {p['name']}: {p['value']:.4f} ({disp})")
            else:
                lines.append(f"    {p['index']:>3}. {p['name']}: {p['value']:.4f}")

    return "\n".join(lines)


def format_presets(data: dict) -> str:
    """Format list_presets response into readable text."""
    lines = []
    track = data.get("track", "?")
    fx_name = data.get("fx_name", "?")
    current = data.get("current_preset", "")
    presets = data.get("presets", [])

    lines.append(f"Presets for '{fx_name}' on '{track}':")
    if current:
        lines.append(f"  Current: {current}")
    lines.append("=" * 50)

    if not presets:
        lines.append("  (no presets available)")
    else:
        for p in presets:
            marker = " <--" if p["name"] == current else ""
            lines.append(f"  {p['index']:>3}. {p['name']}{marker}")

    return "\n".join(lines)


SHAPE_NAMES = {0: "Linear", 1: "Square", 2: "Slow", 3: "Fast start", 4: "Fast end", 5: "Bezier"}


def _vol_to_db(val: float) -> str:
    """Convert linear amplitude to dB string."""
    if val <= 0:
        return "-inf dB"
    import math
    return f"{20 * math.log10(val):.1f} dB"


def format_envelope(data: dict) -> str:
    """Format get_envelope response into readable text."""
    lines = []
    track = data.get("track", "?")
    env = data.get("envelope", "?")
    points = data.get("points", [])
    is_vol = "vol" in env.lower()

    lines.append(f"Envelope '{env}' on '{track}' ({len(points)} points):")
    lines.append("=" * 50)

    if not points:
        lines.append("  (no points)")
    else:
        for p in points:
            shape = SHAPE_NAMES.get(p.get("shape", 0), str(p.get("shape", 0)))
            val = p['value']
            val_str = f"{val:.4f}"
            if is_vol:
                val_str += f" ({_vol_to_db(val)})"
            t_str = f"  {p['index']:>3}. t={p['time']:.3f}s  val={val_str}  shape={shape}"
            lines.append(t_str)

    return "\n".join(lines)


def format_envelope_result(data: dict) -> str:
    """Format set_envelope_points or clear_envelope response."""
    track = data.get("track", "?")
    env = data.get("envelope", "?")

    if "points_added" in data:
        return f"Added {data['points_added']} points to {env} on '{track}'"
    if "points_removed" in data:
        return f"Removed {data['points_removed']} points from {env} on '{track}'"
    return f"Envelope operation on {env} on '{track}': ok"


def format_apply_result(data: dict) -> str:
    """Format apply_plan response."""
    status = data.get("status", "unknown")
    applied = data.get("applied", 0)
    errors = data.get("errors", [])
    track = data.get("track", "?")

    lines = [f"Track: {track}", f"Status: {status}", f"Steps applied: {applied}"]

    confirmed = data.get("confirmed", [])
    if confirmed:
        lines.append("Confirmed params:")
        for c in confirmed:
            disp = c.get("display", "")
            if disp:
                lines.append(f"  {c['name']}: {c['value']:.4f} ({disp})")
            else:
                lines.append(f"  {c['name']}: {c['value']:.4f}")

    if errors:
        lines.append(f"Errors ({len(errors)}):")
        for e in errors:
            lines.append(f"  - {e}")

    return "\n".join(lines)


def format_sends(data: dict) -> str:
    """Format get_sends response into readable text."""
    lines = []
    track = data.get("track", "?")
    sends = data.get("sends", [])

    if not sends:
        lines.append(f"Track '{track}' has no sends.")
        return "\n".join(lines)

    lines.append(f"Sends from '{track}' ({len(sends)}):")
    lines.append("=" * 50)

    for s in sends:
        src_chan = s.get("src_chan", 0)
        midi_flags = s.get("midi_flags", 0)
        vol = s.get("volume", 1.0)

        send_type = "both"
        if src_chan == -1:
            send_type = "midi-only"
        elif midi_flags == 31:
            send_type = "audio-only"

        vol_db = _vol_to_db(vol)
        lines.append(
            f"  [{s['index']}] -> '{s.get('dest_track', '?')}' "
            f"({send_type}, vol={vol_db})"
        )

    return "\n".join(lines)


def format_drum_augment(data: dict) -> str:
    """Format drum_augment response into readable text."""
    lines = []
    status = data.get("status", "unknown")

    lines.append(f"Status: {status}")
    lines.append(f"Audio track: {data.get('audio_track', '?')}")
    lines.append(f"RS5k track: {data.get('rs5k_track', '?')} (index {data.get('rs5k_track_index', '?')})")
    lines.append(f"MIDI note: {data.get('midi_note', '?')}")

    if data.get("reagate_fx_index") is not None:
        lines.append(f"ReaGate FX index: {data['reagate_fx_index']}")
    if data.get("rs5k_fx_index") is not None:
        lines.append(f"RS5k FX index: {data['rs5k_fx_index']}")
    if data.get("send_index") is not None:
        lines.append(f"Send index: {data['send_index']}")

    warnings = data.get("warnings", [])
    if warnings:
        lines.append(f"Warnings ({len(warnings)}):")
        for w in warnings:
            lines.append(f"  - {w}")

    return "\n".join(lines)
