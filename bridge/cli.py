"""CLI entrypoint for reaper-ai."""

import argparse
import json
import sys

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


def cmd_context(args):
    """Query REAPER for tracks and installed FX."""
    result = send_command("get_context")
    if result.get("status") == "ok":
        print(format_context(result))
    else:
        print(f"Error: {result.get('errors', ['Unknown error'])}", file=sys.stderr)
        sys.exit(1)


def cmd_track_info(args):
    """Query FX chain and parameters for a track."""
    result = send_command("get_track_fx", track=args.track)
    if result.get("status") == "ok":
        print(format_track_fx(result))
    else:
        errors = result.get("errors", ["Unknown error"])
        print(f"Error: {'; '.join(errors)}", file=sys.stderr)
        sys.exit(1)


def cmd_set_param(args):
    """Set parameters on an existing FX."""
    params = []
    for pair in args.params:
        name, val = pair.rsplit("=", 1)
        params.append({"name": name, "value": float(val)})
    result = send_command("set_param", track=args.track, fx_index=args.fx_index, params=params)
    status = result.get("status", "unknown")
    applied = result.get("applied", 0)
    errors = result.get("errors", [])
    confirmed = result.get("confirmed", [])
    print(f"Status: {status}, applied: {applied}")
    if confirmed:
        for c in confirmed:
            req = c.get("requested", c.get("value"))
            actual = c.get("value", req)
            disp = c.get("display", "")
            drift = abs(actual - req) if isinstance(actual, (int, float)) and isinstance(req, (int, float)) else 0
            warn = "  ** DRIFT" if drift > 0.005 else ""
            print(f"  {c['name']}: {disp}{warn}")
    for e in errors:
        print(f"  Error: {e}", file=sys.stderr)
    if status == "error":
        sys.exit(1)


def cmd_set_param_display(args):
    """Set FX parameters by target display value (e.g. 'Decay Time=3.0')."""
    params = []
    for pair in args.params:
        name, val = pair.rsplit("=", 1)
        # Keep as string — daemon handles numeric vs text detection
        try:
            display_val = float(val)
        except ValueError:
            display_val = val
        params.append({"name": name, "display_value": display_val})
    result = send_command("set_param_display", track=args.track, fx_index=args.fx_index, params=params)
    status = result.get("status", "unknown")
    applied = result.get("applied", 0)
    errors = result.get("errors", [])
    confirmed = result.get("confirmed", [])
    print(f"Status: {status}, applied: {applied}")
    if confirmed:
        for c in confirmed:
            req = c.get("requested_display", "?")
            actual = c.get("actual_display", "?")
            norm = c.get("normalized", 0)
            err = c.get("error", 0)
            warn = f"  (err={err:.4f})" if err > 0.001 else ""
            print(f"  {c['name']}: requested={req}, actual={actual}, norm={norm:.4f}{warn}")
    for e in errors:
        print(f"  Error: {e}", file=sys.stderr)
    if status == "error":
        sys.exit(1)


def cmd_reorder_fx(args):
    """Reorder FX chain on a track."""
    order = [int(x) for x in args.order]
    result = send_command("reorder_fx", track=args.track, order=order)
    if result.get("status") == "ok":
        print(f"Reordered FX on '{result.get('track')}':")
        for fx in result.get("fx_chain", []):
            print(f"  [{fx['index']}] {fx['name']}")
    else:
        errors = result.get("errors", ["Unknown error"])
        print(f"Error: {'; '.join(errors)}", file=sys.stderr)
        sys.exit(1)


def cmd_create_track(args):
    """Create a new track in REAPER."""
    result = send_command("create_track", name=args.name)
    if result.get("status") == "ok":
        print(f"Created track '{result.get('track')}' at index {result.get('index')}")
    else:
        errors = result.get("errors", ["Unknown error"])
        print(f"Error: {'; '.join(errors)}", file=sys.stderr)
        sys.exit(1)


def cmd_duplicate_track(args):
    """Duplicate a track (with media items, routing, etc)."""
    kwargs = {"track": args.track}
    if args.name:
        kwargs["new_name"] = args.name
    result = send_command("duplicate_track", **kwargs)
    if result.get("status") == "ok":
        print(f"Duplicated '{result.get('source')}' → '{result.get('track')}' at index {result.get('index')}")
    else:
        errors = result.get("errors", ["Unknown error"])
        print(f"Error: {'; '.join(errors)}", file=sys.stderr)
        sys.exit(1)


def cmd_list_presets(args):
    """List available presets for an FX on a track."""
    result = send_command("list_presets", track=args.track, fx_index=args.fx_index)
    if result.get("status") == "ok":
        print(format_presets(result))
    else:
        errors = result.get("errors", ["Unknown error"])
        print(f"Error: {'; '.join(errors)}", file=sys.stderr)
        sys.exit(1)


def cmd_set_preset(args):
    """Set a preset on an FX by name or index."""
    kwargs = {"track": args.track, "fx_index": args.fx_index}
    if args.preset_index is not None:
        kwargs["preset_index"] = args.preset_index
    else:
        kwargs["preset_name"] = args.preset
    result = send_command("set_preset", **kwargs)
    if result.get("status") == "ok":
        print(f"Preset set: {result.get('preset', '?')}")
    else:
        errors = result.get("errors", ["Unknown error"])
        print(f"Error: {'; '.join(errors)}", file=sys.stderr)
        sys.exit(1)


def cmd_get_fx_config(args):
    """Read one or more named config keys from an FX."""
    result = send_command(
        "get_fx_named_config",
        track=args.track,
        fx_index=args.fx_index,
        names=args.keys,
    )
    status = result.get("status", "unknown")
    print(f"Status: {status}")
    print(f"Track: {result.get('track', args.track)}")
    print(f"FX index: {result.get('fx_index', args.fx_index)}")
    values = result.get("values", [])
    for entry in values:
        key = entry.get("name", "?")
        found = entry.get("found", False)
        value = str(entry.get("value", ""))
        if not args.full and len(value) > 180:
            value = value[:180] + f"... (len={len(str(entry.get('value', '')))})"
        marker = "ok" if found else "missing"
        print(f"  {key} [{marker}]: {value}")
    for e in result.get("errors", []):
        print(f"  Error: {e}", file=sys.stderr)
    if status == "error":
        sys.exit(1)


