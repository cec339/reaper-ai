"""Format REAPER state responses for display."""

BAND_LABELS = [
    "Sub", "Bass", "Low-Mid", "Lower-Mid", "Mid", "Upper-Mid",
    "Low Presence", "High Presence", "Brilliance", "Air",
]


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
            req = c.get("requested", c.get("value"))
            actual = c.get("value", req)
            drift = abs(actual - req) if isinstance(actual, (int, float)) and isinstance(req, (int, float)) else 0
            warn = "  ** DRIFT" if drift > 0.005 else ""
            if disp:
                lines.append(f"  {c['name']}: {disp}{warn}")
            else:
                lines.append(f"  {c['name']}: {actual:.4f}{warn}")

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


def format_spectral_analysis(data: dict) -> str:
    """Format analyze_track response into a readable spectral table."""
    lines = []
    track = data.get("track", "?")
    bands = data.get("bands", [])
    frames = data.get("frames", 0)
    sr = data.get("sample_rate", 0)

    lines.append(f"Spectral Analysis: '{track}'")
    lines.append(f"  Frames: {frames}, Sample Rate: {sr}")
    lines.append("=" * 60)
    lines.append(f"  {'Band':<12} {'Range':>14}  {'Mono dB':>8}  {'L dB':>8}  {'R dB':>8}")
    lines.append("  " + "-" * 56)

    for i, band in enumerate(bands):
        label = BAND_LABELS[i] if i < len(BAND_LABELS) else f"Band {i}"
        lo = band.get("lo", 0)
        hi = band.get("hi", 0)
        avg = band.get("avg_db", -120)
        avg_l = band.get("avg_db_l", -120)
        avg_r = band.get("avg_db_r", -120)
        freq_range = f"{lo:>5}-{hi:<5} Hz"
        lines.append(
            f"  {label:<12} {freq_range:>14}  {avg:>8.1f}  {avg_l:>8.1f}  {avg_r:>8.1f}"
        )

    return "\n".join(lines)


