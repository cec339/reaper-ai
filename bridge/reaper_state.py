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


def format_apply_result(data: dict) -> str:
    """Format apply_plan response."""
    status = data.get("status", "unknown")
    applied = data.get("applied", 0)
    errors = data.get("errors", [])
    track = data.get("track", "?")

    lines = [f"Track: {track}", f"Status: {status}", f"Steps applied: {applied}"]

    if errors:
        lines.append(f"Errors ({len(errors)}):")
        for e in errors:
            lines.append(f"  - {e}")

    return "\n".join(lines)