def cmd_set_fx_config(args):
    """Set one or more named config keys on an FX."""
    params = []
    for pair in args.pairs:
        if "=" not in pair:
            print(f"Error: invalid key=value pair '{pair}'", file=sys.stderr)
            sys.exit(1)
        name, value = pair.split("=", 1)
        params.append({"name": name, "value": value})

    result = send_command(
        "set_fx_named_config",
        track=args.track,
        fx_index=args.fx_index,
        params=params,
    )
    status = result.get("status", "unknown")
    applied = result.get("applied", 0)
    print(f"Status: {status}, applied: {applied}")
    for c in result.get("confirmed", []):
        marker = "verified" if c.get("verified") else "unverified"
        print(f"  {c.get('name')}: {c.get('value', '')} ({marker})")
    for e in result.get("errors", []):
        print(f"  Error: {e}", file=sys.stderr)
    if status == "error":
        sys.exit(1)


def cmd_get_envelope(args):
    """Read automation envelope points from a track."""
    result = send_command("get_envelope", track=args.track, envelope=args.envelope)
    if result.get("status") == "ok":
        print(format_envelope(result))
    else:
        errors = result.get("errors", ["Unknown error"])
        print(f"Error: {'; '.join(errors)}", file=sys.stderr)
        sys.exit(1)


def cmd_set_envelope(args):
    """Insert automation points on an envelope."""
    try:
        points = json.loads(args.points_json)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON for points: {e}", file=sys.stderr)
        sys.exit(1)

    result = send_command(
        "set_envelope_points",
        track=args.track,
        envelope=args.envelope,
        points=points,
        clear_first=args.clear,
    )
    if result.get("status") == "ok":
        print(format_envelope_result(result))
    else:
        errors = result.get("errors", ["Unknown error"])
        print(f"Error: {'; '.join(errors)}", file=sys.stderr)
        sys.exit(1)


def cmd_clear_envelope(args):
    """Remove all points from an envelope."""
    result = send_command("clear_envelope", track=args.track, envelope=args.envelope)
    if result.get("status") == "ok":
        print(format_envelope_result(result))
    else:
        errors = result.get("errors", ["Unknown error"])
        print(f"Error: {'; '.join(errors)}", file=sys.stderr)
        sys.exit(1)


def cmd_add_send(args):
    """Create a send between two tracks."""
    result = send_command(
        "add_send",
        src_track=args.src_track,
        dest_track=args.dest_track,
        send_type=args.type,
        midi_channel=args.midi_channel,
        audio_volume=args.volume,
    )
    if result.get("status") == "ok":
        print(f"Send created (index {result.get('send_index')})")
    else:
        errors = result.get("errors", ["Unknown error"])
        print(f"Error: {'; '.join(errors)}", file=sys.stderr)
        sys.exit(1)


def cmd_get_sends(args):
    """List sends from a track."""
    result = send_command("get_sends", track=args.track)
    if result.get("status") == "ok":
        print(format_sends(result))
    else:
        errors = result.get("errors", ["Unknown error"])
        print(f"Error: {'; '.join(errors)}", file=sys.stderr)
        sys.exit(1)


def cmd_set_send_vol(args):
    """Set the volume of a send on a track."""
    kwargs = {"track": args.track, "send_index": args.send_index}
    if args.db is not None:
        kwargs["volume_db"] = args.db
    else:
        kwargs["volume"] = args.linear
    result = send_command("set_send_volume", **kwargs)
    if result.get("status") == "ok":
        db = result.get("volume_db", 0)
        dest = result.get("dest_track", "?")
        print(f"Send {args.send_index} -> '{dest}' set to {db:.1f} dB")
    else:
        errors = result.get("errors", ["Unknown error"])
        print(f"Error: {'; '.join(errors)}", file=sys.stderr)
        sys.exit(1)


def cmd_load_sample_rs5k(args):
    """Load a sample into RS5k on a track."""
    kwargs = {"track": args.track, "sample_path": args.sample_path, "note": args.note}
    if args.fx_index is not None:
        kwargs["fx_index"] = args.fx_index
    result = send_command("load_sample_rs5k", **kwargs)
    status = result.get("status")
    if status in ("ok", "partial"):
        print(f"RS5k loaded on '{result.get('track')}' (FX index {result.get('fx_index')})")
        for w in result.get("warnings", []):
            print(f"  Warning: {w}", file=sys.stderr)
    else:
        errors = result.get("errors", ["Unknown error"])
        print(f"Error: {'; '.join(errors)}", file=sys.stderr)
        sys.exit(1)


def cmd_setup_reagate(args):
    """Setup ReaGate for MIDI triggering."""
    kwargs = {"track": args.track}
    if args.midi_note is not None:
        kwargs["midi_note"] = args.midi_note
    if args.threshold is not None:
        kwargs["threshold"] = args.threshold
    if args.fx_index is not None:
        kwargs["fx_index"] = args.fx_index
    result = send_command("setup_reagate_midi", **kwargs)
    status = result.get("status")
    if status in ("ok", "partial"):
        print(f"ReaGate configured on '{result.get('track')}' (FX index {result.get('fx_index')})")
        for w in result.get("warnings", []):
            print(f"  Warning: {w}", file=sys.stderr)
    else:
        errors = result.get("errors", ["Unknown error"])
        print(f"Error: {'; '.join(errors)}", file=sys.stderr)
        sys.exit(1)


def cmd_set_track_folder(args):
    """Set folder depth on a track."""
    result = send_command("set_track_folder", track=args.track, depth=args.depth)
    if result.get("status") == "ok":
        print(f"Folder depth set to {args.depth} on '{result.get('track')}'")
    else:
        errors = result.get("errors", ["Unknown error"])
        print(f"Error: {'; '.join(errors)}", file=sys.stderr)
        sys.exit(1)


def cmd_set_track_visible(args):
    """Set track visibility."""
    kwargs = {"track": args.track}
    if args.tcp is not None:
        kwargs["tcp"] = args.tcp
    if args.mixer is not None:
        kwargs["mixer"] = args.mixer
    result = send_command("set_track_visible", **kwargs)
    if result.get("status") == "ok":
        print(f"Visibility updated for '{result.get('track')}'")
    else:
        errors = result.get("errors", ["Unknown error"])
        print(f"Error: {'; '.join(errors)}", file=sys.stderr)
        sys.exit(1)


def cmd_rename_track(args):
    """Rename a track by strict index."""
    result = send_command("rename_track", track=args.track_index, name=args.name)
    if result.get("status") == "ok":
        print(
            f"Renamed track [{result.get('index')}] "
            f"'{result.get('old_name')}' -> '{result.get('track')}'"
        )
    else:
        errors = result.get("errors", ["Unknown error"])
        print(f"Error: {'; '.join(errors)}", file=sys.stderr)
        sys.exit(1)