def format_auto_eq_result(data: dict) -> str:
    """Format auto_eq result into a summary of cuts per track."""
    lines = []
    status = data.get("status", "unknown")
    priority = data.get("priority_track", "")
    mode = data.get("mode", "")

    if mode == "auto_eq_sections":
        sec_summaries = data.get("sections", [])
        tw = data.get("tracks_written", 0)
        strat = data.get("strategy", "subtractive")
        lines.append(
            f"Section EQ automation written: {len(sec_summaries)} sections, "
            f"{tw} tracks (strategy: {strat})"
        )
        for ss in sec_summaries:
            lines.append(
                f"  {ss.get('label', '?')} "
                f"({ss.get('start', 0):.0f}s-{ss.get('end', 0):.0f}s): "
                f"{ss.get('eq_count', 0)} tracks EQ'd"
            )
    elif mode == "auto_eq_compl":
        family = data.get("family_requested", "all")
        lines.append(f"Auto-EQ Complementary (family: '{family}')")
    elif priority:
        lines.append(f"Auto-EQ (priority: '{priority}')")
    else:
        strat = data.get("strategy", "subtractive")
        if strat == "hybrid":
            lines.append("Auto-EQ All (hybrid: cuts + complementary boosts)")
        else:
            lines.append("Auto-EQ All")
        resolved_level = data.get("level_resolved")
        if resolved_level:
            lines.append(f"Level: {resolved_level}")
    lines.append(f"Status: {status}")
    audit_path = data.get("audit_path")
    if audit_path:
        lines.append(f"Audit: {audit_path}")
    preflight = data.get("preflight")
    if isinstance(preflight, dict):
        lines.append(
            "Preflight: "
            f"snapshot={preflight.get('snapshot_count', 0)}, "
            f"bypassed={preflight.get('bypassed_total', 0)}, "
            f"restored={preflight.get('restored_applied', 0)}, "
            f"status={preflight.get('status', 'unknown')}"
        )
    lines.append("=" * 60)

    for r in data.get("results", []):
        track = r.get("track", "?")
        cuts = r.get("cuts", [])
        boosts = r.get("boosts", [])
        msg = r.get("message", "")
        moves = r.get("moves", [])
        merge_conflicts = r.get("merge_conflicts", [])
        family = r.get("family")
        role = r.get("role")

        higher_priority = r.get("higher_priority_tracks", r.get("yielding_to", []))
        priority_line = (
            f"    Higher-priority refs: {', '.join(higher_priority)}"
            if higher_priority else ""
        )

        if msg:
            lines.append(f"\n  {track}: {msg}")
        elif moves:
            lines.append(f"\n  {track}:")
            if family or role:
                lines.append(f"    Family/Role: {family or '?'} / {role or '?'}")
            if priority_line:
                lines.append(priority_line)
            for m in moves:
                lo = m.get("lo", 0)
                hi = m.get("hi", 0)
                db = m.get("gain_db", 0.0)
                bi = m.get("band_index", 0)
                label = BAND_LABELS[bi] if bi < len(BAND_LABELS) else f"Band {bi}"
                action = "cut" if db < 0 else "boost"
                contributors = m.get("contributors", [])
                contrib_suffix = (
                    f" (contributors: {', '.join(contributors)})"
                    if contributors and action == "cut" else ""
                )
                lines.append(
                    f"    {label:<12} {lo:>5}-{hi:<5} Hz  {db:>+5.1f} dB ({action}){contrib_suffix}"
                )
            if merge_conflicts:
                lines.append(
                    f"    Note: {len(merge_conflicts)} move(s) collapsed by 10->5 band mapping; see audit"
                )
        elif cuts or boosts:
            lines.append(f"\n  {track}:")
            if priority_line:
                lines.append(priority_line)
            for c in cuts:
                lo = c.get("lo", 0)
                hi = c.get("hi", 0)
                db = c.get("cut_db", 0)
                bi = c.get("band_index", 0)
                label = BAND_LABELS[bi] if bi < len(BAND_LABELS) else f"Band {bi}"
                contributors = c.get("contributors", [])
                contrib_suffix = (
                    f"  (contributors: {', '.join(contributors)})"
                    if contributors else ""
                )
                lines.append(
                    f"    {label:<12} {lo:>5}-{hi:<5} Hz  {db:>+.1f} dB{contrib_suffix}"
                )
            for b in boosts:
                bi = b.get("band_index", 0)
                label = BAND_LABELS[bi] if bi < len(BAND_LABELS) else f"Band {bi}"
                db = b.get("gain_db", 0)
                lines.append(
                    f"    {label:<12} {db:>+.1f} dB  (boost, pressure-scaled)"
                )
        else:
            lines.append(f"\n  {track}: No cuts applied")

        apply_status = r.get("apply_status", "")
        if apply_status:
            lines.append(f"    Apply status: {apply_status}")
        makeup_db = r.get("makeup_applied_db", r.get("makeup_db", 0.0))
        if isinstance(makeup_db, (int, float)) and makeup_db > 0:
            lines.append(f"    Makeup gain: +{makeup_db:.1f} dB")
        makeup_unapplied = r.get("makeup_unapplied_db", 0.0)
        if isinstance(makeup_unapplied, (int, float)) and makeup_unapplied > 0.05:
            lines.append(f"    Makeup capped: +{makeup_unapplied:.1f} dB unapplied")
        makeup_policy = r.get("makeup_policy")
        if makeup_policy and makeup_policy not in {"off", ""}:
            lines.append(f"    Makeup policy: {makeup_policy}")
        apply_errors = r.get("apply_errors", [])
        if apply_errors:
            lines.append("    Apply errors:")
            for e in apply_errors:
                lines.append(f"      - {e}")

    # Complementary boost results (hybrid auto-eq-all)
    compl_results = data.get("compl_results", [])
    if compl_results:
        lines.append("")
        lines.append("Complementary Boosts:")
        lines.append("-" * 40)
        for r in compl_results:
            track = r.get("track", "?")
            fam = r.get("family", "?")
            role = r.get("role", "?")
            moves = r.get("moves", [])
            msg = r.get("message", "")

            if msg:
                lines.append(f"\n  {track} ({fam}/{role}): {msg}")
            elif moves:
                lines.append(f"\n  {track} ({fam}/{role}):")
                for m in moves:
                    lo = m.get("lo", 0)
                    hi = m.get("hi", 0)
                    db = m.get("gain_db", 0.0)
                    bi = m.get("band_index", 0)
                    label = BAND_LABELS[bi] if bi < len(BAND_LABELS) else f"Band {bi}"
                    action = "cut" if db < 0 else "boost"
                    lines.append(
                        f"    {label:<12} {lo:>5}-{hi:<5} Hz  {db:>+5.1f} dB ({action})"
                    )
            else:
                lines.append(f"\n  {track} ({fam}/{role}): No moves")

            apply_status = r.get("apply_status", "")
            if apply_status:
                lines.append(f"    Apply status: {apply_status}")
            apply_errors = r.get("apply_errors", [])
            if apply_errors:
                lines.append("    Apply errors:")
                for e in apply_errors:
                    lines.append(f"      - {e}")

    errors = data.get("errors", [])
    if errors:
        lines.append(f"\nErrors ({len(errors)}):")
        for e in errors:
            lines.append(f"  - {e}")

    return "\n".join(lines)
