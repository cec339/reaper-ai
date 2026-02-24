"""CLI entrypoint for reaper-ai."""

import argparse
import json
import sys

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
    print(f"Status: {status}, applied: {applied}")
    for e in errors:
        print(f"  Error: {e}", file=sys.stderr)
    if status == "error":
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


def cmd_drum_augment(args):
    """SSD-style drum augment/replace."""
    kwargs = {"audio_track": args.audio_track, "sample_path": args.sample_path}
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


def cmd_apply(args):
    """Send a plan JSON file to REAPER."""
    try:
        with open(args.plan_file, encoding="utf-8") as f:
            plan_data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"Error reading plan file: {e}", file=sys.stderr)
        sys.exit(1)

    _send_plan(plan_data)


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

    p_drum = sub.add_parser("drum-augment", help="SSD-style drum augment/replace")
    p_drum.add_argument("audio_track", help="Audio track name or index")
    p_drum.add_argument("sample_path", help="Path to replacement sample")
    p_drum.add_argument("--note", type=int, default=None, help="MIDI note (0-127)")
    p_drum.add_argument("--drum-type", default=None,
                        help="Drum type hint: kick, snare, hihat, crash, ride, etc.")
    p_drum.add_argument("--threshold", type=float, default=None,
                        help="ReaGate threshold (normalized 0-1)")
    p_drum.add_argument("--create-folder", action="store_true",
                        help="Organize into a folder")

    args = parser.parse_args()

    commands = {
        "context": cmd_context,
        "set-param": cmd_set_param,
        "create-track": cmd_create_track,
        "duplicate-track": cmd_duplicate_track,
        "track-info": cmd_track_info,
        "list-presets": cmd_list_presets,
        "set-preset": cmd_set_preset,
        "get-envelope": cmd_get_envelope,
        "set-envelope": cmd_set_envelope,
        "clear-envelope": cmd_clear_envelope,
        "apply": cmd_apply,
        "apply-stdin": cmd_apply_stdin,
        "add-send": cmd_add_send,
        "get-sends": cmd_get_sends,
        "load-sample-rs5k": cmd_load_sample_rs5k,
        "setup-reagate": cmd_setup_reagate,
        "set-track-folder": cmd_set_track_folder,
        "set-track-visible": cmd_set_track_visible,
        "drum-augment": cmd_drum_augment,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