def cmd_set_track_color(args):
    """Set or clear a track color by strict index."""
    kwargs: dict = {"track": args.track_index}
    if args.clear:
        kwargs["clear"] = True
    else:
        kwargs["color"] = args.color
    result = send_command("set_track_color", **kwargs)
    if result.get("status") == "ok":
        if result.get("cleared"):
            print(f"Cleared color on track [{result.get('index')}] '{result.get('track')}'")
        else:
            print(
                f"Set color on track [{result.get('index')}] "
                f"'{result.get('track')}' -> {result.get('color')}"
            )
    else:
        errors = result.get("errors", ["Unknown error"])
        print(f"Error: {'; '.join(errors)}", file=sys.stderr)
        sys.exit(1)


def cmd_reorder_track(args):
    """Move a track to a new index (strict index targeting)."""
    result = send_command(
        "reorder_track",
        track=args.track_index,
        to_index=args.to_index,
    )
    if result.get("status") == "ok":
        moved = result.get("moved", True)
        if moved:
            print(
                f"Moved '{result.get('track')}' "
                f"from [{result.get('from_index')}] to [{result.get('to_index')}]"
            )
        else:
            print(
                f"No move needed for '{result.get('track')}' "
                f"(already at [{result.get('to_index')}])"
            )
    else:
        errors = result.get("errors", ["Unknown error"])
        print(f"Error: {'; '.join(errors)}", file=sys.stderr)
        sys.exit(1)


def cmd_insert_media(args):
    """Insert a media file onto a track."""
    kwargs = {"track": args.track, "file_path": args.file_path}
    if args.position is not None:
        kwargs["position"] = args.position
    result = send_command("insert_media", **kwargs)
    if result.get("status") == "ok":
        print(f"Inserted '{result.get('file_path')}' on '{result.get('track')}' at {result.get('position', 0):.1f}s ({result.get('length', 0):.1f}s long)")
    else:
        errors = result.get("errors", ["Unknown error"])
        print(f"Error: {'; '.join(errors)}", file=sys.stderr)
        sys.exit(1)


def cmd_set_item_rate(args):
    """Set playback rate on a media item."""
    kwargs = {"track": args.track}
    if args.item_index is not None:
        kwargs["item_index"] = args.item_index
    if args.from_bpm and args.to_bpm:
        kwargs["from_bpm"] = args.from_bpm
        kwargs["to_bpm"] = args.to_bpm
    elif args.rate:
        kwargs["rate"] = args.rate
    else:
        print("Error: provide --rate or --from-bpm + --to-bpm", file=sys.stderr)
        sys.exit(1)
    if args.no_preserve_pitch:
        kwargs["preserve_pitch"] = False
    result = send_command("set_item_rate", **kwargs)
    if result.get("status") == "ok":
        print(f"Set rate {result.get('rate', 0):.5f} on '{result.get('track')}' item {result.get('item_index', 0)}")
        print(f"  Length: {result.get('old_length', 0):.1f}s -> {result.get('new_length', 0):.1f}s")
        print(f"  Preserve pitch: {result.get('preserve_pitch', True)}")
    else:
        errors = result.get("errors", ["Unknown error"])
        print(f"Error: {'; '.join(errors)}", file=sys.stderr)
        sys.exit(1)


def cmd_drum_augment(args):
    """SSD-style drum augment/replace."""
    kwargs = {"audio_track": args.audio_track}
    if args.sample_path is not None:
        kwargs["sample_path"] = args.sample_path
    if args.note is not None:
        kwargs["note"] = args.note
    if args.drum_type is not None:
        kwargs["drum_type"] = args.drum_type
    if args.threshold is not None:
        kwargs["threshold"] = args.threshold
    if args.create_folder:
        kwargs["create_folder"] = True
    result = send_command("drum_augment", **kwargs)
    status = result.get("status")
    if status in ("ok", "partial"):
        print(format_drum_augment(result))
    else:
        errors = result.get("errors", ["Unknown error"])
        print(f"Error: {'; '.join(errors)}", file=sys.stderr)
        sys.exit(1)


def cmd_benchmark_fft(args):
    """Run FFT benchmark in REAPER."""
    result = send_command("benchmark_fft")
    if result.get("status") == "ok":
        ms = result.get("ms_per_frame", 0)
        print(f"FFT Benchmark: {result.get('frames', 0)} frames")
        print(f"  Elapsed: {result.get('elapsed_ms', 0):.1f} ms")
        print(f"  Per frame: {ms:.3f} ms")
        if ms > 0.2:
            print(f"  WARNING: {ms:.3f} ms/frame exceeds 0.2ms threshold")
        else:
            print(f"  OK: within performance budget")
    else:
        errors = result.get("errors", ["Unknown error"])
        print(f"Error: {'; '.join(errors)}", file=sys.stderr)
        sys.exit(1)


def cmd_validate_fft_bands(args):
    """Run synthetic FFT/band mapping validation in REAPER."""
    kwargs: dict = {"timeout": 30}
    if args.sample_rate is not None:
        kwargs["sample_rate"] = args.sample_rate

    result = send_command("validate_fft_bands", **kwargs)
    status = result.get("status")
    if status not in ("ok", "partial"):
        errors = result.get("errors", ["Unknown error"])
        print(f"Error: {'; '.join(errors)}", file=sys.stderr)
        sys.exit(1)

    summary = result.get("summary", {})
    tone_tests = result.get("tone_tests", [])
    stereo_tests = result.get("stereo_tests", [])
    low_probe = result.get("low_band_probe", {})

    print("FFT Band Validation")
    print(f"  Status: {status}")
    print(
        f"  Sample Rate: {result.get('sample_rate')}  "
        f"FFT: {result.get('fft_size')}  Bin: {result.get('bin_hz', 0):.2f} Hz"
    )
    print(
        f"  Failures: tone={summary.get('tone_failures', 0)}, "
        f"stereo={summary.get('stereo_failures', 0)}, "
        f"total={summary.get('total_failures', 0)}"
    )

    print("\nTone Tests (freq -> expected/observed band):")
    for t in tone_tests:
        marker = "PASS" if t.get("pass") else "FAIL"
        print(
            f"  {marker:4} {t.get('freq_hz', 0):8.1f} Hz  "
            f"exp={t.get('expected_band')} obs={t.get('observed_band')}"
        )

    print("\nStereo Tests:")
    for t in stereo_tests:
        marker = "PASS" if t.get("pass") else "FAIL"
        ratio = t.get("lr_ratio", t.get("rl_ratio", 0))
        print(
            f"  {marker:4} {t.get('name', '?'):<18} "
            f"exp={t.get('expected_band')} ratio={ratio:.2f}"
        )

    if low_probe:
        print("\nLow Band Probe (informational):")
        print(
            f"  {low_probe.get('freq_hz', 0):.1f} Hz  "
            f"exp={low_probe.get('expected_band')} obs={low_probe.get('observed_band')}"
        )
        note = low_probe.get("note")
        if note:
            print(f"  Note: {note}")

    if status == "partial":
        sys.exit(1)


def cmd_calibrate_reaeq(args):
    """Run ReaEQ gain calibration."""
    result = send_command("calibrate_reaeq", timeout=30)
    if result.get("status") == "ok":
        gain_mapping = result.get("mapping", [])
        # Backward-compatible with older daemon payloads.
        freq_mapping = result.get("freq_mapping", result.get("freq", []))
        band_type_norm = result.get("band_type_norm")
        print(
            "ReaEQ calibration complete: "
            f"gain={len(gain_mapping)} points, freq={len(freq_mapping)} points"
        )
        if band_type_norm is not None:
            print(f"  Parametric band type norm: {band_type_norm:.3f}")
        else:
            print("  Parametric band type norm: unavailable on this ReaEQ build")
        # Save to cache (include all band layout metadata)
        from bridge.auto_eq import _calibration_cache_path
        import json as _json
        cache = _calibration_cache_path()
        cache.parent.mkdir(parents=True, exist_ok=True)
        raw_band_names = result.get("band_names", [])
        band_names = [str(b).strip() for b in raw_band_names] if isinstance(raw_band_names, list) else []
        band_names = [b for b in band_names if b]
        cache_data = {
            "gain": gain_mapping,
            "freq": freq_mapping,
            "band_type_norm": band_type_norm,
            "band_names": band_names,
            "visible_bands_norm": result.get("visible_bands_norm"),
            "layout_mode": result.get("layout_mode"),
            "layout_preset_name": result.get("layout_preset_name"),
        }
        cache.write_text(_json.dumps(cache_data, indent=2), encoding="utf-8")
        print(f"  Saved to: {cache}")
        if band_names:
            print(f"  Bands ({len(band_names)}): {', '.join(str(b) for b in band_names)}")
        layout_mode = result.get("layout_mode", "default")
        if layout_mode != "default":
            print(f"  Layout: {layout_mode}")
            preset_name = result.get("layout_preset_name")
            if preset_name:
                print(f"  Preset: {preset_name}")
    else:
        errors = result.get("errors", ["Unknown error"])
        print(f"Error: {'; '.join(errors)}", file=sys.stderr)
        sys.exit(1)


def cmd_analyze_tracks(args):
    """Analyze spectral content of tracks."""
    from bridge.auto_eq import analyze_track

    for name in args.tracks:
        print(f"Analyzing '{name}'...", file=sys.stderr)
        result = analyze_track(name, args.time_start, args.time_end)
        if result.get("status") == "ok":
            print(format_spectral_analysis(result))
            print()
        else:
            errors = result.get("errors", ["Unknown error"])
            print(f"Error analyzing '{name}': {'; '.join(str(e) for e in errors)}", file=sys.stderr)
            sys.exit(1)


def cmd_auto_eq(args):
    """Run auto-EQ on yield tracks."""
    from bridge.auto_eq import auto_eq

    result = auto_eq(
        priority_track=args.priority_track,
        yield_tracks=args.yield_tracks,
        max_cut_db=args.max_cut,
        aggressiveness=args.aggressiveness,
        max_cuts=args.max_cuts,
        makeup_mode=args.makeup,
        time_start=args.time_start,
        time_end=args.time_end,
    )
    print(format_auto_eq_result(result))
    if result.get("status") == "error":
        sys.exit(1)


def cmd_auto_eq_all(args):
    """Run auto-EQ on all tracks using priority hierarchy."""
    from bridge.auto_eq import auto_eq_all

    role_overrides: dict[str, str] = {}
    for item in getattr(args, "role", []):
        if "=" not in item:
            print(f"Error: invalid --role '{item}' (expected Track Name=role)", file=sys.stderr)
            sys.exit(1)
        track_name, role = item.split("=", 1)
        track_name, role = track_name.strip(), role.strip()
        if not track_name or not role:
            print(f"Error: invalid --role '{item}' (expected Track Name=role)", file=sys.stderr)
            sys.exit(1)
        role_overrides[track_name] = role

    role_hints: dict[str, str] = {}
    for item in getattr(args, "hint", []):
        if "=" not in item:
            print(f"Error: invalid --hint '{item}' (expected Track Name=role)", file=sys.stderr)
            sys.exit(1)
        track_name, role = item.split("=", 1)
        track_name, role = track_name.strip(), role.strip()
        if not track_name or not role:
            print(f"Error: invalid --hint '{item}' (expected Track Name=role)", file=sys.stderr)
            sys.exit(1)
        role_hints[track_name] = role

    result = auto_eq_all(
        max_cut_db=args.max_cut,
        aggressiveness=args.aggressiveness,
        max_cuts=args.max_cuts,
        makeup_mode=args.makeup,
        level=args.level,
        time_start=args.time_start,
        time_end=args.time_end,
        strategy=args.strategy,
        family=args.family,
        max_boost_db=args.max_boost,
        role_overrides=role_overrides or None,
        role_hints=role_hints or None,
    )
    print(format_auto_eq_result(result))
    if result.get("status") == "error":
        sys.exit(1)


def cmd_auto_eq_compl(args):
    """Run complementary Auto-EQ for same-family layering."""
    from bridge.auto_eq import auto_eq_compl

    role_overrides: dict[str, str] = {}
    for item in args.role:
        if "=" not in item:
            print(
                f"Error: invalid --role '{item}' (expected Track Name=role)",
                file=sys.stderr,
            )
            sys.exit(1)
        track_name, role = item.split("=", 1)
        track_name = track_name.strip()
        role = role.strip()
        if not track_name or not role:
            print(
                f"Error: invalid --role '{item}' (expected Track Name=role)",
                file=sys.stderr,
            )
            sys.exit(1)
        role_overrides[track_name] = role

    role_hints: dict[str, str] = {}
    for item in getattr(args, "hint", []):
        if "=" not in item:
            print(f"Error: invalid --hint '{item}' (expected Track Name=role)", file=sys.stderr)
            sys.exit(1)
        track_name, role = item.split("=", 1)
        track_name, role = track_name.strip(), role.strip()
        if not track_name or not role:
            print(f"Error: invalid --hint '{item}' (expected Track Name=role)", file=sys.stderr)
            sys.exit(1)
        role_hints[track_name] = role

    result = auto_eq_compl(
        family=args.family,
        level=args.level,
        max_cut_db=args.max_cut,
        max_boost_db=args.max_boost,
        max_moves=args.max_moves,
        role_overrides=role_overrides,
        role_hints=role_hints or None,
        time_start=args.time_start,
        time_end=args.time_end,
    )
    print(format_auto_eq_result(result))
    if result.get("status") == "error":
        sys.exit(1)


def cmd_auto_eq_sections(args):
    """Run per-section auto-EQ with automation envelopes."""
    from bridge.auto_eq import auto_eq_sections

    # Parse --section args: "label:start-end"
    sections: list[tuple[str, float, float]] = []
    for raw in args.section:
        if ":" not in raw:
            print(
                f"Error: invalid --section '{raw}' (expected label:start-end)",
                file=sys.stderr,
            )
            sys.exit(1)
        label, _, times = raw.partition(":")
        label = label.strip()
        if "-" not in times:
            print(
                f"Error: invalid --section '{raw}' (expected label:start-end)",
                file=sys.stderr,
            )
            sys.exit(1)
        parts = times.split("-", 1)
        try:
            start = float(parts[0])
            end = float(parts[1])
        except ValueError:
            print(
                f"Error: invalid --section '{raw}' (start/end must be numbers)",
                file=sys.stderr,
            )
            sys.exit(1)
        sections.append((label, start, end))

    analyze_range = None
    if args.analyze_range:
        ar_parts = args.analyze_range.split("-", 1)
        try:
            analyze_range = (float(ar_parts[0]), float(ar_parts[1]))
        except (ValueError, IndexError):
            print(
                f"Error: invalid --analyze-range '{args.analyze_range}' (expected start-end)",
                file=sys.stderr,
            )
            sys.exit(1)

    # Parse --role overrides: "Track Name=role"
    role_overrides = None
    if args.roles:
        role_overrides = {}
        for raw_role in args.roles:
            if "=" not in raw_role:
                print(
                    f"Error: invalid --role '{raw_role}' (expected Track=role)",
                    file=sys.stderr,
                )
                sys.exit(1)
            rname, _, rrole = raw_role.partition("=")
            role_overrides[rname.strip()] = rrole.strip()

    result = auto_eq_sections(
        sections=sections,
        max_cut_db=args.max_cut,
        aggressiveness=args.aggressiveness,
        max_cuts=args.max_cuts,
        level=args.level,
        analyze_range=analyze_range,
        makeup_mode="off" if args.no_makeup else "auto",
        strategy=args.strategy,
        family=args.family,
        max_boost_db=args.max_boost,
        role_overrides=role_overrides,
        write_mode=args.write_mode,
        hybrid_selective_makeup=args.hybrid_selective_makeup,
    )
    print(format_auto_eq_result(result))
    if result.get("status") == "error":
        sys.exit(1)


def cmd_toggle_autoeq(args):
    """Enable or bypass all [AutoEQ], [AutoEQ-Comp], and [AutoEQ-Gain] FX instances."""
    enabled = args.state == "on"
    total_toggled = 0
    all_details = []
    errors = []
    for tag in ("[AutoEQ]", "[AutoEQ-Comp]", "[AutoEQ-Gain]"):
        result = send_command("set_fx_enabled", pattern=tag, enabled=enabled, timeout=10)
        if result.get("status") == "ok":
            total_toggled += int(result.get("toggled", 0))
            all_details.extend(result.get("details", []))
        else:
            errors.append(f"{tag}: {result.get('errors', ['Unknown'])}")
    if errors:
        for e in errors:
            print(f"Error: {e}", file=sys.stderr)
        if total_toggled == 0:
            sys.exit(1)
    state_str = "enabled" if enabled else "bypassed"
    print(f"{state_str.capitalize()} {total_toggled} AutoEQ instance(s)")
    for d in all_details:
        print(f"  Track {d['track_index']}: {d['track']} [{d['fx_index']}] {d['fx_name']}")


def cmd_recover_autoeq(args):
    """Re-enable AutoEQ FX after a crash left them bypassed."""
    from bridge.auto_eq import recover_autoeq
    result = recover_autoeq()
    method = result.get("method", "unknown")
    if method == "snapshot_restore":
        restored = result.get("restored", 0)
        print(f"Restored {restored} FX state(s) from crash-recovery snapshot")
    else:
        toggled = result.get("toggled", 0)
        print(f"Re-enabled {toggled} AutoEQ instance(s) via tag scan")
        for d in result.get("details", []):
            print(f"  Track {d['track_index']}: {d['track']} [{d['fx_index']}] {d['fx_name']}")
    if result.get("errors"):
        for e in result["errors"]:
            print(f"  Warning: {e}", file=sys.stderr)
    if result.get("status") == "error":
        sys.exit(1)


def cmd_apply(args):
    """Send a plan JSON file to REAPER."""
    try:
        with open(args.plan_file, encoding="utf-8") as f:
            plan_data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"Error reading plan file: {e}", file=sys.stderr)
        sys.exit(1)

    _send_plan(plan_data)


def cmd_enable_reaeq_band(args):
    """Enable or disable a ReaEQ band."""
    enabled = args.enabled == "on"
    result = send_command("enable_reaeq_band", track=args.track,
                          fx_index=args.fx_index, band=args.band, enabled=enabled)
    if result.get("status") == "ok":
        state = "enabled" if enabled else "disabled"
        print(f"Band {result.get('band')} {state} on '{result.get('track')}' fx {result.get('fx_index')}")
    else:
        errors = result.get("errors", ["Unknown error"])
        print(f"Error: {'; '.join(errors)}", file=sys.stderr)
        sys.exit(1)


def cmd_apply_stdin(args):
    """Read plan JSON from stdin and send to REAPER."""
    try:
        raw = sys.stdin.read()
        plan_data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON from stdin: {e}", file=sys.stderr)
        sys.exit(1)

    _send_plan(plan_data)


def _send_plan(plan_data: dict):
    """Validate and send a plan to REAPER."""
    # The plan_data can be either a full command or just the plan portion
    if "op" in plan_data:
        # Full command format
        result = send_command(**plan_data)
    elif "track" in plan_data and "plan" in plan_data:
        # Shorthand: {track, plan}
        result = send_command("apply_plan", track=plan_data["track"], plan=plan_data["plan"])
    else:
        print(
            "Error: JSON must contain either a full command (with 'op') "
            "or {track, plan} shorthand.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(format_apply_result(result))
    if result.get("status") == "error":
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(prog="reaper-ai", description="REAPER AI FX Chain Controller")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("context", help="Show tracks and installed FX")

    p_param = sub.add_parser("set-param", help="Set FX parameters")
    p_param.add_argument("track", help="Track name (or substring)")
    p_param.add_argument("fx_index", type=int, help="FX index on the track")
    p_param.add_argument("params", nargs="+", help="name=value pairs (e.g. 'Global Gain=0.25')")

    p_paramd = sub.add_parser("set-param-display", help="Set FX parameters by display value")
    p_paramd.add_argument("track", help="Track name (or substring)")
    p_paramd.add_argument("fx_index", type=int, help="FX index on the track")
    p_paramd.add_argument("params", nargs="+", help="name=display_value pairs (e.g. 'Decay Time=3.0')")

    p_reorderfx = sub.add_parser("reorder-fx", help="Reorder FX chain on a track")
    p_reorderfx.add_argument("track", help="Track name (or substring)")
    p_reorderfx.add_argument("order", nargs="+", help="Current FX indices in desired order (e.g. 2 3 4 1 5 0)")

    p_create = sub.add_parser("create-track", help="Create a new track")
    p_create.add_argument("name", help="Track name")

    p_dup = sub.add_parser("duplicate-track", help="Duplicate a track with media items")
    p_dup.add_argument("track", help="Source track name (or substring)")
    p_dup.add_argument("--name", help="Name for the new track (optional)")

    p_track = sub.add_parser("track-info", help="Show FX chain + params for a track")
    p_track.add_argument("track", help="Track name (or substring)")

    p_apply = sub.add_parser("apply", help="Send a plan JSON file to REAPER")
    p_apply.add_argument("plan_file", help="Path to plan JSON file")

    p_presets = sub.add_parser("list-presets", help="List presets for an FX")
    p_presets.add_argument("track", help="Track name (or substring)")
    p_presets.add_argument("fx_index", type=int, help="FX index on the track")

    p_set_preset = sub.add_parser("set-preset", help="Set a preset on an FX")
    p_set_preset.add_argument("track", help="Track name (or substring)")
    p_set_preset.add_argument("fx_index", type=int, help="FX index on the track")
    preset_group = p_set_preset.add_mutually_exclusive_group(required=True)
    preset_group.add_argument("--name", dest="preset", help="Preset name")
    preset_group.add_argument("--index", dest="preset_index", type=int, help="Preset index")

    p_get_fxcfg = sub.add_parser("get-fx-config", help="Read FX named-config key(s)")
    p_get_fxcfg.add_argument("track", help="Track name (or substring)")
    p_get_fxcfg.add_argument("fx_index", type=int, help="FX index on the track")
    p_get_fxcfg.add_argument("keys", nargs="+", help="Named config keys (e.g. fx_ident vst_chunk)")
    p_get_fxcfg.add_argument("--full", action="store_true", help="Print full values without truncation")

    p_set_fxcfg = sub.add_parser("set-fx-config", help="Set FX named-config key(s)")
    p_set_fxcfg.add_argument("track", help="Track name (or substring)")
    p_set_fxcfg.add_argument("fx_index", type=int, help="FX index on the track")
    p_set_fxcfg.add_argument("pairs", nargs="+", help="key=value pairs")

    p_get_env = sub.add_parser("get-envelope", help="Read automation envelope points")
    p_get_env.add_argument("track", help="Track name (or 'master')")
    p_get_env.add_argument("envelope", help="Envelope name: Volume, Pan, Mute, Width")

    p_set_env = sub.add_parser("set-envelope", help="Insert automation points on an envelope")
    p_set_env.add_argument("track", help="Track name (or 'master')")
    p_set_env.add_argument("envelope", help="Envelope name: Volume, Pan, Mute, Width")
    p_set_env.add_argument("points_json", help='JSON array of points, e.g. \'[{"time":0,"value":1.0}]\'')
    p_set_env.add_argument("--clear", action="store_true", help="Clear existing points first")

    p_clr_env = sub.add_parser("clear-envelope", help="Remove all points from an envelope")
    p_clr_env.add_argument("track", help="Track name (or 'master')")
    p_clr_env.add_argument("envelope", help="Envelope name: Volume, Pan, Mute, Width")

    sub.add_parser("apply-stdin", help="Read plan JSON from stdin")

    p_enable_band = sub.add_parser("enable-reaeq-band", help="Enable/disable a ReaEQ band")
    p_enable_band.add_argument("track", help="Track name or index")
    p_enable_band.add_argument("fx_index", type=int, help="ReaEQ FX index on the track")
    p_enable_band.add_argument("band", type=int, help="Band index (0=Low Shelf, 1=Band 2, 2=Band 3, 3=High Shelf 4, 4=High Pass 5)")
    p_enable_band.add_argument("enabled", choices=["on", "off"], help="Enable or disable the band")

    p_add_send = sub.add_parser("add-send", help="Create a send between two tracks")
    p_add_send.add_argument("src_track", help="Source track name or index")
    p_add_send.add_argument("dest_track", help="Destination track name or index")
    p_add_send.add_argument("--type", default="both", choices=["audio", "midi", "both"],
                            help="Send type (default: both)")
    p_add_send.add_argument("--midi-channel", type=int, default=-1,
                            help="MIDI channel (-1=all, 0-15)")
    p_add_send.add_argument("--volume", type=float, default=1.0,
                            help="Audio volume (linear, 1.0=0dB)")

    p_get_sends = sub.add_parser("get-sends", help="List sends from a track")
    p_get_sends.add_argument("track", help="Track name or index")

    p_set_send_vol = sub.add_parser("set-send-vol", help="Set send volume")
    p_set_send_vol.add_argument("track", help="Track name or index")
    p_set_send_vol.add_argument("send_index", type=int, help="Send index (0-based)")
    vol_group = p_set_send_vol.add_mutually_exclusive_group(required=True)
    vol_group.add_argument("--db", type=float, help="Volume in dB (e.g. -12)")
    vol_group.add_argument("--linear", type=float, help="Volume as linear scalar (1.0 = 0 dB)")

    p_rs5k = sub.add_parser("load-sample-rs5k", help="Load sample into RS5k")
    p_rs5k.add_argument("track", help="Track name or index")
    p_rs5k.add_argument("sample_path", help="Path to sample file")
    p_rs5k.add_argument("--note", type=int, default=60, help="MIDI note (0-127, default 60)")
    p_rs5k.add_argument("--fx-index", type=int, default=None,
                        help="Existing RS5k FX index to update")

    p_reagate = sub.add_parser("setup-reagate", help="Setup ReaGate for MIDI triggering")
    p_reagate.add_argument("track", help="Track name or index")
    p_reagate.add_argument("--midi-note", type=int, help="MIDI note (0-127)")
    p_reagate.add_argument("--threshold", type=float, help="Gate threshold (normalized 0-1)")
    p_reagate.add_argument("--fx-index", type=int, default=None,
                           help="Existing ReaGate FX index to update")

    p_folder = sub.add_parser("set-track-folder", help="Set folder depth on a track")
    p_folder.add_argument("track", help="Track name or index")
    p_folder.add_argument("depth", type=int, help="Folder depth (1=parent, 0=normal, -1=end)")

    p_vis = sub.add_parser("set-track-visible", help="Set track visibility")
    p_vis.add_argument("track", help="Track name or index")
    p_vis.add_argument("--tcp", type=bool, default=None, help="Show in TCP")
    p_vis.add_argument("--mixer", type=bool, default=None, help="Show in mixer")

    p_rename_track = sub.add_parser("rename-track", help="Rename a track by index")
    p_rename_track.add_argument("track_index", type=int, help="Track index (strict)")
    p_rename_track.add_argument("name", help="New track name")

    p_track_color = sub.add_parser("set-track-color", help="Set or clear track color by index")
    p_track_color.add_argument("track_index", type=int, help="Track index (strict)")
    color_group = p_track_color.add_mutually_exclusive_group(required=True)
    color_group.add_argument("--color", help="Hex color, e.g. #2FA3FF")
    color_group.add_argument("--clear", action="store_true", help="Clear custom color")

    p_reorder = sub.add_parser("reorder-track", help="Move track to target index")
    p_reorder.add_argument("track_index", type=int, help="Track index to move (strict)")
    p_reorder.add_argument("to_index", type=int, help="Destination index")

    p_insert = sub.add_parser("insert-media", help="Insert a media file onto a track")
    p_insert.add_argument("track", help="Track name or index")
    p_insert.add_argument("file_path", help="Path to media file (wav, mp3, etc.)")
    p_insert.add_argument("--position", type=float, default=None, help="Position in seconds (default 0)")

    p_rate = sub.add_parser("set-item-rate", help="Set playback rate on a media item (tempo stretch)")
    p_rate.add_argument("track", help="Track name or index")
    p_rate.add_argument("--item-index", type=int, default=None, help="Item index on track (default 0)")
    p_rate.add_argument("--rate", type=float, default=None, help="Direct playback rate (e.g. 0.99074)")
    p_rate.add_argument("--from-bpm", type=float, default=None, help="Original BPM")
    p_rate.add_argument("--to-bpm", type=float, default=None, help="Target BPM")
    p_rate.add_argument("--no-preserve-pitch", action="store_true", help="Disable pitch preservation")

    p_drum = sub.add_parser("drum-augment", help="SSD-style drum augment/replace")
    p_drum.add_argument("audio_track", help="Audio track name or index")
    p_drum.add_argument("sample_path", nargs="?", default=None, help="Path to replacement sample (optional, creates empty RS5k if omitted)")
    p_drum.add_argument("--note", type=int, default=None, help="MIDI note (0-127)")
    p_drum.add_argument("--drum-type", default=None,
                        help="Drum type hint: kick, snare, hihat, crash, ride, etc.")
    p_drum.add_argument("--threshold", type=float, default=None,
                        help="ReaGate threshold (normalized 0-1)")
    p_drum.add_argument("--create-folder", action="store_true",
                        help="Organize into a folder")

    # --- Auto-EQ commands ---
    sub.add_parser("benchmark-fft", help="Run FFT performance benchmark")
    p_validate_fft = sub.add_parser(
        "validate-fft-bands",
        help="Validate FFT band mapping/stereo separation using synthetic tones",
    )
    p_validate_fft.add_argument(
        "--sample-rate",
        type=float,
        default=None,
        help="Sample rate for synthetic tests (default 44100)",
    )

    sub.add_parser("calibrate-reaeq", help="Run ReaEQ gain calibration")

    p_analyze = sub.add_parser("analyze-tracks", help="Analyze spectral content of tracks")
    p_analyze.add_argument("tracks", nargs="+", help="Track names to analyze")
    p_analyze.add_argument("--time-start", type=float, default=None,
                           help="Analysis start time in seconds")
    p_analyze.add_argument("--time-end", type=float, default=None,
                           help="Analysis end time in seconds")

    p_autoeq = sub.add_parser("auto-eq", help="Auto-EQ: reduce masking on yield tracks")
    p_autoeq.add_argument("priority_track", help="Track that should be heard clearly")
    p_autoeq.add_argument("--yield", dest="yield_tracks", nargs="+", required=True,
                          help="Tracks to apply corrective EQ to")
    p_autoeq.add_argument("--max-cut", type=float, default=-6.0,
                          help="Max EQ cut in dB (negative, default -6)")
    p_autoeq.add_argument("--aggressiveness", type=float, default=1.5,
                          help="Cut aggressiveness (0.5=gentle, 1.0=normal, 1.5=strong, 2.0=aggressive)")
    p_autoeq.add_argument("--max-cuts", type=int, default=5,
                          help="Maximum number of EQ cuts per yield track (default 5)")
    p_autoeq.add_argument("--makeup", choices=["off", "auto"], default="auto",
                          help="Selective makeup mode on uncut bands (default auto)")
    p_autoeq.add_argument("--time-start", type=float, default=None,
                          help="Analysis start time in seconds")
    p_autoeq.add_argument("--time-end", type=float, default=None,
                          help="Analysis end time in seconds")

    p_autoeqall = sub.add_parser("auto-eq-all",
                                 help="Auto-EQ all tracks using priority hierarchy")
    p_autoeqall.add_argument("--max-cut", type=float, default=-3.0,
                             help="Max EQ cut in dB (negative, default -3)")
    p_autoeqall.add_argument("--aggressiveness", type=float, default=1.0,
                             help="Cut aggressiveness (0.5=gentle, 1.0=normal, 2.0=aggressive)")
    p_autoeqall.add_argument("--max-cuts", type=int, default=2,
                             help="Maximum number of EQ cuts per track (default 2)")
    p_autoeqall.add_argument("--makeup", choices=["off", "auto"], default="off",
                             help="Selective makeup mode on uncut bands (default off)")
    p_autoeqall.add_argument("--level", choices=["auto", "leaf", "bus"], default="leaf",
                             help="Target level selection (default leaf)")
    p_autoeqall.add_argument("--time-start", type=float, default=None,
                             help="Analysis start time in seconds")
    p_autoeqall.add_argument("--time-end", type=float, default=None,
                             help="Analysis end time in seconds")
    p_autoeqall.add_argument("--strategy", choices=["subtractive", "hybrid"],
                             default="subtractive",
                             help="subtractive (cuts only) or hybrid (cuts + family boosts)")
    p_autoeqall.add_argument("--family",
                             choices=["all", "guitar", "keys-synth", "vocal"],
                             default="all",
                             help="Restrict hybrid boosts to this family (default all)")
    p_autoeqall.add_argument("--max-boost", type=float, default=6.0,
                             help="Max complementary boost in dB (default 6, hybrid only)")
    p_autoeqall.add_argument("--role", action="append", default=[],
                             help="Hard role override: 'Track Name=role' (anchor/presence/texture/support)")
    p_autoeqall.add_argument("--hint", action="append", default=[],
                             help="Soft role hint: 'Track Name=role' (nudges but doesn't force)")

    p_autoeqcompl = sub.add_parser(
        "auto-eq-compl",
        help="Complementary Auto-EQ for same-family layering (guitar/keys-synth)",
    )
    p_autoeqcompl.add_argument(
        "--family",
        choices=["all", "guitar", "keys-synth", "vocal"],
        default="all",
        help="Family scope for complementary EQ (default all)",
    )
    p_autoeqcompl.add_argument(
        "--level",
        choices=["auto", "leaf", "bus"],
        default="leaf",
        help="Target level selection (default leaf)",
    )
    p_autoeqcompl.add_argument(
        "--max-cut",
        type=float,
        default=-3.0,
        help="Maximum cut magnitude in dB (negative, default -3)",
    )
    p_autoeqcompl.add_argument(
        "--max-boost",
        type=float,
        default=6.0,
        help="Maximum boost magnitude in dB (default +6)",
    )
    p_autoeqcompl.add_argument(
        "--max-moves",
        type=int,
        default=4,
        help="Maximum total EQ moves per track (default 4)",
    )
    p_autoeqcompl.add_argument(
        "--role",
        action="append",
        default=[],
        help="Role override, repeatable: \"Track Name=anchor|presence|texture|support\"",
    )
    p_autoeqcompl.add_argument(
        "--hint",
        action="append",
        default=[],
        help="Soft role hint: \"Track Name=role\" (nudges but doesn't force)",
    )
    p_autoeqcompl.add_argument("--time-start", type=float, default=None,
                               help="Analysis start time in seconds")
    p_autoeqcompl.add_argument("--time-end", type=float, default=None,
                               help="Analysis end time in seconds")

    p_autoeqsec = sub.add_parser(
        "auto-eq-sections",
        help="Per-section auto-EQ with automation envelopes",
    )
    p_autoeqsec.add_argument(
        "--section",
        action="append",
        default=[],
        required=True,
        help='Section spec, repeatable: "label:start-end" (seconds)',
    )
    p_autoeqsec.add_argument("--max-cut", type=float, default=-6.0,
                              help="Max EQ cut in dB (negative, default -6)")
    p_autoeqsec.add_argument("--aggressiveness", type=float, default=1.5,
                              help="Cut aggressiveness (default 1.5)")
    p_autoeqsec.add_argument("--max-cuts", type=int, default=5,
                              help="Maximum number of EQ cuts per section per track (default 5)")
    p_autoeqsec.add_argument("--level", choices=["auto", "leaf", "bus"], default="leaf",
                              help="Target level selection (default leaf)")
    p_autoeqsec.add_argument("--analyze-range",
                              help='Override analysis window: "start-end" in seconds (e.g. "80-90")')
    p_autoeqsec.add_argument("--no-makeup", action="store_true", default=False,
                              help="Disable selective makeup on uncut bands (on by default)")
    p_autoeqsec.add_argument("--strategy", choices=["subtractive", "hybrid"],
                              default="hybrid",
                              help="EQ strategy: subtractive (cuts only) or hybrid (cuts + family boosts, default)")
    p_autoeqsec.add_argument("--family", default="all",
                              help="Restrict hybrid boosts to family (guitar, keys-synth, or all)")
    p_autoeqsec.add_argument("--max-boost", type=float, default=6.0,
                              help="Maximum boost in dB for hybrid mode (default 6)")
    p_autoeqsec.add_argument("--role", dest="roles", metavar="TRACK=ROLE",
                              action="append", default=[],
                              help='Override complementary role: "Track Name=anchor" (repeatable)')
    p_autoeqsec.add_argument("--write-mode", choices=["replace", "merge", "auto"],
                              default="auto",
                              help="Write mode: replace (wipe existing), merge (incremental), auto (default)")
    p_autoeqsec.add_argument("--hybrid-selective-makeup", action="store_true", default=False,
                              help="Allow selective makeup even in hybrid strategy")

    p_toggle = sub.add_parser("toggle-autoeq",
                               help="Enable or bypass all [AutoEQ] and [AutoEQ-Comp] instances")
    p_toggle.add_argument("state", choices=["on", "off"],
                          help="'on' enables, 'off' bypasses all AutoEQ FX")

    sub.add_parser("recover-autoeq",
                    help="Re-enable AutoEQ FX after a crash left them bypassed")

    args = parser.parse_args()

    commands = {
        "context": cmd_context,
        "set-param": cmd_set_param,
        "set-param-display": cmd_set_param_display,
        "reorder-fx": cmd_reorder_fx,
        "create-track": cmd_create_track,
        "duplicate-track": cmd_duplicate_track,
        "track-info": cmd_track_info,
        "list-presets": cmd_list_presets,
        "set-preset": cmd_set_preset,
        "get-fx-config": cmd_get_fx_config,
        "set-fx-config": cmd_set_fx_config,
        "get-envelope": cmd_get_envelope,
        "set-envelope": cmd_set_envelope,
        "clear-envelope": cmd_clear_envelope,
        "apply": cmd_apply,
        "apply-stdin": cmd_apply_stdin,
        "enable-reaeq-band": cmd_enable_reaeq_band,
        "add-send": cmd_add_send,
        "get-sends": cmd_get_sends,
        "set-send-vol": cmd_set_send_vol,
        "load-sample-rs5k": cmd_load_sample_rs5k,
        "setup-reagate": cmd_setup_reagate,
        "set-track-folder": cmd_set_track_folder,
        "set-track-visible": cmd_set_track_visible,
        "rename-track": cmd_rename_track,
        "set-track-color": cmd_set_track_color,
        "reorder-track": cmd_reorder_track,
        "insert-media": cmd_insert_media,
        "set-item-rate": cmd_set_item_rate,
        "drum-augment": cmd_drum_augment,
        "benchmark-fft": cmd_benchmark_fft,
        "validate-fft-bands": cmd_validate_fft_bands,
        "calibrate-reaeq": cmd_calibrate_reaeq,
        "analyze-tracks": cmd_analyze_tracks,
        "auto-eq": cmd_auto_eq,
        "auto-eq-all": cmd_auto_eq_all,
        "auto-eq-compl": cmd_auto_eq_compl,
        "auto-eq-sections": cmd_auto_eq_sections,
        "toggle-autoeq": cmd_toggle_autoeq,
        "recover-autoeq": cmd_recover_autoeq,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
