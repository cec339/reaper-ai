"""Auto-EQ: frequency masking detection and corrective EQ.

Analyzes spectral content of tracks, detects frequency masking between
priority and yield tracks, and applies corrective EQ cuts via ReaEQ.
"""

import json
import math
import time
from pathlib import Path
from typing import Callable

from bridge.ipc import send_command as _default_send_command

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BAND_EDGES = [20, 40, 80, 160, 320, 640, 1250, 2500, 5000, 10000, 20000]
BAND_LABELS = [
    "Sub", "Bass", "Low-Mid", "Lower-Mid", "Mid", "Upper-Mid",
    "Low Presence", "High Presence", "Brilliance", "Air",
]

AUTO_EQ_TAG = "[AutoEQ]"
AUTO_EQ_COMPL_TAG = "[AutoEQ-Comp]"
AUTOEQ_GAIN_TAG = "[AutoEQ-Gain]"

# Keywords that mark helper/reference tracks — skip in auto-eq-all
SKIP_KEYWORDS = {"guide", "ref", "tmp"}

# Role-based priority keywords (higher = more important, gets spectral priority)
DEFAULT_PRIORITY = [
    ("vocal", 1.0), ("vox", 1.0), ("lead", 0.95),
    ("kick", 0.9), ("snare", 0.85), ("clap", 0.85),
    ("bass", 0.8),
    ("tom", 0.78), ("floor tom", 0.78), ("rack tom", 0.78),
    ("overhead", 0.72), ("oh", 0.72), ("room", 0.72),
    ("hihat", 0.68), ("hi hat", 0.68), ("hat", 0.68),
    ("ride", 0.68), ("crash", 0.68), ("cymbal", 0.68),
    ("perc", 0.62), ("shaker", 0.62), ("tamb", 0.62),
    ("drum", 0.7), ("piano", 0.65), ("keys", 0.6),
    ("guitar", 0.55), ("gtr", 0.55), ("synth", 0.5),
    ("pad", 0.4), ("string", 0.4), ("fx", 0.3),
]

# --- Mode-specific defaults ---
# auto-eq (surgical, user-targeted): more assertive
PAIR_MAX_CUTS = 5
PAIR_MAX_CUT_DB = -6.0
PAIR_AGGRESSIVENESS = 1.5
PAIR_MAKEUP_MODE = "auto"

# auto-eq-all (unattended, many tracks): conservative
ALL_MAX_CUTS = 2
ALL_MAX_CUT_DB = -3.0
ALL_MAKEUP_MODE = "off"

# Minimum cut threshold — ignore cuts shallower than this
MIN_CUT_DB = -0.7

# Priority energy floor in dB — don't cut yield if priority is weaker than this
PRIORITY_ENERGY_FLOOR_DB = -60.0

# Priority reference activity floor (track-level peak dB).
# References below this are treated as inactive (noise floor) and ignored
# when selecting higher-priority contributors across all Auto-EQ modes.
PRIORITY_REFERENCE_ACTIVE_FLOOR_DB = -35.0

# Relative lane detection for complementary boost blocking.
# A priority track is "active" in a band if its energy there is within
# PRIORITY_LANE_GAP_DB of its own peak AND above PRIORITY_LANE_FLOOR_DB absolute.
PRIORITY_LANE_GAP_DB = 22.0
PRIORITY_LANE_FLOOR_DB = -55.0

# Per-band contributor attribution in section summaries
CONTRIBUTOR_NEAR_MAX_DB = 3.0
CONTRIBUTOR_MAX_TRACKS = 3

# Makeup gain guardrails
MAKEUP_MIN_DB = 0.1
MAKEUP_BIAS = 1.5
MAKEUP_FLOOR_DB = 1.2
MAKEUP_TOTAL_CAP_DB = 2.5
MAKEUP_PER_BAND_CAP_DB = 2.5
MAKEUP_MAX_DB = MAKEUP_TOTAL_CAP_DB  # Backward-compatible alias.

VOLUME_FLOOR_DB = -80.0  # Skip tracks whose volume envelope is below this during the section

ANALYSIS_TIMEOUT = 300
ANALYSIS_BUSY_RETRY_TIMEOUT = 300
ANALYSIS_BUSY_RETRY_INTERVAL = 1.0
CALIBRATION_TIMEOUT = 30
PREFLIGHT_TIMEOUT = 15

# Complementary mode defaults / guardrails
SUPPORTED_COMPL_FAMILIES = {"all", "guitar", "keys-synth", "vocal"}
COMPL_MAX_MOVES = 4
COMPL_MAX_BOOST_BANDS = 1
COMPL_MAX_BOOST_DB = 6.0
COMPL_HIGH_BAND_BOOST_CAP_DB = 6.0
COMPL_TARGET_BOOST_MEAN_DB = 6.0
COMPL_CROWDING_THRESHOLD_DB = 3.0
COMPL_DEFICIT_THRESHOLD_DB = -3.0
COMPL_MIN_MOVE_DB = 0.7
COMPL_CUT_SCALE = 0.5
COMPL_BOOST_SCALE = 1.0
COMPL_OWNERSHIP_BOOST_DB = 6.0  # Full boost on target bands even when not deficit
COMPL_OWNERSHIP_THRESHOLD_DB = 1.0  # Skip ownership boost if already well above mean
COMPL_SUPPORT_CUT_SCALE = 0.75  # Support role gets heavier cuts (vs 0.5 normal)
COMPL_BOOST_BW_OCT = 0.5  # Narrower bandwidth for complementary boosts (default preset = 1.0)
COMPL_CUT_BW_OCT = 1.0    # Bandwidth for complementary/masking cuts (keep preset default)

# Inter-family spectral lane assignment
LANE_DOMINANCE_THRESHOLD_DB = 3.0   # Min dB advantage to claim a band
LANE_ACTIVITY_FLOOR_DB = -45.0      # Family must have at least one track with peak above this to participate
LANE_FAMILY_PRIORITY = {"vocal": 3, "guitar": 2, "keys-synth": 1}  # Tie-breaking sort stability only

ROLE_ALIAS = {
    "body": "anchor",
    "main": "anchor",
    "focus": "presence",
    "presence": "presence",
    "air": "texture",
    "texture": "texture",
    "support": "support",
    "anchor": "anchor",
}

COMPL_ROLE_BANDS = {
    "guitar": {
        "anchor": {2, 3, 4, 5},
        "presence": {6, 7},
        "texture": {8, 9},
    },
    "keys-synth": {
        "anchor": {1, 2, 3, 4},
        "presence": {5, 6, 7},
        "texture": {8, 9},
    },
    "vocal": {
        "anchor": {3, 4},        # fundamentals: 160-640 Hz
        "presence": {5, 6, 7},   # intelligibility: 640-5k Hz
        "texture": {8, 9},       # air/breath: 5k-20k Hz
    },
}


def _compute_contested_bands() -> dict[int, list[str]]:
    """Find analysis bands where 2+ families have boost-eligible roles.

    Returns {band_index: [family_names...]} for contested bands only.
    """
    band_families: dict[int, list[str]] = {}
    for fam, roles in COMPL_ROLE_BANDS.items():
        fam_bands: set[int] = set()
        for role_bands in roles.values():
            fam_bands |= role_bands
        for b in fam_bands:
            band_families.setdefault(b, []).append(fam)
    return {b: fams for b, fams in band_families.items() if len(fams) >= 2}


LANE_CONTESTED_BANDS = _compute_contested_bands()


# ---------------------------------------------------------------------------
# Audit artifacts
# ---------------------------------------------------------------------------

def _audit_dir_path() -> Path:
    """Path to persistent Auto-EQ decision audit artifacts."""
    from bridge.ipc import get_queue_path
    return get_queue_path() / "auto_eq_audit"


def _now_utc_iso() -> str:
    """UTC timestamp string for audit metadata."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _write_audit_artifact(mode: str, payload: dict) -> str | None:
    """Write an audit artifact and return its path (or None on failure)."""
    try:
        audit_dir = _audit_dir_path()
        audit_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
        ms = int(time.time() * 1000) % 1000
        filename = f"{mode}_{stamp}_{ms:03d}.json"
        path = audit_dir / filename
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        # Keep stable pointers to latest for quick verification.
        latest_mode = audit_dir / f"latest_{mode}.json"
        latest_all = audit_dir / "latest.json"
        latest_mode.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        latest_all.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return str(path)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Preflight helpers (AutoEQ tag bypass/restore)
# ---------------------------------------------------------------------------

def _tag_for_fx_name(fx_name: str) -> str | None:
    if AUTOEQ_GAIN_TAG in fx_name:
        return AUTOEQ_GAIN_TAG
    if AUTO_EQ_COMPL_TAG in fx_name:
        return AUTO_EQ_COMPL_TAG
    if AUTO_EQ_TAG in fx_name:
        return AUTO_EQ_TAG
    return None


def _snapshot_tagged_fx_states(
    send_command_fn: Callable = _default_send_command,
) -> dict:
    """Snapshot enabled state of existing tagged AutoEQ FX."""
    ctx = send_command_fn("get_context", timeout=10)
    if ctx.get("status") != "ok":
        return {
            "status": "error",
            "states": [],
            "errors": [f"get_context failed: {ctx.get('errors', ['Unknown'])}"],
        }

    states: list[dict] = []
    errors: list[str] = []
    for track in ctx.get("tracks", []):
        idx = track.get("index")
        if not isinstance(idx, (int, float)):
            continue
        if not any(_tag_for_fx_name(str(n)) for n in track.get("fx", [])):
            continue
        fx_result = send_command_fn("get_track_fx", track=int(idx), timeout=10)
        if fx_result.get("status") != "ok":
            errors.append(
                f"get_track_fx failed on index {int(idx)}: "
                f"{fx_result.get('errors', ['Unknown'])}"
            )
            continue
        track_name = fx_result.get("track", track.get("name", f"Track {int(idx)}"))
        for fx in fx_result.get("fx_chain", []):
            fx_name = fx.get("name", "")
            tag = _tag_for_fx_name(fx_name)
            if tag is None:
                continue
            states.append({
                "track_index": int(idx),
                "track": track_name,
                "fx_index": int(fx.get("index", -1)),
                "fx_name": fx_name,
                "enabled": bool(fx.get("enabled", True)),
                "tag": tag,
            })

    status = "partial" if errors else "ok"
    return {"status": status, "states": states, "errors": errors}


def _preflight_snapshot_path() -> Path:
    """Path to crash-recovery preflight snapshot."""
    from bridge.ipc import get_queue_path
    return get_queue_path() / "preflight_snapshot.json"


def _persist_preflight_snapshot(preflight: dict) -> None:
    """Write preflight state to disk for crash recovery."""
    path = _preflight_snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(preflight, indent=2), encoding="utf-8")


def _clear_preflight_snapshot() -> None:
    """Remove preflight snapshot after successful restore."""
    path = _preflight_snapshot_path()
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def recover_autoeq(
    send_command_fn: Callable = _default_send_command,
) -> dict:
    """Re-enable all AutoEQ FX after a crash left them bypassed.

    Checks for a persisted preflight snapshot and restores from it,
    or falls back to enabling all tagged FX instances.
    """
    snapshot_path = _preflight_snapshot_path()
    errors: list[str] = []
    restored = 0

    if snapshot_path.exists():
        try:
            preflight = json.loads(snapshot_path.read_text(encoding="utf-8"))
            summary = _restore_autoeq_preflight(preflight, send_command_fn, mode="recovery")
            restored = summary.get("restored_applied", 0)
            errors.extend(summary.get("errors", []))
            _clear_preflight_snapshot()
            return {
                "status": "ok" if not errors else "partial",
                "method": "snapshot_restore",
                "restored": restored,
                "errors": errors,
            }
        except (json.JSONDecodeError, OSError) as exc:
            errors.append(f"Snapshot read failed: {exc}, falling back to tag enable")

    # Fallback: just re-enable everything with AutoEQ tags
    total_toggled = 0
    details = []
    for tag in (AUTO_EQ_TAG, AUTO_EQ_COMPL_TAG, AUTOEQ_GAIN_TAG):
        result = send_command_fn(
            "set_fx_enabled", pattern=tag, enabled=True, timeout=10,
        )
        if result.get("status") == "ok":
            toggled = int(result.get("toggled", 0))
            total_toggled += toggled
            details.extend(result.get("details", []))
        else:
            errors.append(f"Enable {tag}: {result.get('errors', ['Unknown'])}")

    _clear_preflight_snapshot()
    return {
        "status": "ok" if not errors else "partial",
        "method": "tag_enable",
        "toggled": total_toggled,
        "details": details,
        "errors": errors,
    }


def _prepare_autoeq_preflight(
    send_command_fn: Callable = _default_send_command,
) -> dict:
    """Snapshot and bypass tagged AutoEQ FX before analysis."""
    snapshot = _snapshot_tagged_fx_states(send_command_fn)
    states = snapshot.get("states", [])
    errors = [str(e) for e in snapshot.get("errors", [])]

    bypass_total = 0
    bypass_details: list[dict] = []
    for tag in (AUTO_EQ_TAG, AUTO_EQ_COMPL_TAG, AUTOEQ_GAIN_TAG):
        result = send_command_fn(
            "set_fx_enabled",
            pattern=tag,
            enabled=False,
            timeout=PREFLIGHT_TIMEOUT,
        )
        if result.get("status") == "ok":
            bypass_total += int(result.get("toggled", 0))
            bypass_details.append({
                "tag": tag,
                "toggled": int(result.get("toggled", 0)),
            })
        else:
            errors.append(
                f"bypass failed for {tag}: {result.get('errors', ['Unknown'])}"
            )

    preflight = {
        "snapshot_count": len(states),
        "snapshot_states": states,
        "bypassed_total": bypass_total,
        "bypass_details": bypass_details,
        "errors": errors,
    }
    # Persist to disk so crash recovery can restore FX states
    if bypass_total > 0:
        _persist_preflight_snapshot(preflight)
    return preflight


def _restore_autoeq_preflight(
    preflight: dict,
    send_command_fn: Callable = _default_send_command,
    mode: str = "auto_eq",
) -> dict:
    """Best-effort restore of tagged FX enabled states after run."""
    states = preflight.get("snapshot_states", [])
    restore_errors: list[str] = []
    restored_applied = 0

    if states:
        payload = [
            {
                "track": s["track_index"],
                "fx_index": s["fx_index"],
                "enabled": s["enabled"],
                "expected_name": s["fx_name"],
            }
            for s in states
        ]
        restore_result = send_command_fn(
            "set_fx_enabled_exact",
            states=payload,
            timeout=PREFLIGHT_TIMEOUT,
        )
        if restore_result.get("status") in ("ok", "partial"):
            restored_applied = int(restore_result.get("applied", 0))
        if restore_result.get("status") != "ok":
            restore_errors.extend(str(e) for e in restore_result.get("errors", []))

    # If a tag was fully bypassed before run, keep all current instances bypassed.
    tag_totals = {
        AUTO_EQ_TAG: {"total": 0, "enabled": 0},
        AUTO_EQ_COMPL_TAG: {"total": 0, "enabled": 0},
        AUTOEQ_GAIN_TAG: {"total": 0, "enabled": 0},
    }
    for st in states:
        tag = st.get("tag")
        if tag not in tag_totals:
            continue
        tag_totals[tag]["total"] += 1
        if st.get("enabled"):
            tag_totals[tag]["enabled"] += 1

    for tag, counts in tag_totals.items():
        if counts["total"] > 0 and counts["enabled"] == 0:
            keep_off = send_command_fn(
                "set_fx_enabled",
                pattern=tag,
                enabled=False,
                timeout=PREFLIGHT_TIMEOUT,
            )
            if keep_off.get("status") != "ok":
                restore_errors.append(
                    f"failed to preserve bypass state for {tag}: {keep_off.get('errors', ['Unknown'])}"
                )

    summary = {
        "mode": mode,
        "snapshot_count": int(preflight.get("snapshot_count", 0)),
        "bypassed_total": int(preflight.get("bypassed_total", 0)),
        "restored_applied": restored_applied,
        "errors": [*preflight.get("errors", []), *restore_errors],
    }
    status = "ok" if not summary["errors"] else ("partial" if restored_applied > 0 else "error")
    summary["status"] = status

    msg = (
        f"AutoEQ preflight ({mode}): snapshot={summary['snapshot_count']}, "
        f"bypassed={summary['bypassed_total']}, restored={summary['restored_applied']}, "
        f"errors={len(summary['errors'])}"
    )
    try:
        send_command_fn("log", message=msg, timeout=5)
    except Exception:
        pass
    return summary


def _finalize_with_preflight(
    result: dict,
    preflight: dict | None,
    send_command_fn: Callable,
    mode: str,
) -> dict:
    """Attach preflight summary while ensuring restore always runs."""
    if preflight is None:
        return result
    summary = _restore_autoeq_preflight(preflight, send_command_fn, mode=mode)
    _clear_preflight_snapshot()
    result["preflight"] = summary
    return result


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

def _calibration_cache_path() -> Path:
    """Path to cached ReaEQ calibration data."""
    from bridge.ipc import get_queue_path
    return get_queue_path() / "calibration" / "reaeq_calibration.json"


def _gain_mapping_has_boost_range(gain_mapping: list[dict]) -> bool:
    """True when gain calibration includes both cut and boost ranges."""
    if not isinstance(gain_mapping, list) or len(gain_mapping) < 10:
        return False
    db_values = [m.get("db") for m in gain_mapping if isinstance(m, dict) and isinstance(m.get("db"), (int, float))]
    if len(db_values) < 10:
        return False
    return min(db_values) <= -3.0 and max(db_values) >= 3.0


def ensure_calibration(
    send_command_fn: Callable = _default_send_command,
    require_boost_range: bool = False,
) -> dict:
    """Load or create ReaEQ gain + freq calibration mappings.

    Returns dict with keys:
        "gain": list of {"normalized": float, "db": float}
        "freq": list of {"normalized": float, "hz": float}
        "band_type_norm": optional normalized value for ReaEQ parametric type
        "band_names": optional ordered ReaEQ band names discovered from params
        "visible_bands_norm": optional normalized value for expanded visible bands
        "layout_mode": optional layout strategy used ("visible_bands", "preset_11band", ...)
        "layout_preset_name": optional preset name used for expanded layout
    When require_boost_range=True, cached/calibrated gain data must include
    both cut and boost ranges.
    """
    cache = _calibration_cache_path()
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            # Backward compatibility: old cache was a gain-only list.
            if isinstance(data, list):
                # Force one refresh to capture band layout metadata.
                pass
            # New cache format: require valid gain mapping; freq/type are optional.
            if isinstance(data, dict):
                gain = data.get("gain", [])
                gain_ok = isinstance(gain, list) and len(gain) > 10
                if gain_ok and require_boost_range:
                    gain_ok = _gain_mapping_has_boost_range(gain)
                has_layout_meta = "band_names" in data and (
                    "visible_bands_norm" in data
                    or "layout_mode" in data
                    or "layout_preset_name" in data
                )
                if gain_ok and has_layout_meta:
                    freq = data.get("freq", [])
                    if not isinstance(freq, list):
                        freq = []
                    return {
                        "gain": gain,
                        "freq": freq,
                        "band_type_norm": data.get("band_type_norm"),
                        "band_names": data.get("band_names", []),
                        "visible_bands_norm": data.get("visible_bands_norm"),
                        "layout_mode": data.get("layout_mode"),
                        "layout_preset_name": data.get("layout_preset_name"),
                    }
        except (json.JSONDecodeError, OSError):
            pass

    # Run calibration in REAPER
    result = send_command_fn("calibrate_reaeq", timeout=CALIBRATION_TIMEOUT)
    if result.get("status") != "ok":
        raise RuntimeError(
            f"ReaEQ calibration failed: {result.get('errors', ['Unknown'])}"
        )

    gain_mapping = result.get("mapping", [])
    if len(gain_mapping) < 10:
        raise RuntimeError("ReaEQ calibration returned too few gain points")
    if require_boost_range and not _gain_mapping_has_boost_range(gain_mapping):
        raise RuntimeError("ReaEQ calibration missing boost-range gain points")
    gain_mapping.sort(key=lambda m: m["normalized"])

    # Older daemons may not return freq/type mappings yet; keep graceful fallback.
    freq_mapping = result.get("freq_mapping", result.get("freq", []))
    if not isinstance(freq_mapping, list):
        freq_mapping = []
    freq_mapping.sort(key=lambda m: m["normalized"])

    band_type_norm = result.get("band_type_norm")
    band_names = result.get("band_names", [])
    if not isinstance(band_names, list):
        band_names = []
    visible_bands_norm = result.get("visible_bands_norm")
    layout_mode = result.get("layout_mode")
    layout_preset_name = result.get("layout_preset_name")

    cal_data = {
        "gain": gain_mapping,
        "freq": freq_mapping,
        "band_type_norm": band_type_norm,
        "band_names": band_names,
        "visible_bands_norm": visible_bands_norm,
        "layout_mode": layout_mode,
        "layout_preset_name": layout_preset_name,
    }

    # Cache to disk
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(cal_data, indent=2), encoding="utf-8")
    return cal_data


def _interpolate_table(
    value: float, table: list[dict], value_key: str, norm_key: str = "normalized",
) -> float:
    """Generic interpolation: find normalized value for a given real-world value."""
    if not table:
        raise ValueError("Empty calibration table")

    min_val = table[0][value_key]
    max_val = table[-1][value_key]
    value = max(min_val, min(max_val, value))

    for i in range(len(table) - 1):
        lo = table[i]
        hi = table[i + 1]
        if lo[value_key] <= value <= hi[value_key]:
            span = hi[value_key] - lo[value_key]
            if span == 0:
                return lo[norm_key]
            t = (value - lo[value_key]) / span
            return lo[norm_key] + t * (hi[norm_key] - lo[norm_key])

    if value <= min_val:
        return table[0][norm_key]
    return table[-1][norm_key]


def db_to_normalized(db: float, calibration: list[dict]) -> float:
    """Convert a dB value to ReaEQ normalized gain parameter."""
    return _interpolate_table(db, calibration, "db")


def normalized_to_db(norm: float, calibration: list[dict]) -> float:
    """Convert a ReaEQ normalized gain parameter back to dB."""
    return _interpolate_table(norm, calibration, "normalized", "db")


def hz_to_normalized(hz: float, freq_calibration: list[dict]) -> float:
    """Convert a Hz value to ReaEQ normalized freq parameter."""
    if freq_calibration:
        return _interpolate_table(hz, freq_calibration, "hz")
    # Fallback when freq calibration is unavailable:
    # approximate ReaEQ's frequency knob as logarithmic 20Hz..24kHz.
    hz = max(20.0, min(24000.0, hz))
    return math.log10(hz / 20.0) / math.log10(24000.0 / 20.0)


# ---------------------------------------------------------------------------
# Selective makeup helpers
# ---------------------------------------------------------------------------

def _makeup_target_db(raw_makeup_db: float, max_makeup_db: float = MAKEUP_MAX_DB) -> float:
    """Scale + clamp raw makeup estimate for selective makeup mode."""
    if raw_makeup_db < MAKEUP_MIN_DB:
        return 0.0
    target = raw_makeup_db * MAKEUP_BIAS
    target = max(target, MAKEUP_FLOOR_DB)
    return min(target, max_makeup_db)


# ---------------------------------------------------------------------------
# Priority matching
# ---------------------------------------------------------------------------

def _tokenize_name(name: str) -> list[str]:
    """Split a track name into lowercase tokens (letters/digits groups)."""
    import re
    return re.findall(r'[a-z0-9]+', name.lower())


def _name_matches_keyword(name_lower: str, tokens: list[str], keyword: str) -> bool:
    """Keyword matching helper with token-based behavior for short aliases."""
    if keyword in {"vx", "oh", "hat"}:
        return keyword in tokens
    if keyword in {"ref", "tmp"}:
        return any(tok == keyword or tok.startswith(keyword) for tok in tokens)
    return keyword in name_lower


def match_track_priority(track_name: str) -> float:
    """Return a priority score (0-1) for a track name based on role keywords.

    Uses substring matching for most keywords, but token matching for
    short aliases (vx) to avoid false positives.
    """
    name_lower = track_name.lower()
    tokens = _tokenize_name(track_name)

    # Force low priority for helper/reference tracks
    for skip in SKIP_KEYWORDS:
        if _name_matches_keyword(name_lower, tokens, skip):
            return 0.1

    # Token-based matching for short aliases that could false-positive
    if "vx" in tokens:
        return 1.0

    # Substring matching for everything else
    for keyword, priority in DEFAULT_PRIORITY:
        if _name_matches_keyword(name_lower, tokens, keyword):
            return priority
    return 0.5  # default mid-priority for unknown tracks


def is_helper_track(track_name: str) -> bool:
    """Return True if track name matches a helper/reference keyword."""
    name_lower = track_name.lower()
    tokens = _tokenize_name(track_name)
    return any(_name_matches_keyword(name_lower, tokens, skip) for skip in SKIP_KEYWORDS)


def _is_folder_track(track: dict) -> bool:
    """Best-effort folder detection from get_context metadata."""
    if "is_folder" in track:
        return bool(track.get("is_folder"))
    depth = track.get("folder_depth")
    return bool(isinstance(depth, (int, float)) and int(depth) > 0)


def _build_children_map(tracks: list[dict]) -> tuple[dict[int, dict], dict[int, list[int]]]:
    """Build index->track and parent->children maps from track metadata."""
    by_index = {
        int(t["index"]): t
        for t in tracks
        if isinstance(t.get("index"), (int, float))
    }
    children: dict[int, list[int]] = {idx: [] for idx in by_index}
    for idx, track in by_index.items():
        parent = track.get("parent_index", -1)
        if isinstance(parent, (int, float)) and int(parent) >= 0 and int(parent) in by_index:
            children[int(parent)].append(idx)
    return by_index, children


def _collect_descendants(root: int, children: dict[int, list[int]]) -> set[int]:
    """Collect all descendants (recursive) for a root track index."""
    seen: set[int] = set()
    stack = list(children.get(root, []))
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(children.get(cur, []))
    return seen


def _lowest_folder_targets(tracks: list[dict]) -> list[dict]:
    """Return lowest-level folder tracks (those without folder descendants)."""
    by_index, children = _build_children_map(tracks)
    folder_indices = {idx for idx, t in by_index.items() if _is_folder_track(t)}
    if not folder_indices:
        return []

    selected: list[dict] = []
    for idx in sorted(folder_indices):
        descendants = _collect_descendants(idx, children)
        if not descendants:
            continue
        has_folder_desc = any(d in folder_indices for d in descendants)
        if not has_folder_desc:
            selected.append(by_index[idx])
    return selected


def _select_auto_eq_all_targets(tracks: list[dict], level: str) -> tuple[list[dict], str]:
    """Select targets while avoiding parent+child double processing."""
    level = (level or "auto").lower()
    if level not in {"auto", "leaf", "bus"}:
        level = "auto"

    tracks_sorted = sorted(
        [t for t in tracks if isinstance(t.get("index"), (int, float))],
        key=lambda t: t["index"],
    )
    leaves = [t for t in tracks_sorted if not _is_folder_track(t)]
    buses = _lowest_folder_targets(tracks_sorted)

    _, children = _build_children_map(tracks_sorted)
    bus_indices = {int(t["index"]) for t in buses}
    covered_descendants: set[int] = set()
    for idx in bus_indices:
        covered_descendants.update(_collect_descendants(idx, children))

    # Keep leaf tracks not already represented by selected bus targets.
    standalone_leaves = [
        t for t in leaves
        if int(t["index"]) not in covered_descendants
    ]

    if level == "leaf":
        return leaves, "leaf"

    bus_plus_standalone = buses + standalone_leaves
    deduped: list[dict] = []
    seen: set[int] = set()
    for track in bus_plus_standalone:
        idx = int(track["index"])
        if idx in seen:
            continue
        seen.add(idx)
        deduped.append(track)

    if level == "bus":
        if buses:
            return deduped, "bus"
        return leaves, "leaf"

    if buses:
        return deduped, "bus"
    return leaves, "leaf"


def match_track_priorities(track_names: list[str]) -> dict[str, float]:
    """Return priority scores for a list of track names."""
    return {name: match_track_priority(name) for name in track_names}


# ---------------------------------------------------------------------------
# Multi-reference aggregation helpers
# ---------------------------------------------------------------------------

def _aggregate_bands(results_list: list[dict], mode: str = "max") -> list[dict] | None:
    """Combine multiple analysis results into one band list via max-envelope.

    For each band index, takes the loudest value across all inputs for each
    of avg_db, avg_db_l, avg_db_r independently.  Validates band count and
    edge frequencies match across inputs.

    Args:
        results_list: List of successful analysis dicts (each with "bands").
        mode: Aggregation mode — only "max" is supported.

    Returns:
        Combined band list, or None if inputs are empty / incompatible.
    """
    if not results_list:
        return None

    band_lists = [r["bands"] for r in results_list if r.get("bands")]
    if not band_lists:
        return None

    n_bands = len(band_lists[0])
    # Validate all band lists have the same count and edge frequencies.
    for bl in band_lists[1:]:
        if len(bl) != n_bands:
            return None
        for i in range(n_bands):
            if bl[i].get("lo") != band_lists[0][i].get("lo"):
                return None
            if bl[i].get("hi") != band_lists[0][i].get("hi"):
                return None

    combined: list[dict] = []
    for i in range(n_bands):
        band = dict(band_lists[0][i])  # copy structure from first
        for key in ("avg_db", "avg_db_l", "avg_db_r"):
            vals = [bl[i].get(key, -120.0) for bl in band_lists]
            band[key] = max(vals)  # max-envelope: loudest wins
        combined.append(band)
    return combined


def _analysis_peak_db(result: dict) -> float:
    """Track-level peak dB from analysis bands (mono/L/R max)."""
    bands = result.get("bands", [])
    if not isinstance(bands, list) or not bands:
        return -120.0
    peak = -120.0
    for b in bands:
        if not isinstance(b, dict):
            continue
        band_peak = max(
            float(b.get("avg_db", -120.0)),
            float(b.get("avg_db_l", -120.0)),
            float(b.get("avg_db_r", -120.0)),
        )
        if band_peak > peak:
            peak = band_peak
    return peak


def _is_active_priority_reference(
    result: dict,
    floor_db: float = PRIORITY_REFERENCE_ACTIVE_FLOOR_DB,
) -> bool:
    """True when a priority reference has meaningful signal above noise floor."""
    return _analysis_peak_db(result) >= float(floor_db)


def _annotate_cut_contributors(
    cuts: list[dict],
    priority_bands: list[dict],
    priority_results: list[dict],
    *,
    near_max_db: float = CONTRIBUTOR_NEAR_MAX_DB,
    max_tracks: int = CONTRIBUTOR_MAX_TRACKS,
) -> list[dict]:
    """Attach likely per-band masker contributors to selected cuts.

    A contributor is a priority track whose energy in the cut band is near the
    max-envelope used for masking (within *near_max_db* on avg/L/R).
    """
    if not cuts or not priority_results:
        return cuts

    annotated: list[dict] = []
    for cut in cuts:
        cut_with_contrib = dict(cut)
        band_idx = int(cut_with_contrib.get("band_index", -1))
        if band_idx < 0 or band_idx >= len(priority_bands):
            annotated.append(cut_with_contrib)
            continue

        agg_band = priority_bands[band_idx]
        agg_db = float(agg_band.get("avg_db", -120.0))
        agg_db_l = float(agg_band.get("avg_db_l", -120.0))
        agg_db_r = float(agg_band.get("avg_db_r", -120.0))

        scored: list[tuple[float, float, str]] = []
        for ref in priority_results:
            ref_name = str(ref.get("track", "")).strip()
            ref_bands = ref.get("bands", [])
            if not ref_name or not isinstance(ref_bands, list) or band_idx >= len(ref_bands):
                continue

            ref_band = ref_bands[band_idx]
            ref_db = float(ref_band.get("avg_db", -120.0))
            ref_db_l = float(ref_band.get("avg_db_l", -120.0))
            ref_db_r = float(ref_band.get("avg_db_r", -120.0))
            strongest_ref_db = max(ref_db, ref_db_l, ref_db_r)
            if strongest_ref_db < PRIORITY_REFERENCE_ACTIVE_FLOOR_DB:
                continue

            near_avg = ref_db >= (agg_db - near_max_db)
            near_l = ref_db_l >= (agg_db_l - near_max_db)
            near_r = ref_db_r >= (agg_db_r - near_max_db)
            if not (near_avg or near_l or near_r):
                continue

            deltas: list[float] = []
            if near_avg:
                deltas.append(max(0.0, agg_db - ref_db))
            if near_l:
                deltas.append(max(0.0, agg_db_l - ref_db_l))
            if near_r:
                deltas.append(max(0.0, agg_db_r - ref_db_r))
            best_delta = min(deltas) if deltas else 999.0

            # Sort by closeness to envelope, then by stronger band energy.
            scored.append((best_delta, -strongest_ref_db, ref_name))

        scored.sort(key=lambda t: (t[0], t[1], t[2]))
        contributors: list[str] = []
        for _delta, _neg_db, name in scored:
            if name not in contributors:
                contributors.append(name)
            if len(contributors) >= max_tracks:
                break

        if contributors:
            cut_with_contrib["contributors"] = contributors
        annotated.append(cut_with_contrib)

    return annotated


_SILENCE_THRESHOLD_DB = -100.0


def _track_volume_db_at_range(
    track_index: int,
    time_start: float,
    time_end: float,
    send_command_fn: Callable = _default_send_command,
) -> float | None:
    """Read the maximum volume envelope value (in dB) over a time range.

    Returns the max dB value across the range, or None if no volume
    envelope exists (meaning static volume fader — not silent).
    REAPER volume envelope values: 1.0 = 0 dB, 0.0 = -inf dB.
    """
    result = send_command_fn(
        "get_envelope", track=track_index, envelope="Volume",
        strict=True, peek=True, timeout=10,
    )
    if result.get("status") != "ok":
        return None  # No envelope — assume not silent

    points = result.get("points", [])
    if not points:
        return None  # Empty envelope — assume not silent

    # REAPER Volume envelope default is 1.0 (0 dB). Before the first
    # point and after the last point the envelope holds the nearest
    # point's value.  Before any points at all → default = 1.0.
    _VOL_ENV_DEFAULT = 1.0

    # Find the maximum envelope value within the time range.
    max_val = 0.0

    # Include any points that fall inside the range
    for pt in points:
        t = pt.get("time", 0.0)
        v = pt.get("value", _VOL_ENV_DEFAULT)
        if time_start <= t <= time_end:
            max_val = max(max_val, v)

    # Evaluate at range endpoints via linear interpolation
    for eval_t in (time_start, time_end):
        before = None
        after = None
        for pt in points:
            t = pt.get("time", 0.0)
            v = pt.get("value", _VOL_ENV_DEFAULT)
            if t <= eval_t:
                before = (t, v)
            if t >= eval_t and after is None:
                after = (t, v)
        if before and after:
            if before[0] == after[0]:
                val = before[1]
            else:
                frac = (eval_t - before[0]) / (after[0] - before[0])
                val = before[1] + frac * (after[1] - before[1])
            max_val = max(max_val, val)
        elif before:
            # After the last point — holds last value
            max_val = max(max_val, before[1])
        elif after:
            # Before the first point — holds envelope default (0 dB)
            max_val = max(max_val, _VOL_ENV_DEFAULT)

    if max_val <= 0:
        return -math.inf
    return 20.0 * math.log10(max_val)


def _is_silent_analysis(result: dict) -> bool:
    """Return True if analysis result looks like silence (all bands below threshold)."""
    bands = result.get("bands", [])
    if not bands:
        return True
    return all(b.get("avg_db", -120.0) < _SILENCE_THRESHOLD_DB for b in bands)


def _analyze_folder_aggregate(
    folder_track: dict,
    filtered_tracks: list[dict],
    time_start: float | None,
    time_end: float | None,
    send_command_fn: Callable,
    analysis_cache: dict,
) -> dict:
    """Analyze a folder/bus track, falling back to child aggregation on silence.

    First tries direct analysis of the bus track itself (preserves bus FX /
    output character).  If the result is silent (all bands < -100 dB) or
    analysis fails, falls back to analyzing leaf children and combining
    via max-envelope.

    Children are identified by index (not name) to avoid duplicate-name
    collisions in the cache.

    Returns an analysis-result dict (status ok/error, bands, track, index).
    """
    folder_idx = int(folder_track["index"])
    folder_name = folder_track["name"]

    # --- Try direct bus analysis first ---
    direct = _cached_analyze_simple(
        folder_name,
        time_start,
        time_end,
        send_command_fn,
        analysis_cache,
        track_index=folder_idx,
        cache_key=folder_idx,
    )
    if direct.get("status") == "ok" and not _is_silent_analysis(direct):
        return direct

    # --- Direct analysis silent or failed — fall back to child aggregation ---
    by_index, children = _build_children_map(filtered_tracks)
    descendants = _collect_descendants(folder_idx, children)
    if not descendants:
        # No children; return whatever direct gave us (even if silent).
        return direct if direct.get("status") == "ok" else {
            "status": "error",
            "errors": [f"No children and direct analysis failed for '{folder_name}'"],
        }

    # Keep only leaf descendants (non-folder tracks).
    leaves = [by_index[d] for d in sorted(descendants)
              if d in by_index and not _is_folder_track(by_index[d])]
    if not leaves:
        return direct if direct.get("status") == "ok" else {
            "status": "error",
            "errors": [f"No leaf children found for '{folder_name}'"],
        }

    # Analyze each leaf by numeric index to avoid duplicate-name collisions.
    ok_results: list[dict] = []
    for leaf in leaves:
        leaf_name = leaf["name"]
        leaf_idx = int(leaf["index"])
        result = _cached_analyze_simple(
            leaf_name,
            time_start,
            time_end,
            send_command_fn,
            analysis_cache,
            track_index=leaf_idx,
            cache_key=leaf_idx,
        )
        if result.get("status") == "ok":
            ok_results.append(result)

    if not ok_results:
        return direct if direct.get("status") == "ok" else {
            "status": "error",
            "errors": [f"All leaf children of '{folder_name}' failed analysis"],
        }

    combined_bands = _aggregate_bands(ok_results)
    if combined_bands is None:
        return direct if direct.get("status") == "ok" else {
            "status": "error",
            "errors": [f"Band aggregation failed for '{folder_name}' children"],
        }

    return {
        "status": "ok",
        "track": folder_name,
        "index": folder_idx,
        "bands": combined_bands,
        "aggregated_from": [r["track"] for r in ok_results],
    }


def _cached_analyze_simple(
    track_name: str,
    time_start: float | None,
    time_end: float | None,
    send_command_fn: Callable,
    analysis_cache: dict,
    *,
    track_index: int | None = None,
    cache_key: int | str | None = None,
) -> dict:
    """Analyze a track with caching (standalone helper for reuse).

    Prefer numeric-index keys to avoid duplicate-name collisions.
    """
    key = cache_key
    if key is None:
        key = track_index if track_index is not None else track_name
    if key not in analysis_cache:
        analysis_cache[key] = analyze_track(
            track_name, time_start, time_end, send_command_fn, track_index=track_index
        )
    return analysis_cache[key]


def _safe_log(send_command_fn: Callable, message: str) -> None:
    """Best-effort REAPER console logging."""
    if not message:
        return
    try:
        send_command_fn("log", message=message, timeout=5)
    except Exception:
        pass


def _make_analysis_progress_logger(
    send_command_fn: Callable,
    mode_label: str,
    total_tracks: int,
    *,
    section_label: str | None = None,
) -> Callable[[str, int], None]:
    """Create a per-run progress logger for unique analyzed track indices."""
    seen: set[int] = set()
    total = max(1, int(total_tracks))

    def mark(track_name: str, track_idx: int) -> None:
        idx = int(track_idx)
        if idx in seen:
            return
        seen.add(idx)
        done = len(seen)
        prefix = f"{mode_label} [{section_label}]" if section_label else mode_label
        _safe_log(
            send_command_fn,
            f"{prefix}: analyzed {done}/{total} tracks ({track_name})",
        )

    return mark


# ---------------------------------------------------------------------------
# Masking computation
# ---------------------------------------------------------------------------

def _compute_masking_with_details(
    priority_bands: list[dict],
    yield_bands: list[dict],
    max_cut_db: float = -3.0,
    aggressiveness: float = 1.0,
    max_cuts: int = 10,
) -> dict:
    """Compute masking cuts and per-band decision details.

    Args:
        priority_bands: Band analysis from the priority track (list of dicts
                        with keys: lo, hi, avg_db, avg_db_l, avg_db_r).
        yield_bands: Band analysis from the yield track.
        max_cut_db: Maximum cut in dB (negative, e.g. -3.0).
        aggressiveness: Multiplier for cut depth (1.0 = normal).
        max_cuts: Maximum number of cuts to return (deepest first).

    Returns:
        Dict with:
            cuts: selected cut dicts (deepest N)
            band_decisions: per-band metrics + selected/skip reason
    """
    k = 6.0 * aggressiveness
    eps = 1e-12
    candidates: list[dict] = []
    band_decisions: list[dict] = []

    for b in range(len(priority_bands)):
        pb = priority_bands[b]
        yb = yield_bands[b]
        freq_lo = pb.get("lo", 0)
        freq_hi = pb.get("hi", 20000)
        mid_freq = (freq_lo + freq_hi) / 2
        p_db = pb.get("avg_db", -120)

        decision = {
            "band_index": b,
            "label": BAND_LABELS[b] if b < len(BAND_LABELS) else f"Band {b}",
            "lo": freq_lo,
            "hi": freq_hi,
            "priority_db": p_db,
            "priority_db_l": pb.get("avg_db_l", -120),
            "priority_db_r": pb.get("avg_db_r", -120),
            "yield_db": yb.get("avg_db", -120),
            "yield_db_l": yb.get("avg_db_l", -120),
            "yield_db_r": yb.get("avg_db_r", -120),
        }

        # Priority energy floor: skip bands where priority track is too weak
        # (no point cutting yield if priority has nothing there)
        if p_db < PRIORITY_ENERGY_FLOOR_DB:
            decision["decision"] = "skip"
            decision["reason"] = "priority_below_floor"
            band_decisions.append(decision)
            continue
        # Convert from dB back to linear energy for ratio computation
        p_l = 10 ** (pb.get("avg_db_l", -120) / 10)
        p_r = 10 ** (pb.get("avg_db_r", -120) / 10)
        y_l = 10 ** (yb.get("avg_db_l", -120) / 10)
        y_r = 10 ** (yb.get("avg_db_r", -120) / 10)
        decision["priority_linear_l"] = p_l
        decision["priority_linear_r"] = p_r
        decision["yield_linear_l"] = y_l
        decision["yield_linear_r"] = y_r

        # We only cut yield when it dominates the priority track in this band.
        if max(p_l, p_r) < 1e-9:
            decision["decision"] = "skip"
            decision["reason"] = "priority_silent"
            band_decisions.append(decision)
            continue

        pressure_l = y_l / (p_l + eps)
        pressure_r = y_r / (p_r + eps)
        decision["pressure_l"] = pressure_l
        decision["pressure_r"] = pressure_r

        max_pressure = max(pressure_l, pressure_r)
        min_pressure = min(pressure_l, pressure_r)
        decision["max_pressure"] = max_pressure

        # Stereo safety guard: if tracks are panned apart, reduce cut
        stereo_factor = 1.0
        overlap_ratio = 1.0
        if max_pressure > eps:
            overlap_ratio = min_pressure / max_pressure
            if overlap_ratio < 0.3:
                stereo_factor = 0.3
        decision["overlap_ratio"] = overlap_ratio
        decision["stereo_factor"] = stereo_factor

        # Only cut if significant masking pressure
        if max_pressure < 1.5:
            decision["decision"] = "skip"
            decision["reason"] = "pressure_below_threshold"
            band_decisions.append(decision)
            continue

        raw_cut = -k * math.log10(max_pressure)
        clamped_cut = -min(abs(raw_cut), abs(max_cut_db))
        cut = clamped_cut * stereo_factor
        decision["raw_cut_db"] = raw_cut
        decision["clamped_cut_db"] = clamped_cut

        # Frequency guardrails
        guard = "none"
        if mid_freq < 80:
            cut *= 0.5  # Gentle in sub/bass region
            guard = "low_freq_halved"
        elif mid_freq > 5000:
            cut *= 0.7  # Gentle in high frequencies
            guard = "high_freq_0.7x"
        decision["freq_guard"] = guard

        # Minimum cut threshold — skip tiny cuts
        if cut > MIN_CUT_DB:
            decision["decision"] = "skip"
            decision["reason"] = "cut_too_shallow"
            decision["final_cut_db"] = round(cut, 4)
            band_decisions.append(decision)
            continue

        cut_entry = {
            "band_index": b,
            "lo": freq_lo,
            "hi": freq_hi,
            "cut_db": round(cut, 2),
        }
        decision["decision"] = "candidate"
        decision["reason"] = "candidate_cut"
        decision["final_cut_db"] = cut_entry["cut_db"]
        candidates.append(cut_entry)
        band_decisions.append(decision)

    # Return only the deepest N cuts (most impactful)
    candidates.sort(key=lambda c: c["cut_db"])
    selected = candidates[:max_cuts]
    selected_indices = {c["band_index"] for c in selected}

    # Mark unselected candidates as top-N limited.
    for d in band_decisions:
        if d.get("decision") == "candidate":
            if d["band_index"] in selected_indices:
                d["decision"] = "selected"
                d["reason"] = "top_n_selected"
            else:
                d["decision"] = "skip"
                d["reason"] = "top_n_limit"

    return {"cuts": selected, "band_decisions": band_decisions}


def compute_masking(
    priority_bands: list[dict],
    yield_bands: list[dict],
    max_cut_db: float = -3.0,
    aggressiveness: float = 1.0,
    max_cuts: int = 10,
) -> list[dict]:
    """Compute per-band EQ cuts for the yield track to reduce masking."""
    return _compute_masking_with_details(
        priority_bands=priority_bands,
        yield_bands=yield_bands,
        max_cut_db=max_cut_db,
        aggressiveness=aggressiveness,
        max_cuts=max_cuts,
    )["cuts"]


# ---------------------------------------------------------------------------
# FX chain helpers
# ---------------------------------------------------------------------------

def _describe_cuts(cuts: list[dict]) -> str:
    """Summarize cuts as readable band names, e.g. 'low-mids and presence'."""
    if not cuts:
        return "no cuts"
    names = []
    for c in cuts:
        bi = c.get("band_index", 0)
        label = BAND_LABELS[bi].lower() if bi < len(BAND_LABELS) else f"band {bi}"
        if label not in names:
            names.append(label)
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def find_tagged_fx(fx_chain: list[dict], tag: str) -> tuple[int, str] | None:
    """Find existing tagged FX in a chain. Returns (fx_index, fx_name) or None."""
    for fx in fx_chain:
        if tag in fx.get("name", ""):
            return fx["index"], fx["name"]
    return None


def find_auto_eq_fx(fx_chain: list[dict]) -> tuple[int, str] | None:
    """Find existing [AutoEQ]-tagged ReaEQ in an FX chain."""
    return find_tagged_fx(fx_chain, AUTO_EQ_TAG)


# Masking constraint threshold: ignore masking cuts shallower than this
MASKING_CONSTRAINT_THRESHOLD_DB = -1.0


def _normalize_param_key(name: str) -> str:
    """Lowercase alnum-only key for tolerant name matching."""
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def _reaeq_band_names(calibration: dict) -> list[str]:
    """Resolved ReaEQ band names from calibration, else defaults."""
    names = calibration.get("band_names", [])
    if isinstance(names, list):
        cleaned = [str(n) for n in names if str(n).strip()]
        if len(cleaned) >= 5:
            return cleaned
    return DEFAULT_REAEQ_BAND_NAMES


def _band_to_reaeq_map(calibration: dict) -> dict[int, int]:
    """Map 10 analysis bands to resolved ReaEQ band indices."""
    num_analysis_bands = len(BAND_EDGES) - 1
    reaeq_band_count = len(_reaeq_band_names(calibration))
    if reaeq_band_count >= num_analysis_bands:
        # Expanded ReaEQ layout: one-to-one mapping (10 analysis -> 10 ReaEQ).
        return {i: i for i in range(num_analysis_bands)}
    # Fallback for older/default 5-band layouts.
    return DEFAULT_BAND_TO_REAEQ


def read_masking_constraints(
    fx_chain: list[dict],
    calibration: dict,
) -> dict[int, float]:
    """Read existing [AutoEQ] masking cuts and return constrained ReaEQ band indices.

    Returns dict mapping ReaEQ band index → cut dB from the masking EQ.
    Only includes bands with cuts at or below MASKING_CONSTRAINT_THRESHOLD_DB.
    """
    existing = find_auto_eq_fx(fx_chain)
    if existing is None:
        return {}

    fx_index, _ = existing
    # Find the FX entry in the chain to read its params
    fx_entry = None
    for fx in fx_chain:
        if fx.get("index") == fx_index:
            fx_entry = fx
            break
    if fx_entry is None:
        return {}

    gain_cal = calibration.get("gain", [])
    if not gain_cal:
        return {}

    band_names = _reaeq_band_names(calibration)
    by_norm_name = {
        _normalize_param_key(name): idx
        for idx, name in enumerate(band_names)
    }

    constraints: dict[int, float] = {}
    gain_prefix = "Gain-"
    for param in fx_entry.get("params", []):
        pname = param.get("name", "")
        if not pname.startswith(gain_prefix):
            continue
        band_name = pname[len(gain_prefix):]
        reaeq_idx = by_norm_name.get(_normalize_param_key(band_name))
        if reaeq_idx is None:
            continue
        norm_val = param.get("value")
        if norm_val is None:
            continue
        try:
            db_val = normalized_to_db(float(norm_val), gain_cal)
        except (ValueError, TypeError):
            continue
        if db_val <= MASKING_CONSTRAINT_THRESHOLD_DB:
            constraints[reaeq_idx] = round(db_val, 2)

    return constraints


# ReaEQ default 5-band names (fallback when no expanded calibration is available).
DEFAULT_REAEQ_BAND_NAMES = ["Low Shelf", "Band 2", "Band 3", "High Shelf 4", "High Pass 5"]

# Fallback map: 10 analysis bands -> 5 default ReaEQ bands (merge adjacent).
DEFAULT_BAND_TO_REAEQ = {
    0: 0, 1: 0,   # Sub + Bass → Low Shelf
    2: 1, 3: 1,   # Low-Mid + Mid → Band 2
    4: 2, 5: 2,   # Mid + Upper-Mid → Band 3
    6: 3, 7: 3,   # Presence + Presence → High Shelf 4
    # Keep Air/Brilliance on the top bell/shelf lane, not High Pass 5.
    # HP movement can become destructive when type switching is unavailable.
    8: 3, 9: 3,
}


def _merge_cuts_to_reaeq(cuts: list[dict], calibration: dict) -> dict[int, dict]:
    """Map analysis cuts onto ReaEQ bands, keeping the deepest cut per band."""
    reaeq_cuts: dict[int, dict] = {}
    band_map = _band_to_reaeq_map(calibration)
    for cut in cuts:
        reaeq_idx = band_map.get(cut["band_index"])
        if reaeq_idx is None:
            continue
        if reaeq_idx not in reaeq_cuts or cut["cut_db"] < reaeq_cuts[reaeq_idx]["cut_db"]:
            reaeq_cuts[reaeq_idx] = cut
    return reaeq_cuts


def _merge_moves_to_reaeq(moves: list[dict], calibration: dict) -> dict[int, dict]:
    """Map analysis moves onto ReaEQ bands, keeping the strongest absolute move."""
    reaeq_moves: dict[int, dict] = {}
    band_map = _band_to_reaeq_map(calibration)
    for move in moves:
        reaeq_idx = band_map.get(move["band_index"])
        if reaeq_idx is None:
            continue
        prev = reaeq_moves.get(reaeq_idx)
        if prev is None or abs(move["gain_db"]) > abs(prev["gain_db"]):
            reaeq_moves[reaeq_idx] = move
    return reaeq_moves


def _find_reaeq_merge_conflicts(moves: list[dict], calibration: dict) -> list[dict]:
    """Describe moves dropped when multiple analysis bands map to one ReaEQ band."""
    band_map = _band_to_reaeq_map(calibration)
    band_names = _reaeq_band_names(calibration)
    grouped: dict[int, list[dict]] = {}
    for move in moves:
        reaeq_idx = band_map.get(move["band_index"])
        if reaeq_idx is None:
            continue
        grouped.setdefault(reaeq_idx, []).append(move)

    conflicts: list[dict] = []
    for reaeq_idx, group in sorted(grouped.items()):
        if len(group) <= 1:
            continue
        ranked = sorted(group, key=lambda m: abs(m.get("gain_db", 0.0)), reverse=True)
        conflicts.append({
            "reaeq_band_index": reaeq_idx,
            "reaeq_band_name": (
                band_names[reaeq_idx]
                if reaeq_idx < len(band_names)
                else f"Band {reaeq_idx + 1}"
            ),
            "kept_move": ranked[0],
            "dropped_moves": ranked[1:],
            "reason": "multiple_analysis_bands_map_to_single_reaeq_band",
        })
    return conflicts


def _representative_freq(cut: dict) -> float:
    """Geometric mean of band edges as the representative frequency."""
    return math.sqrt(cut["lo"] * cut["hi"])


def _representative_freq_for_reaeq_band(reaeq_idx: int, calibration: dict) -> float | None:
    """Representative frequency for a ReaEQ band based on mapped analysis bands."""
    mapped_analysis = [
        ai for ai, ri in _band_to_reaeq_map(calibration).items()
        if int(ri) == int(reaeq_idx)
    ]
    if not mapped_analysis:
        return None
    lo = BAND_EDGES[min(mapped_analysis)]
    hi = BAND_EDGES[max(mapped_analysis) + 1]
    return math.sqrt(lo * hi)


def _estimate_makeup_db_raw(
    cuts: list[dict],
    calibration: dict,
) -> float:
    """Raw makeup estimate before bias/floor/cap."""
    reaeq_cuts = _merge_cuts_to_reaeq(cuts, calibration)
    if not reaeq_cuts:
        return 0.0
    total_bands = len(BAND_EDGES) - 1  # 10 analysis bands
    avg_cut = sum(c["cut_db"] for c in reaeq_cuts.values()) / len(reaeq_cuts)
    return -avg_cut * len(reaeq_cuts) / total_bands


def estimate_makeup_db(
    cuts: list[dict],
    calibration: dict,
    max_makeup_db: float = MAKEUP_MAX_DB,
) -> float:
    """Estimate selective-makeup target dB from applied cuts."""
    raw_makeup_db = _estimate_makeup_db_raw(cuts, calibration)
    return _makeup_target_db(raw_makeup_db, max_makeup_db=max_makeup_db)


def _eligible_makeup_reaeq_bands(
    cut_reaeq_indices: set[int],
    calibration: dict,
) -> list[int]:
    """ReaEQ bands eligible for selective makeup (mapped + uncut only)."""
    mapped = sorted({
        int(ri) for ri in _band_to_reaeq_map(calibration).values()
        if isinstance(ri, (int, float))
    })
    band_count = len(_reaeq_band_names(calibration))
    return [bi for bi in mapped if 0 <= bi < band_count and bi not in cut_reaeq_indices]


def _compute_selective_makeup_boosts(
    target_makeup_db: float,
    cut_reaeq_indices: set[int],
    calibration: dict,
    per_band_cap_db: float = MAKEUP_PER_BAND_CAP_DB,
) -> tuple[dict[int, float], float, float]:
    """Distribute selective makeup onto uncut ReaEQ bands with caps.

    Returns:
      (boosts_by_reaeq_band, applied_makeup_db_estimate, unapplied_makeup_db)
    """
    if target_makeup_db <= 0:
        return {}, 0.0, 0.0

    eligible = _eligible_makeup_reaeq_bands(cut_reaeq_indices, calibration)
    if not eligible:
        return {}, 0.0, target_makeup_db

    # Treat target as average boost intent across eligible bands.
    remaining_budget_db = target_makeup_db * len(eligible)
    boosts = {bi: 0.0 for bi in eligible}
    active = set(eligible)
    max_rank = max(len(eligible) - 1, 1)
    weights = {
        bi: (0.8 + 0.5 * (rank / max_rank))
        for rank, bi in enumerate(eligible)
    }

    while remaining_budget_db > 1e-9 and active:
        total_weight = sum(weights[bi] for bi in active)
        if total_weight <= 0:
            break

        allocations = {
            bi: remaining_budget_db * (weights[bi] / total_weight)
            for bi in active
        }
        used = 0.0
        for bi in list(active):
            room = max(0.0, per_band_cap_db - boosts[bi])
            if room <= 1e-9:
                active.remove(bi)
                continue
            add = min(room, allocations[bi])
            if add > 1e-9:
                boosts[bi] += add
                used += add
            if per_band_cap_db - boosts[bi] <= 1e-9:
                active.remove(bi)

        if used <= 1e-9:
            break
        remaining_budget_db -= used

    boosts = {bi: db for bi, db in boosts.items() if db > 1e-6}
    applied_makeup_db = (
        sum(boosts.values()) / len(eligible)
        if eligible else 0.0
    )
    unapplied = max(0.0, target_makeup_db - applied_makeup_db)
    return boosts, applied_makeup_db, unapplied


def compute_reaeq_makeup_profile(
    cuts: list[dict],
    calibration: dict,
    *,
    makeup_mode: str = "off",
    allow_selective_makeup: bool = True,
    max_makeup_db: float = MAKEUP_MAX_DB,
) -> dict:
    """Compute selective makeup profile and per-band boosts for ReaEQ."""
    reaeq_cuts = _merge_cuts_to_reaeq(cuts, calibration)
    has_cut = bool(reaeq_cuts)
    raw_makeup_db = _estimate_makeup_db_raw(cuts, calibration) if has_cut else 0.0
    target_makeup_db = (
        _makeup_target_db(raw_makeup_db, max_makeup_db=max_makeup_db)
        if makeup_mode == "auto" and has_cut and allow_selective_makeup
        else 0.0
    )

    policy = "off"
    selective_boosts: dict[int, float] = {}
    applied_makeup_db = 0.0
    unapplied_makeup_db = 0.0
    if makeup_mode == "auto" and has_cut:
        if allow_selective_makeup:
            policy = "selective"
            selective_boosts, applied_makeup_db, unapplied_makeup_db = _compute_selective_makeup_boosts(
                target_makeup_db=target_makeup_db,
                cut_reaeq_indices=set(reaeq_cuts.keys()),
                calibration=calibration,
                per_band_cap_db=MAKEUP_PER_BAND_CAP_DB,
            )
        else:
            policy = "hybrid_disabled"

    return {
        "policy": policy,
        "raw_makeup_db": raw_makeup_db,
        "target_makeup_db": target_makeup_db,
        "applied_makeup_db": applied_makeup_db,
        "unapplied_makeup_db": unapplied_makeup_db,
        "boosts_by_reaeq_band": selective_boosts,
    }


def _build_eq_params_for_reaeq_gains(
    gain_db_by_reaeq: dict[int, float],
    calibration: dict,
    reaeq_cuts: dict[int, dict] | None = None,
) -> list[dict]:
    """Build ReaEQ set_param entries from explicit ReaEQ-band gain values."""
    gain_cal = calibration["gain"]
    freq_cal = calibration.get("freq", [])
    band_type_norm = calibration.get("band_type_norm")
    band_names = _reaeq_band_names(calibration)
    visible_bands_norm = calibration.get("visible_bands_norm")
    params = []

    if isinstance(visible_bands_norm, (int, float)):
        params.append({"name": "Visible bands", "value": float(visible_bands_norm)})

    cuts_lookup = reaeq_cuts or {}
    for reaeq_idx in sorted(gain_db_by_reaeq):
        if reaeq_idx >= len(band_names):
            continue
        band_name = band_names[reaeq_idx]
        gain_db = float(gain_db_by_reaeq[reaeq_idx])

        if band_type_norm is not None:
            params.append({"name": f"Type-{band_name}", "value": band_type_norm})

        gain_norm = db_to_normalized(gain_db, gain_cal)
        params.append({"name": f"Gain-{band_name}", "value": gain_norm})

        cut = cuts_lookup.get(reaeq_idx)
        if cut is not None:
            target_hz = _representative_freq(cut)
        else:
            target_hz = _representative_freq_for_reaeq_band(reaeq_idx, calibration)
        if target_hz is not None:
            freq_norm = hz_to_normalized(target_hz, freq_cal)
            params.append({"name": f"Freq-{band_name}", "value": freq_norm})

    return params


def build_reaeq_gain_moves(
    cuts: list[dict],
    calibration: dict,
    *,
    makeup_mode: str = "off",
    allow_selective_makeup: bool = True,
) -> tuple[dict[int, float], dict]:
    """Build final ReaEQ per-band gain moves and makeup metadata."""
    reaeq_cuts = _merge_cuts_to_reaeq(cuts, calibration)
    gain_moves = {bi: float(cut["cut_db"]) for bi, cut in reaeq_cuts.items()}
    makeup = compute_reaeq_makeup_profile(
        cuts,
        calibration,
        makeup_mode=makeup_mode,
        allow_selective_makeup=allow_selective_makeup,
    )
    for bi, boost_db in makeup["boosts_by_reaeq_band"].items():
        if bi not in gain_moves:
            gain_moves[bi] = float(boost_db)
    return gain_moves, makeup


def build_eq_params(
    cuts: list[dict],
    calibration: dict,
    extra_gains: dict[int, float] | None = None,
) -> list[dict]:
    """Build ReaEQ parameter set_param entries from computed cuts.

    Args:
        cuts: List of per-band cuts from compute_masking().
        calibration: Dict with "gain" and "freq" calibration tables.

    Returns list of {"name": str, "value": float} for each band that needs a move.
    Sets gain and frequency, and sets band type only when calibration exposes it.
    """
    reaeq_cuts = _merge_cuts_to_reaeq(cuts, calibration)
    gain_db_by_reaeq = {bi: float(cut["cut_db"]) for bi, cut in reaeq_cuts.items()}
    for bi, gain_db in (extra_gains or {}).items():
        if bi not in gain_db_by_reaeq:
            gain_db_by_reaeq[int(bi)] = float(gain_db)
    return _build_eq_params_for_reaeq_gains(
        gain_db_by_reaeq,
        calibration,
        reaeq_cuts=reaeq_cuts,
    )


def build_eq_params_for_moves(
    moves: list[dict],
    calibration: dict,
) -> list[dict]:
    """Build ReaEQ set_param entries from generic per-band moves (cuts/boosts)."""
    gain_cal = calibration["gain"]
    freq_cal = calibration.get("freq", [])
    band_type_norm = calibration.get("band_type_norm")
    reaeq_moves = _merge_moves_to_reaeq(moves, calibration)
    band_names = _reaeq_band_names(calibration)
    visible_bands_norm = calibration.get("visible_bands_norm")
    params = []

    # Ensure expanded ReaEQ layout before per-band writes (best-effort).
    if isinstance(visible_bands_norm, (int, float)):
        params.append({"name": "Visible bands", "value": float(visible_bands_norm)})

    for reaeq_idx in sorted(reaeq_moves):
        move = reaeq_moves[reaeq_idx]
        if reaeq_idx >= len(band_names):
            continue
        band_name = band_names[reaeq_idx]

        if band_type_norm is not None:
            params.append({"name": f"Type-{band_name}", "value": band_type_norm})

        gain_norm = db_to_normalized(move["gain_db"], gain_cal)
        params.append({"name": f"Gain-{band_name}", "value": gain_norm})

        target_hz = _representative_freq(move)
        freq_norm = hz_to_normalized(target_hz, freq_cal)
        params.append({"name": f"Freq-{band_name}", "value": freq_norm})

        # Set bandwidth: narrower for boosts (surgical), wider for cuts
        bw_oct = move.get("bw_oct")
        if bw_oct is None:
            if move["gain_db"] > 0:
                bw_oct = COMPL_BOOST_BW_OCT
            else:
                bw_oct = COMPL_CUT_BW_OCT
        bw_norm = bw_oct / 4.0  # ReaEQ: 0-1 maps to 0-4 octaves
        params.append({"name": f"BW-{band_name}", "value": round(bw_norm, 4)})

    return params


def _split_visible_bands_param(params: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split out optional Visible bands param so it can be applied first."""
    visible = [p for p in params if p.get("name") == "Visible bands"][:1]
    rest = [p for p in params if p.get("name") != "Visible bands"]
    return visible, rest


def _build_reaeq_layout_steps(calibration: dict, fx_index: int = 0) -> list[dict]:
    """Optional pre-steps to force ReaEQ into the calibrated band layout."""
    steps: list[dict] = []
    preset_name = calibration.get("layout_preset_name")
    if isinstance(preset_name, str) and preset_name.strip():
        steps.append({
            "action": "set_preset",
            "fx_index": fx_index,
            "preset_name": preset_name,
        })
    return steps


# ---------------------------------------------------------------------------
# Complementary EQ helpers
# ---------------------------------------------------------------------------

def _band_linear(db_value: float) -> float:
    """Convert dB to linear band energy with floor protection."""
    return 10 ** (max(-120.0, float(db_value)) / 10.0)


def _classify_compl_family(track_name: str) -> str | None:
    """Classify a track into a complementary family, or None if unsupported."""
    name_lower = track_name.lower()
    tokens = _tokenize_name(track_name)

    if "guitar" in name_lower or "gtr" in tokens:
        return "guitar"

    key_tokens = {"keys", "piano", "synth", "pad", "string", "organ", "rhodes", "ep"}
    if any(tok in key_tokens for tok in tokens):
        return "keys-synth"
    # Substring fallback catches concatenated names like "PianoLayer".
    if any(k in name_lower for k in ("keys", "piano", "synth", "pad", "string", "organ", "rhodes")):
        return "keys-synth"

    vocal_tokens = {"vocal", "vocals", "vox", "voice", "sing", "singer", "bck", "bgv", "lead"}
    if any(tok in vocal_tokens for tok in tokens):
        return "vocal"
    if any(k in name_lower for k in ("vocal", "vox", "voice", "ohno", "bckgr", "bgvox")):
        return "vocal"

    return None


def _normalize_role_name(role: str) -> str | None:
    """Normalize user role aliases to canonical role names."""
    if not role:
        return None
    return ROLE_ALIAS.get(role.strip().lower())


def _score_role_for_track(bands: list[dict], role_bands: set[int]) -> float:
    """Role score from summed linear energy in target role bands."""
    return sum(
        _band_linear(bands[idx].get("avg_db", -120))
        for idx in role_bands
        if 0 <= idx < len(bands)
    )


def _assign_complementary_roles(
    family: str,
    analysis_rows: list[dict],
    role_overrides: dict[str, str] | None = None,
    role_hints: dict[str, str] | None = None,
) -> dict[int, str]:
    """Assign complementary roles within a family using overrides + greedy scoring.

    role_overrides: hard lock — forces a track into the specified role.
    role_hints: soft nudge — adds a small bias (15% of max score) when audio
                evidence is ambiguous, but doesn't override clear audio evidence.
    """
    HINT_BIAS_FRACTION = 0.15

    overrides = role_overrides or {}
    canonical_overrides = {
        name.lower(): role
        for name, role in overrides.items()
        if _normalize_role_name(role) is not None
    }
    hints = role_hints or {}
    canonical_hints = {
        name.lower(): _normalize_role_name(role)
        for name, role in hints.items()
        if _normalize_role_name(role) is not None
    }

    role_bands = COMPL_ROLE_BANDS[family]
    rows = [r for r in analysis_rows if isinstance(r.get("index"), (int, float))]
    roles: dict[int, str] = {}
    unassigned = list(rows)

    # Apply explicit overrides first.
    for row in rows:
        name = str(row.get("track", ""))
        idx = int(row["index"])
        override_role = _normalize_role_name(canonical_overrides.get(name.lower(), ""))
        if override_role is None:
            continue
        roles[idx] = override_role
        if row in unassigned:
            unassigned.remove(row)

    # Auto-assign primary lanes.
    for role_name in ("anchor", "presence", "texture"):
        if not unassigned:
            break
        candidates = []
        for row in unassigned:
            bands = row["bands"]
            score = _score_role_for_track(bands, role_bands[role_name])
            candidates.append((row, score))
        # Apply hint bias: nudge hinted tracks toward their preferred role.
        max_score = max((s for _, s in candidates), default=0.0)
        if max_score > 0 and canonical_hints:
            hint_bias = max_score * HINT_BIAS_FRACTION
            candidates = [
                (row, score + hint_bias)
                if canonical_hints.get(str(row.get("track", "")).lower()) == role_name
                else (row, score)
                for row, score in candidates
            ]
        candidates.sort(key=lambda t: t[1], reverse=True)
        if not candidates:
            continue
        chosen_row = candidates[0][0]
        roles[int(chosen_row["index"])] = role_name
        unassigned.remove(chosen_row)

    # Remaining tracks are support.
    for row in unassigned:
        roles[int(row["index"])] = "support"

    return roles


def _family_band_means(analysis_rows: list[dict]) -> list[float]:
    """Mean dB per analysis band for a family."""
    if not analysis_rows:
        return [-120.0] * (len(BAND_EDGES) - 1)
    num_bands = len(analysis_rows[0]["bands"])
    means = []
    for b in range(num_bands):
        vals = [row["bands"][b].get("avg_db", -120) for row in analysis_rows]
        means.append(sum(vals) / max(1, len(vals)))
    return means


def _compute_family_aggregate_spectrum(
    family_analysis: dict[str, list[dict]],
) -> dict[str, list[float]]:
    """Compute per-family max-envelope dB spectrum from grouped analysis results.

    For each band, takes the max dB across all tracks in the family.
    This prevents a quiet track from diluting a loud track's contribution.

    Args:
        family_analysis: {family_name: [analysis_result_dicts]}

    Returns:
        {family_name: [10 max_dB values]}
    """
    result = {}
    for fam, rows in family_analysis.items():
        if not rows:
            result[fam] = [-120.0] * (len(BAND_EDGES) - 1)
            continue
        num_bands = len(rows[0]["bands"])
        maxes = []
        for b in range(num_bands):
            vals = [row["bands"][b].get("avg_db", -120.0) for row in rows]
            maxes.append(max(vals))
        result[fam] = maxes
    return result


def _assign_spectral_lanes(
    family_spectra: dict[str, list[float]],
    family_analysis_rows: dict[str, list[dict]] | None = None,
) -> dict:
    """Assign spectral lane ownership for contested bands.

    For each contested band (where 2+ families have boost-eligible roles):
    - Families below the activity floor are excluded.
    - Per-family normalization: subtract each family's own mean so spectral
      *shape* is compared, not absolute level.
    - The family with the strongest normalized energy owns the band if its
      margin over the second-strongest is >= LANE_DOMINANCE_THRESHOLD_DB.
    - Otherwise the band is shared (no blocking).

    Returns dict with:
        lane_owners: {band_index: family_name | None}
        lane_constraints: {family_name: {band_index: owning_family}}
        contested_bands: list of per-band audit detail dicts
    """
    # --- Activity gate: determine which families are active ---
    active_families: set[str] = set()
    for fam in family_spectra:
        if family_analysis_rows is not None and fam in family_analysis_rows:
            # Per-track peak: active if ANY track has peak >= floor
            rows = family_analysis_rows[fam]
            if any(_analysis_peak_db(row) >= LANE_ACTIVITY_FLOOR_DB for row in rows):
                active_families.add(fam)
        else:
            # Fallback: check max of aggregate spectrum
            spectrum = family_spectra[fam]
            if max(spectrum) >= LANE_ACTIVITY_FLOOR_DB:
                active_families.add(fam)

    if len(active_families) < 2:
        return {
            "lane_owners": {},
            "lane_constraints": {},
            "contested_bands": [],
        }

    # --- Sanitize and normalize per-family ---
    def _sanitize(v: float) -> float:
        if not math.isfinite(v):
            return -120.0
        return v

    active_spectra: dict[str, list[float]] = {}
    normalized_spectra: dict[str, list[float]] = {}
    for fam in active_families:
        raw = [_sanitize(v) for v in family_spectra[fam]]
        active_spectra[fam] = raw
        fam_mean = sum(raw) / len(raw)
        normalized_spectra[fam] = [v - fam_mean for v in raw]

    lane_owners: dict[int, str | None] = {}
    contested_detail: list[dict] = []

    for band_idx, contesting_fams in LANE_CONTESTED_BANDS.items():
        # Only consider families that are active
        present = [f for f in contesting_fams if f in active_spectra]
        if len(present) < 2:
            continue

        # Gather normalized energy and sort by (energy desc, priority desc)
        entries = []
        for fam in present:
            norm_energy = normalized_spectra[fam][band_idx] if band_idx < len(normalized_spectra[fam]) else -120.0
            raw_energy = active_spectra[fam][band_idx] if band_idx < len(active_spectra[fam]) else -120.0
            priority = LANE_FAMILY_PRIORITY.get(fam, 0)
            entries.append((fam, norm_energy, raw_energy, priority))
        entries.sort(key=lambda e: (e[1], e[3]), reverse=True)

        strongest_fam, strongest_norm, _, _ = entries[0]
        second_norm = entries[1][1]
        margin = strongest_norm - second_norm

        if margin >= LANE_DOMINANCE_THRESHOLD_DB:
            owner = strongest_fam
        else:
            owner = None  # shared

        lane_owners[band_idx] = owner
        contested_detail.append({
            "band_index": band_idx,
            "label": BAND_LABELS[band_idx] if band_idx < len(BAND_LABELS) else f"Band {band_idx}",
            "energies": {e[0]: round(e[2], 3) for e in entries},
            "normalized_energies": {e[0]: round(e[1], 3) for e in entries},
            "margin_db": round(margin, 3),
            "owner": owner,
            "decision": "owned" if owner else "shared",
        })

    # Build per-family constraint map: which bands is each family blocked from?
    lane_constraints: dict[str, dict[int, str]] = {}
    for band_idx, owner in lane_owners.items():
        if owner is None:
            continue
        # Block all other active families that contest this band
        contesting = LANE_CONTESTED_BANDS.get(band_idx, [])
        for fam in contesting:
            if fam == owner or fam not in active_spectra:
                continue
            lane_constraints.setdefault(fam, {})[band_idx] = owner

    return {
        "lane_owners": lane_owners,
        "lane_constraints": lane_constraints,
        "contested_bands": contested_detail,
    }


def _compute_complementary_moves_with_details(
    family: str,
    role: str,
    track_bands: list[dict],
    family_means_db: list[float],
    max_cut_db: float,
    max_boost_db: float,
    max_moves: int,
    masking_constraints: dict[int, float] | None = None,
    band_to_reaeq: dict[int, int] | None = None,
    boost_only: bool = False,
    lane_constraints: dict[int, str] | None = None,
) -> dict:
    """Compute complementary moves (cuts + boosts) for one track with audit details.

    masking_constraints: optional dict of ReaEQ band index → cut dB from existing
    [AutoEQ] masking EQ. Boosts on constrained bands are blocked to avoid undoing
    masking work.
    boost_only: when True, skip crowding-reduction cuts entirely (hybrid mode).
    lane_constraints: optional dict of analysis band index → owning family name.
    Boosts on lane-blocked bands are skipped (inter-family spectral lane ownership).
    """
    # "support" intentionally has no target bands: trim crowding only, no boosts.
    role_targets = COMPL_ROLE_BANDS[family].get(role, set())
    _masking = masking_constraints or {}
    band_map = band_to_reaeq or DEFAULT_BAND_TO_REAEQ
    cut_candidates: list[dict] = []
    boost_candidates: list[dict] = []
    band_decisions: list[dict] = []

    for b, band in enumerate(track_bands):
        lo = band.get("lo", 0)
        hi = band.get("hi", 0)
        track_db = band.get("avg_db", -120)
        family_db = family_means_db[b] if b < len(family_means_db) else -120
        delta_db = track_db - family_db
        in_role_target = b in role_targets

        detail = {
            "band_index": b,
            "label": BAND_LABELS[b] if b < len(BAND_LABELS) else f"Band {b}",
            "lo": lo,
            "hi": hi,
            "track_db": round(track_db, 3),
            "family_mean_db": round(family_db, 3),
            "delta_db": round(delta_db, 3),
            "role": role,
            "target_band": in_role_target,
        }

        # Non-target bands: trim crowding (skipped in boost_only/hybrid mode).
        # Support role gets heavier cuts to make room for other roles.
        cut_scale = COMPL_SUPPORT_CUT_SCALE if role == "support" else COMPL_CUT_SCALE
        if boost_only and (not in_role_target) and delta_db >= COMPL_CROWDING_THRESHOLD_DB:
            detail["decision"] = "skip"
            detail["reason"] = "boost_only_no_cuts"
            band_decisions.append(detail)
            continue
        if (not in_role_target) and delta_db >= COMPL_CROWDING_THRESHOLD_DB:
            cut_db = -min(abs(max_cut_db), delta_db * cut_scale)
            if abs(cut_db) >= COMPL_MIN_MOVE_DB:
                move = {
                    "band_index": b,
                    "lo": lo,
                    "hi": hi,
                    "gain_db": round(cut_db, 2),
                    "kind": "cut",
                    "reason": "crowding_reduction",
                }
                cut_candidates.append(move)
                detail["decision"] = "candidate_cut"
                detail["gain_db"] = move["gain_db"]
                band_decisions.append(detail)
                continue

        # Masking constraint: block boosts on bands where [AutoEQ] made cuts.
        reaeq_idx_for_band = band_map.get(b)
        masked_cut_db = _masking.get(reaeq_idx_for_band) if reaeq_idx_for_band is not None else None
        if in_role_target and masked_cut_db is not None:
            detail["decision"] = "skip_boost"
            detail["reason"] = "masked_constraint"
            detail["masked_cut_db"] = masked_cut_db
            band_decisions.append(detail)
            continue

        # Lane constraint: block boosts on bands owned by another family.
        _lane = lane_constraints or {}
        lane_owner = _lane.get(b)
        if in_role_target and lane_owner is not None:
            detail["decision"] = "skip_boost"
            detail["reason"] = "lane_blocked"
            detail["lane_owner"] = lane_owner
            band_decisions.append(detail)
            continue

        # Target bands: reinforce weak role lanes with guarded boosts.
        if in_role_target and delta_db <= COMPL_DEFICIT_THRESHOLD_DB:
            boost_db = min(max_boost_db, abs(delta_db) * COMPL_BOOST_SCALE)
            if hi <= 80:
                detail["decision"] = "skip_boost"
                detail["reason"] = "no_sub_boost"
                band_decisions.append(detail)
                continue
            if lo >= 5000:
                boost_db = min(boost_db, COMPL_HIGH_BAND_BOOST_CAP_DB)
            if boost_db >= COMPL_MIN_MOVE_DB:
                move = {
                    "band_index": b,
                    "lo": lo,
                    "hi": hi,
                    "gain_db": round(boost_db, 2),
                    "kind": "boost",
                    "reason": "role_reinforcement",
                }
                boost_candidates.append(move)
                detail["decision"] = "candidate_boost"
                detail["gain_db"] = move["gain_db"]
                band_decisions.append(detail)
                continue

        # Target bands: ownership boost even when not deficit.
        # Gives each role a visible bump in its assigned lane.
        if in_role_target and delta_db < COMPL_OWNERSHIP_THRESHOLD_DB:
            ownership_db = COMPL_OWNERSHIP_BOOST_DB
            if hi <= 80:
                detail["decision"] = "skip_boost"
                detail["reason"] = "no_sub_boost"
                band_decisions.append(detail)
                continue
            if lo >= 5000:
                ownership_db = min(ownership_db, COMPL_HIGH_BAND_BOOST_CAP_DB)
            if ownership_db >= COMPL_MIN_MOVE_DB:
                move = {
                    "band_index": b,
                    "lo": lo,
                    "hi": hi,
                    "gain_db": round(ownership_db, 2),
                    "kind": "boost",
                    "reason": "role_ownership",
                }
                boost_candidates.append(move)
                detail["decision"] = "candidate_boost"
                detail["gain_db"] = move["gain_db"]
                band_decisions.append(detail)
                continue

        detail["decision"] = "skip"
        detail["reason"] = "no_action"
        band_decisions.append(detail)

    # Keep boosts constrained and musical.
    boost_candidates.sort(key=lambda m: m["gain_db"], reverse=True)
    boost_candidates = boost_candidates[:COMPL_MAX_BOOST_BANDS]

    if boost_candidates:
        mean_boost = sum(m["gain_db"] for m in boost_candidates) / len(boost_candidates)
        if mean_boost > COMPL_TARGET_BOOST_MEAN_DB:
            scale = COMPL_TARGET_BOOST_MEAN_DB / mean_boost
            for move in boost_candidates:
                move["gain_db"] = round(move["gain_db"] * scale, 2)
                move["reason"] = "boost_mean_clamped"

    cuts_sorted = sorted(cut_candidates, key=lambda m: abs(m["gain_db"]), reverse=True)
    boosts_sorted = sorted(boost_candidates, key=lambda m: abs(m["gain_db"]), reverse=True)
    merged = cuts_sorted + boosts_sorted
    merged.sort(key=lambda m: abs(m["gain_db"]), reverse=True)
    selected = merged[:max_moves]
    selected_band_indices = {m["band_index"] for m in selected}

    for detail in band_decisions:
        if detail["decision"].startswith("candidate"):
            if detail["band_index"] in selected_band_indices:
                detail["decision"] = "selected"
                detail["reason"] = "top_n_selected"
            else:
                detail["decision"] = "skip"
                detail["reason"] = "top_n_limit"

    return {"moves": selected, "band_decisions": band_decisions}


def _describe_moves(moves: list[dict]) -> str:
    """Readable summary of complementary moves by kind and band."""
    if not moves:
        return "no moves"
    boosts = []
    cuts = []
    for move in moves:
        bi = move.get("band_index", 0)
        label = BAND_LABELS[bi].lower() if bi < len(BAND_LABELS) else f"band {bi}"
        if move.get("gain_db", 0) >= 0:
            if label not in boosts:
                boosts.append(label)
        else:
            if label not in cuts:
                cuts.append(label)
    chunks = []
    if cuts:
        chunks.append(f"trimmed {', '.join(cuts)}")
    if boosts:
        chunks.append(f"lifted {', '.join(boosts)}")
    return " and ".join(chunks) if chunks else "no moves"

# ---------------------------------------------------------------------------
# Orchestrator: idempotent auto-EQ application
# ---------------------------------------------------------------------------

def analyze_track(
    track_name: str,
    time_start: float | None = None,
    time_end: float | None = None,
    send_command_fn: Callable = _default_send_command,
    *,
    track_index: int | None = None,
) -> dict:
    """Analyze a single track's spectral content.

    When *track_index* is provided, targets the track by numeric index
    (immune to duplicate-name collisions).  Otherwise falls back to
    name-based strict matching.

    Returns the raw analysis result dict from the daemon.
    """
    track_id: str | int = track_index if track_index is not None else track_name
    kwargs: dict = {"track": track_id, "strict": True, "timeout": ANALYSIS_TIMEOUT}
    if time_start is not None:
        kwargs["time_start"] = time_start
    if time_end is not None:
        kwargs["time_end"] = time_end

    # If another client or a previous interrupted run has analysis in flight,
    # wait/retry instead of failing immediately on single-flight guard.
    deadline = time.monotonic() + ANALYSIS_BUSY_RETRY_TIMEOUT
    last_result: dict | None = None
    while True:
        result = send_command_fn("analyze_track", **kwargs)
        last_result = result
        if result.get("status") == "ok":
            return result

        errors = result.get("errors", [])
        error_text = "; ".join(str(e) for e in errors)
        if "Analysis already in progress" not in error_text:
            return result

        if time.monotonic() >= deadline:
            return {
                "status": "error",
                "errors": [
                    "Timed out waiting for existing analysis to finish "
                    f"while analyzing '{track_name}'"
                ],
            }
        time.sleep(ANALYSIS_BUSY_RETRY_INTERVAL)


def auto_eq(
    priority_track: str,
    yield_tracks: list[str],
    max_cut_db: float = PAIR_MAX_CUT_DB,
    aggressiveness: float = PAIR_AGGRESSIVENESS,
    max_cuts: int = PAIR_MAX_CUTS,
    makeup_mode: str = PAIR_MAKEUP_MODE,
    time_start: float | None = None,
    time_end: float | None = None,
    send_command_fn: Callable = _default_send_command,
) -> dict:
    """Run auto-EQ: analyze tracks, compute masking, apply corrective EQ.

    Args:
        priority_track: Track name that should be heard clearly.
        yield_tracks: Track names that should yield spectral space.
        max_cut_db: Maximum EQ cut in dB (negative).
        aggressiveness: Cut depth multiplier (1.0 = normal).
        max_cuts: Maximum number of EQ cuts applied per yield track.
        makeup_mode: "off" or "auto" selective makeup on uncut bands.
        time_start: Analysis start time in seconds (None = project start).
        time_end: Analysis end time in seconds (None = project end).
        send_command_fn: IPC function (for testing).

    Returns:
        Summary dict with per-track results.
    """
    if makeup_mode not in {"off", "auto"}:
        makeup_mode = "off"

    preflight = _prepare_autoeq_preflight(send_command_fn)
    try:
        # 1. Ensure calibration
        calibration = ensure_calibration(send_command_fn)
        analysis_total = 1 + len(yield_tracks)
        analysis_done = 0
        _safe_log(send_command_fn, f"Auto-EQ analysis: analyzed 0/{analysis_total} tracks")

        # 2. Analyze priority track
        priority_result = analyze_track(
            priority_track, time_start, time_end, send_command_fn
        )
        analysis_done += 1
        _safe_log(
            send_command_fn,
            f"Auto-EQ analysis: analyzed {analysis_done}/{analysis_total} tracks ({priority_track})",
        )
        if priority_result.get("status") != "ok":
            return _finalize_with_preflight(
                {
                    "status": "error",
                    "errors": [f"Priority track analysis failed: {priority_result.get('errors', ['Unknown'])}"],
                },
                preflight,
                send_command_fn,
                "auto_eq",
            )

        priority_bands = priority_result["bands"]
        # Pair mode has a single priority track — wrap it for the
        # contributor-annotation helper which expects a list.
        if "track" not in priority_result:
            priority_result["track"] = priority_track
        priority_peak_db = _analysis_peak_db(priority_result)
        ok_priority_results = (
            [priority_result]
            if _is_active_priority_reference(priority_result)
            else []
        )
        if not ok_priority_results:
            _safe_log(
                send_command_fn,
                (
                    f"Auto-EQ: priority '{priority_track}' inactive "
                    f"(peak={priority_peak_db:.1f} dB < "
                    f"{PRIORITY_REFERENCE_ACTIVE_FLOOR_DB:.1f} dB floor); "
                    "skipping masking cuts."
                ),
            )

        # 3. Process each yield track
        results = []
        all_errors = []
        audit_pairs = []

        for yield_name in yield_tracks:
            # Analyze yield track
            yield_result = analyze_track(
                yield_name, time_start, time_end, send_command_fn
            )
            analysis_done += 1
            _safe_log(
                send_command_fn,
                f"Auto-EQ analysis: analyzed {analysis_done}/{analysis_total} tracks ({yield_name})",
            )
            if yield_result.get("status") != "ok":
                all_errors.append(
                    f"Analysis failed for '{yield_name}': "
                    f"{yield_result.get('errors', ['Unknown'])}"
                )
                audit_pairs.append({
                    "priority_track": priority_track,
                    "priority_index": priority_result.get("index"),
                    "yield_track": yield_name,
                    "yield_index": None,
                    "error": f"Yield analysis failed: {yield_result.get('errors', ['Unknown'])}",
                })
                continue

            yield_bands = yield_result["bands"]
            yield_index = yield_result["index"]
            target_index = yield_index

            # Read FX chain early so stale [AutoEQ-Gain] gets cleaned even when no cuts are found.
            fx_result = send_command_fn(
                "get_track_fx", track=yield_name, strict=True, timeout=10
            )
            if fx_result.get("status") == "ok":
                fx_track_index = fx_result.get("index")
                if isinstance(fx_track_index, (int, float)):
                    target_index = int(fx_track_index)
                chain = fx_result.get("fx_chain", [])
                existing_gain = find_tagged_fx(chain, AUTOEQ_GAIN_TAG)
                if existing_gain:
                    fx_re = send_command_fn(
                        "get_track_fx", track=target_index, strict=True, timeout=10,
                    )
                    if fx_re.get("status") == "ok":
                        eg2 = find_tagged_fx(fx_re.get("fx_chain", []), AUTOEQ_GAIN_TAG)
                        if eg2:
                            send_command_fn(
                                "remove_fx", track=target_index, fx_index=eg2[0], timeout=10,
                            )
            else:
                fx_errors = fx_result.get("errors", ["Unknown error"])
                all_errors.append(
                    f"Pre-flight FX read failed for '{yield_name}': "
                    f"{'; '.join(str(e) for e in fx_errors)}"
                )

            if not ok_priority_results:
                inactive_msg = (
                    f"Priority inactive in analysis window "
                    f"(peak {priority_peak_db:.1f} dB < "
                    f"{PRIORITY_REFERENCE_ACTIVE_FLOOR_DB:.1f} dB floor)"
                )
                results.append({
                    "track": yield_name,
                    "index": target_index,
                    "cuts": [],
                    "message": inactive_msg,
                    "higher_priority_tracks": [],
                    "yielding_to": [],
                    "makeup_db": 0.0,
                    "makeup_policy": "off",
                })
                audit_pairs.append({
                    "priority_track": priority_track,
                    "priority_index": priority_result.get("index"),
                    "priority_peak_db": round(priority_peak_db, 3),
                    "priority_active_floor_db": PRIORITY_REFERENCE_ACTIVE_FLOOR_DB,
                    "yield_track": yield_name,
                    "yield_index": target_index,
                    "message": inactive_msg,
                })
                continue

            # Compute masking cuts + auditable per-band decision details.
            masking = _compute_masking_with_details(
                priority_bands=priority_bands,
                yield_bands=yield_bands,
                max_cut_db=max_cut_db,
                aggressiveness=aggressiveness,
                max_cuts=max_cuts,
            )
            cuts = _annotate_cut_contributors(
                masking["cuts"],
                priority_bands,
                ok_priority_results,
            )
            band_decisions = masking["band_decisions"]

            makeup_profile = compute_reaeq_makeup_profile(
                cuts,
                calibration,
                makeup_mode=makeup_mode,
                allow_selective_makeup=True,
            )
            makeup_db = makeup_profile["target_makeup_db"]
            makeup_applied_db = makeup_profile["applied_makeup_db"]
            makeup_unapplied_db = makeup_profile["unapplied_makeup_db"]
            makeup_policy = makeup_profile["policy"]

            if not cuts:
                results.append({
                    "track": yield_name, "index": yield_index,
                    "cuts": [], "message": "No significant masking detected",
                    "makeup_db": 0.0,
                    "makeup_policy": "off",
                })
                audit_pairs.append({
                    "priority_track": priority_track,
                    "priority_index": priority_result.get("index"),
                    "yield_track": yield_name,
                    "yield_index": yield_index,
                    "priority_bands": priority_bands,
                    "yield_bands": yield_bands,
                    "band_decisions": band_decisions,
                    "selected_cuts": [],
                    "message": "No significant masking detected",
                })
                continue

            # Build EQ params (cuts + selective makeup on uncut bands).
            gain_moves, _ = build_reaeq_gain_moves(
                cuts,
                calibration,
                makeup_mode=makeup_mode,
                allow_selective_makeup=True,
            )
            eq_params = _build_eq_params_for_reaeq_gains(
                gain_moves,
                calibration,
                reaeq_cuts=_merge_cuts_to_reaeq(cuts, calibration),
            )

            # Idempotent: remove existing AutoEQ if present
            if fx_result.get("status") == "ok":
                chain = fx_result.get("fx_chain", [])
                existing = find_auto_eq_fx(chain)
                if existing:
                    remove_result = send_command_fn(
                        "remove_fx", track=target_index, fx_index=existing[0], timeout=10
                    )
                    if remove_result.get("status") != "ok":
                        remove_errors = remove_result.get("errors", ["Unknown error"])
                        all_errors.append(
                            f"Remove existing AutoEQ on '{yield_name}': "
                            f"{'; '.join(str(e) for e in remove_errors)}"
                        )

            # Apply: add ReaEQ + set params
            vis_params, eq_body_params = _split_visible_bands_param(eq_params)
            post_params = [*vis_params, *eq_body_params]
            plan_steps = [{"action": "add_fx", "fx_name": "ReaEQ"}]
            plan_steps.extend(_build_reaeq_layout_steps(calibration, fx_index=0))
            plan = {
                "title": f"AutoEQ: {yield_name} (yield to {priority_track})",
                "steps": plan_steps,
            }
            apply_result = send_command_fn(
                "apply_plan", track=target_index, plan=plan, timeout=30
            )
            apply_errors = [str(e) for e in apply_result.get("errors", [])]
            if apply_errors:
                all_errors.append(f"Apply on '{yield_name}': {'; '.join(apply_errors)}")
            track_apply_status = apply_result.get("status", "unknown")

            if apply_result.get("status") in ("ok", "partial"):
                # Tag the new ReaEQ with [AutoEQ]
                added = apply_result.get("added_fx_indices", [])
                if added:
                    new_fx_idx = added[0]
                    if post_params:
                        param_result = send_command_fn(
                            "set_param",
                            track=target_index,
                            fx_index=new_fx_idx,
                            params=post_params,
                            strict=True,
                            timeout=10,
                        )
                        param_status = param_result.get("status")
                        param_errors = [str(e) for e in param_result.get("errors", [])]
                        if param_status not in ("ok", "partial"):
                            if track_apply_status in ("ok", "partial"):
                                track_apply_status = "partial"
                            err_text = "; ".join(param_errors) if param_errors else "Unknown error"
                            all_errors.append(f"Set params on '{yield_name}': {err_text}")
                            if param_errors:
                                apply_errors.extend(param_errors)
                        elif param_status == "partial":
                            if track_apply_status == "ok":
                                track_apply_status = "partial"
                            if param_errors:
                                all_errors.append(
                                    f"Set params partial on '{yield_name}': "
                                    f"{'; '.join(param_errors)}"
                                )
                                apply_errors.extend(param_errors)
                    rename_result = send_command_fn(
                        "rename_fx", track=target_index, fx_index=new_fx_idx,
                        name=f"ReaEQ {AUTO_EQ_TAG}",
                        timeout=10,
                    )
                    if rename_result.get("status") != "ok":
                        rename_errors = rename_result.get("errors", ["Unknown error"])
                        all_errors.append(
                            f"Rename AutoEQ on '{yield_name}': "
                            f"{'; '.join(str(e) for e in rename_errors)}"
                        )

            results.append({
                "track": yield_name,
                "index": target_index,
                "cuts": cuts,
                "eq_params": eq_params,
                "makeup_db": round(makeup_db, 2),
                "makeup_applied_db": round(makeup_applied_db, 2),
                "makeup_unapplied_db": round(makeup_unapplied_db, 2),
                "makeup_policy": makeup_policy,
                "apply_status": track_apply_status,
                "apply_errors": apply_errors,
            })
            audit_pairs.append({
                "priority_track": priority_track,
                "priority_index": priority_result.get("index"),
                "yield_track": yield_name,
                "yield_index": target_index,
                "priority_bands": priority_bands,
                "yield_bands": yield_bands,
                "band_decisions": band_decisions,
                "selected_cuts": cuts,
                "eq_params": eq_params,
                "makeup_db": round(makeup_db, 2),
                "makeup_applied_db": round(makeup_applied_db, 2),
                "makeup_unapplied_db": round(makeup_unapplied_db, 2),
                "makeup_policy": makeup_policy,
                "apply_status": track_apply_status,
                "apply_errors": apply_errors,
            })

        status = "error" if (not results and all_errors) else "ok"
        if results and all_errors:
            status = "partial"

        # Log detailed summary to REAPER console
        summary_lines = [f"=== Auto-EQ complete (priority: {priority_track}) ==="]
        for r in results:
            cuts = r.get("cuts", [])
            if cuts:
                desc = _describe_cuts(cuts)
                max_db = min(c["cut_db"] for c in cuts)
                summary_lines.append(
                    f"  Ducked {r['track']}'s {desc} to get out of {priority_track}'s way "
                    f"(up to {max_db:.1f} dB)"
                )
            else:
                summary_lines.append(f"  {r['track']}: no masking found")
        send_command_fn("log", message="\n".join(summary_lines), timeout=5)

        audit_payload = {
            "mode": "auto_eq",
            "created_at_utc": _now_utc_iso(),
            "params": {
                "priority_track": priority_track,
                "yield_tracks": yield_tracks,
                "max_cut_db": max_cut_db,
                "aggressiveness": aggressiveness,
                "max_cuts": max_cuts,
                "makeup_mode": makeup_mode,
                "time_start": time_start,
                "time_end": time_end,
            },
            "band_edges": BAND_EDGES,
            "band_labels": BAND_LABELS,
            "pairs": audit_pairs,
            "status": status,
            "errors": all_errors,
        }
        audit_path = _write_audit_artifact("auto_eq", audit_payload)

        return _finalize_with_preflight(
            {
                "status": status,
                "priority_track": priority_track,
                "audit_path": audit_path,
                "results": results,
                "errors": all_errors,
            },
            preflight,
            send_command_fn,
            "auto_eq",
        )
    except Exception as exc:
        return _finalize_with_preflight(
            {"status": "error", "errors": [f"Auto-EQ failed: {exc}"]},
            preflight,
            send_command_fn,
            "auto_eq",
        )


def auto_eq_all(
    max_cut_db: float = ALL_MAX_CUT_DB,
    aggressiveness: float = 1.0,
    max_cuts: int = ALL_MAX_CUTS,
    makeup_mode: str = ALL_MAKEUP_MODE,
    level: str = "leaf",
    time_start: float | None = None,
    time_end: float | None = None,
    strategy: str = "subtractive",
    family: str = "all",
    max_boost_db: float = COMPL_MAX_BOOST_DB,
    role_overrides: dict[str, str] | None = None,
    role_hints: dict[str, str] | None = None,
    send_command_fn: Callable = _default_send_command,
) -> dict:
    """Run auto-EQ across all tracks using priority hierarchy.

    Higher-priority tracks get spectral priority over lower-priority ones.
    Each track yields to all tracks with higher priority.
    Caches analysis results to avoid re-analyzing the same track multiple times.
    level controls target selection:
      - auto: lowest-level buses plus standalone leaves (fallback to leaves)
      - leaf: leaf tracks only
      - bus: lowest-level buses plus standalone leaves
    strategy controls what happens after hierarchy cuts:
      - subtractive: only masking cuts (default, original behavior)
      - hybrid: masking cuts + complementary boosts within instrument families
    """
    if strategy not in ("subtractive", "hybrid"):
        strategy = "subtractive"
    family = (family or "all").lower()
    if family not in SUPPORTED_COMPL_FAMILIES:
        family = "all"
    if strategy == "hybrid":
        makeup_mode = "off"
    if makeup_mode not in {"off", "auto"}:
        makeup_mode = "off"

    # Get all tracks
    ctx = send_command_fn("get_context", timeout=10)
    if ctx.get("status") != "ok":
        return {"status": "error", "errors": ["Failed to get context"]}

    tracks = ctx.get("tracks", [])
    # Filter out temp/internal tracks, muted tracks, and helper tracks.
    tracks = [
        t for t in tracks
        if not t["name"].startswith("__tmp_")
        and not t.get("muted")
        and not is_helper_track(t["name"])
    ]
    selected_tracks, resolved_level = _select_auto_eq_all_targets(tracks, level)
    if len(selected_tracks) < 2:
        return {"status": "error", "errors": ["Need at least 2 tracks"]}

    preflight = _prepare_autoeq_preflight(send_command_fn)

    try:
        return _auto_eq_all_inner(
            tracks, selected_tracks, resolved_level, preflight,
            max_cut_db=max_cut_db, aggressiveness=aggressiveness,
            max_cuts=max_cuts, makeup_mode=makeup_mode, level=level,
            time_start=time_start, time_end=time_end,
            strategy=strategy, family=family, max_boost_db=max_boost_db,
            role_overrides=role_overrides, role_hints=role_hints,
            send_command_fn=send_command_fn,
        )
    except Exception as exc:
        return _finalize_with_preflight(
            {"status": "error", "errors": [f"Auto-EQ-all failed: {exc}"]},
            preflight, send_command_fn, "auto_eq_all",
        )


def _auto_eq_all_inner(
    tracks, selected_tracks, resolved_level, preflight,
    *, max_cut_db, aggressiveness, max_cuts, makeup_mode, level,
    time_start, time_end, strategy="subtractive", family="all",
    max_boost_db=COMPL_MAX_BOOST_DB, role_overrides=None, role_hints=None,
    send_command_fn,
):
    """Inner body of auto_eq_all — extracted for exception-safe preflight restore."""
    # Ensure calibration once upfront — hybrid needs boost range for compl pass
    calibration = ensure_calibration(
        send_command_fn,
        require_boost_range=(strategy == "hybrid"),
    )

    # Score and sort by priority
    scored = [
        (t["name"], t["index"], match_track_priority(t["name"]))
        for t in selected_tracks
    ]
    scored.sort(key=lambda x: x[2], reverse=True)
    analysis_progress = _make_analysis_progress_logger(
        send_command_fn,
        "Auto-EQ All analysis",
        len(scored),
    )
    _safe_log(send_command_fn, f"Auto-EQ All analysis: analyzed 0/{len(scored)} tracks")

    # Analysis cache: keyed by track INDEX to avoid duplicate-name collisions.
    analysis_cache: dict[int, dict] = {}

    # Build an index->track lookup for folder-aware analysis.
    _track_by_idx: dict[int, dict] = {
        int(t["index"]): t for t in tracks
        if isinstance(t.get("index"), (int, float))
    }

    def cached_analyze(track_name: str, track_idx: int) -> dict:
        """Folder-aware cached analysis, keyed by track index.

        If the track is a bus/folder, aggregates leaf children via
        max-envelope.  Otherwise analyzes the track directly.
        """
        if track_idx in analysis_cache:
            return analysis_cache[track_idx]
        analysis_progress(track_name, track_idx)
        track_meta = _track_by_idx.get(track_idx)
        if track_meta and _is_folder_track(track_meta):
            result = _analyze_folder_aggregate(
                track_meta, tracks, time_start, time_end,
                send_command_fn, analysis_cache,
            )
        else:
            result = analyze_track(
                track_name, time_start, time_end, send_command_fn,
                track_index=track_idx,
            )
        analysis_cache[track_idx] = result
        return result

    all_results = []
    all_errors = []
    audit_pairs = []

    # Process: each track yields to ALL tracks with strictly higher priority.
    # higher carries (name, index) tuples so we target by index, not name.
    for i, (name, _idx, priority) in enumerate(scored):
        higher = [(s[0], s[1]) for s in scored[:i] if s[2] > priority]
        if not higher:
            continue  # Top priority track doesn't yield

        # Analyze ALL higher-priority tracks, skip any that fail.
        ok_priority_results: list[dict] = []
        failed_priority_names: list[str] = []
        inactive_priority_names: list[str] = []
        for ref_name, ref_idx in higher:
            ref_result = cached_analyze(ref_name, int(ref_idx))
            if ref_result.get("status") == "ok":
                if _is_active_priority_reference(ref_result):
                    ok_priority_results.append(ref_result)
                else:
                    inactive_priority_names.append(ref_name)
            else:
                failed_priority_names.append(ref_name)

        higher_names = [h[0] for h in higher]
        priority_track = higher_names[0]
        top_ref_index = int(higher[0][1])
        if not ok_priority_results:
            if failed_priority_names:
                all_errors.append(
                    f"No active priority refs for '{name}': "
                    f"failed={failed_priority_names}, inactive={inactive_priority_names}"
                )
            inactive_msg = (
                "No active higher-priority refs in analysis window "
                f"(floor {PRIORITY_REFERENCE_ACTIVE_FLOOR_DB:.1f} dB)"
            )
            all_results.append({
                "track": name,
                "priority_track": priority_track,
                "index": _idx,
                "cuts": [],
                "higher_priority_tracks": [],
                "yielding_to": [],
                "message": inactive_msg,
                "makeup_db": 0.0,
                "makeup_policy": "off",
            })
            audit_pairs.append({
                "priority_track": priority_track,
                "priority_tracks": higher_names,
                "failed_priority_tracks": failed_priority_names,
                "inactive_priority_tracks": inactive_priority_names,
                "priority_index": top_ref_index,
                "yield_track": name,
                "yield_index": _idx,
                "message": inactive_msg,
            })
            continue

        if failed_priority_names:
            all_errors.append(
                f"Some priority refs failed for '{name}': {failed_priority_names}"
            )
        if inactive_priority_names:
            _safe_log(
                send_command_fn,
                (
                    f"Auto-EQ All: ignored inactive priority refs for '{name}' "
                    f"(floor {PRIORITY_REFERENCE_ACTIVE_FLOOR_DB:.1f} dB): "
                    f"{', '.join(inactive_priority_names)}"
                ),
            )

        active_priority_names: list[str] = []
        for ref in ok_priority_results:
            ref_name = str(ref.get("track", "")).strip()
            if ref_name and ref_name not in active_priority_names:
                active_priority_names.append(ref_name)
        if not active_priority_names:
            active_priority_names = higher_names[:1]

        # Aggregate all successful priority analyses via max-envelope.
        if len(ok_priority_results) == 1:
            priority_bands = ok_priority_results[0]["bands"]
        else:
            priority_bands = _aggregate_bands(ok_priority_results)
            if priority_bands is None:
                all_errors.append(
                    f"Band aggregation failed for priority refs of '{name}'"
                )
                audit_pairs.append({
                    "priority_track": priority_track,
                    "priority_tracks": active_priority_names,
                    "failed_priority_tracks": failed_priority_names,
                    "inactive_priority_tracks": inactive_priority_names,
                    "priority_index": top_ref_index,
                    "yield_track": name,
                    "yield_index": _idx,
                    "error": "Band aggregation failed for priority refs",
                })
                continue

        # Analyze yield track (cached, folder-aware)
        yield_result = cached_analyze(name, int(_idx))
        if yield_result.get("status") != "ok":
            all_errors.append(
                f"Analysis failed for '{name}': "
                f"{yield_result.get('errors', ['Unknown'])}"
            )
            audit_pairs.append({
                "priority_track": priority_track,
                "priority_tracks": active_priority_names,
                "failed_priority_tracks": failed_priority_names,
                "inactive_priority_tracks": inactive_priority_names,
                "priority_index": top_ref_index,
                "yield_track": name,
                "yield_index": _idx,
                "error": f"Yield analysis failed: {yield_result.get('errors', ['Unknown'])}",
            })
            continue

        # Compute masking and apply EQ
        yield_bands = yield_result["bands"]
        yield_index = yield_result["index"]
        target_index = yield_index

        # Read FX chain early so stale [AutoEQ-Gain] gets cleaned even when no cuts are found.
        fx_result = send_command_fn(
            "get_track_fx", track=int(target_index), strict=True, timeout=10
        )
        if fx_result.get("status") == "ok":
            chain = fx_result.get("fx_chain", [])
            existing_gain = find_tagged_fx(chain, AUTOEQ_GAIN_TAG)
            if existing_gain:
                fx_re = send_command_fn(
                    "get_track_fx", track=int(target_index), strict=True, timeout=10,
                )
                if fx_re.get("status") == "ok":
                    eg2 = find_tagged_fx(fx_re.get("fx_chain", []), AUTOEQ_GAIN_TAG)
                    if eg2:
                        send_command_fn(
                            "remove_fx", track=int(target_index), fx_index=eg2[0], timeout=10,
                        )
        else:
            fx_errors = fx_result.get("errors", ["Unknown error"])
            all_errors.append(
                f"Pre-flight FX read failed for '{name}': "
                f"{'; '.join(str(e) for e in fx_errors)}"
            )

        masking = _compute_masking_with_details(
            priority_bands=priority_bands,
            yield_bands=yield_bands,
            max_cut_db=max_cut_db,
            aggressiveness=aggressiveness,
            max_cuts=max_cuts,
        )
        cuts = masking["cuts"]
        band_decisions = masking["band_decisions"]

        makeup_profile = compute_reaeq_makeup_profile(
            cuts,
            calibration,
            makeup_mode=makeup_mode,
            allow_selective_makeup=True,
        )
        makeup_db = makeup_profile["target_makeup_db"]
        makeup_applied_db = makeup_profile["applied_makeup_db"]
        makeup_unapplied_db = makeup_profile["unapplied_makeup_db"]
        makeup_policy = makeup_profile["policy"]

        if not cuts:
            all_results.append({
                "track": name, "priority_track": priority_track, "index": yield_index,
                "higher_priority_tracks": active_priority_names,
                "yielding_to": active_priority_names,
                "cuts": [], "message": "No significant masking detected",
                "makeup_db": 0.0,
                "makeup_policy": "off",
            })
            audit_pairs.append({
                "priority_track": priority_track,
                "priority_tracks": active_priority_names,
                "failed_priority_tracks": failed_priority_names,
                "inactive_priority_tracks": inactive_priority_names,
                "priority_index": top_ref_index,
                "yield_track": name,
                "yield_index": yield_index,
                "priority_bands": priority_bands,
                "yield_bands": yield_bands,
                "band_decisions": band_decisions,
                "selected_cuts": [],
                "message": "No significant masking detected",
            })
            continue

        gain_moves, _ = build_reaeq_gain_moves(
            cuts,
            calibration,
            makeup_mode=makeup_mode,
            allow_selective_makeup=True,
        )
        eq_params = _build_eq_params_for_reaeq_gains(
            gain_moves,
            calibration,
            reaeq_cuts=_merge_cuts_to_reaeq(cuts, calibration),
        )

        # Idempotent: remove existing AutoEQ if present (use index, not name)
        if fx_result.get("status") == "ok":
            chain = fx_result.get("fx_chain", [])
            existing = find_auto_eq_fx(chain)
            if existing:
                remove_result = send_command_fn(
                    "remove_fx", track=int(target_index), fx_index=existing[0], timeout=10
                )
                if remove_result.get("status") != "ok":
                    remove_errors = remove_result.get("errors", ["Unknown error"])
                    all_errors.append(
                        f"Remove existing AutoEQ on '{name}': "
                        f"{'; '.join(str(e) for e in remove_errors)}"
                    )

        # Apply: add ReaEQ + set params
        vis_params, eq_body_params = _split_visible_bands_param(eq_params)
        post_params = [*vis_params, *eq_body_params]
        plan_steps = [{"action": "add_fx", "fx_name": "ReaEQ"}]
        plan_steps.extend(_build_reaeq_layout_steps(calibration, fx_index=0))
        plan = {
            "title": f"AutoEQ: {name} (yield to {priority_track})",
            "steps": plan_steps,
        }
        apply_result = send_command_fn(
            "apply_plan", track=target_index, plan=plan, timeout=30
        )
        apply_errors = [str(e) for e in apply_result.get("errors", [])]
        if apply_errors:
            all_errors.append(f"Apply on '{name}': {'; '.join(apply_errors)}")
        track_apply_status = apply_result.get("status", "unknown")

        if apply_result.get("status") in ("ok", "partial"):
            added = apply_result.get("added_fx_indices", [])
            if added:
                new_fx_idx = added[0]
                if post_params:
                    param_result = send_command_fn(
                        "set_param",
                        track=target_index,
                        fx_index=new_fx_idx,
                        params=post_params,
                        strict=True,
                        timeout=10,
                    )
                    param_status = param_result.get("status")
                    param_errors = [str(e) for e in param_result.get("errors", [])]
                    if param_status not in ("ok", "partial"):
                        if track_apply_status in ("ok", "partial"):
                            track_apply_status = "partial"
                        err_text = "; ".join(param_errors) if param_errors else "Unknown error"
                        all_errors.append(f"Set params on '{name}': {err_text}")
                        if param_errors:
                            apply_errors.extend(param_errors)
                    elif param_status == "partial":
                        if track_apply_status == "ok":
                            track_apply_status = "partial"
                        if param_errors:
                            all_errors.append(
                                f"Set params partial on '{name}': "
                                f"{'; '.join(param_errors)}"
                            )
                            apply_errors.extend(param_errors)
                rename_result = send_command_fn(
                    "rename_fx", track=target_index, fx_index=new_fx_idx,
                    name=f"ReaEQ {AUTO_EQ_TAG}",
                    timeout=10,
                )
                if rename_result.get("status") != "ok":
                    rename_errors = rename_result.get("errors", ["Unknown error"])
                    all_errors.append(
                        f"Rename AutoEQ on '{name}': "
                        f"{'; '.join(str(e) for e in rename_errors)}"
                    )

        all_results.append({
            "track": name,
            "priority_track": priority_track,
            "index": target_index,
            "higher_priority_tracks": active_priority_names,
            "yielding_to": active_priority_names,
            "cuts": cuts,
            "eq_params": eq_params,
            "makeup_db": round(makeup_db, 2),
            "makeup_applied_db": round(makeup_applied_db, 2),
            "makeup_unapplied_db": round(makeup_unapplied_db, 2),
            "makeup_policy": makeup_policy,
            "apply_status": track_apply_status,
            "apply_errors": apply_errors,
        })
        audit_pairs.append({
            "priority_track": priority_track,
            "priority_tracks": active_priority_names,
            "failed_priority_tracks": failed_priority_names,
            "inactive_priority_tracks": inactive_priority_names,
            "priority_index": top_ref_index,
            "yield_track": name,
            "yield_index": target_index,
            "priority_bands": priority_bands,
            "yield_bands": yield_bands,
            "band_decisions": band_decisions,
            "selected_cuts": cuts,
            "eq_params": eq_params,
            "makeup_db": round(makeup_db, 2),
            "makeup_applied_db": round(makeup_applied_db, 2),
            "makeup_unapplied_db": round(makeup_unapplied_db, 2),
            "makeup_policy": makeup_policy,
            "apply_status": track_apply_status,
            "apply_errors": apply_errors,
        })

    # -----------------------------------------------------------------------
    # Phase 2: Complementary boosts within instrument families (hybrid only)
    # -----------------------------------------------------------------------
    compl_results = []
    all_compl_errors = []
    compl_audit_families = []

    if strategy == "hybrid":
        _safe_log(send_command_fn, "=== Phase 2: Complementary boosts (hybrid) ===")

        # 3a. Classify selected tracks into families
        family_groups: dict[str, list[dict]] = {}
        for t in selected_tracks:
            fam = _classify_compl_family(t["name"])
            if fam is None:
                continue
            if family != "all" and fam != family:
                continue
            family_groups.setdefault(fam, []).append(t)

        # Keep only families with 2+ members
        family_groups = {k: v for k, v in family_groups.items() if len(v) >= 2}

        # Normalize overrides/hints for compl pass
        compl_overrides = {
            str(k): str(v)
            for k, v in (role_overrides or {}).items()
            if _normalize_role_name(str(v)) is not None
        }
        compl_hints = {
            str(k): str(v)
            for k, v in (role_hints or {}).items()
            if _normalize_role_name(str(v)) is not None
        }

        # Pre-pass: analyze ALL family tracks for inter-family lane assignment
        family_analysis_rows: dict[str, list[dict]] = {}
        for fam_name_pre, fam_tracks_pre in family_groups.items():
            rows_pre: list[dict] = []
            for ft in fam_tracks_pre:
                track_idx = int(ft["index"])
                if track_idx not in analysis_cache:
                    cached_analyze(ft["name"], track_idx)
                analyzed = analysis_cache.get(track_idx)
                if analyzed and analyzed.get("status") == "ok":
                    rows_pre.append(analyzed)
            if rows_pre:
                family_analysis_rows[fam_name_pre] = rows_pre

        # Compute lane assignment
        lane_result: dict = {"lane_owners": {}, "lane_constraints": {}, "contested_bands": []}
        if len(family_analysis_rows) >= 2:
            family_spectra = _compute_family_aggregate_spectrum(family_analysis_rows)
            lane_result = _assign_spectral_lanes(family_spectra, family_analysis_rows)
            lane_summary = {
                b: owner for b, owner in lane_result["lane_owners"].items()
                if owner is not None
            }
            if lane_summary:
                _safe_log(
                    send_command_fn,
                    f"Lane assignment: {lane_summary}",
                )

        for fam_name, fam_tracks in sorted(family_groups.items()):
            fam_errors: list[str] = []

            # 3b. Ensure analysis cache is complete for family tracks
            fam_rows: list[dict] = []
            for ft in fam_tracks:
                track_name = ft["name"]
                track_idx = int(ft["index"])
                if track_idx not in analysis_cache:
                    cached_analyze(track_name, track_idx)
                analyzed = analysis_cache.get(track_idx)
                if analyzed is None or analyzed.get("status") != "ok":
                    msg = f"Analysis failed for '{track_name}'"
                    fam_errors.append(msg)
                    all_compl_errors.append(msg)
                    continue
                fam_rows.append(analyzed)

            if len(fam_rows) < 2:
                all_compl_errors.append(
                    f"Family '{fam_name}' skipped: need at least 2 analyzable tracks"
                )
                compl_audit_families.append({
                    "family": fam_name,
                    "tracks": [t["name"] for t in fam_tracks],
                    "errors": fam_errors + ["Need at least 2 analyzable tracks"],
                })
                continue

            # 3c. Role assignment and band means
            roles_by_index = _assign_complementary_roles(
                fam_name, fam_rows, compl_overrides, compl_hints
            )
            role_assignments = [
                {
                    "index": int(r["index"]),
                    "track": r["track"],
                    "role": roles_by_index.get(int(r["index"]), "support"),
                }
                for r in fam_rows
                if isinstance(r.get("index"), (int, float))
            ]
            family_means_db = _family_band_means(fam_rows)
            family_audit = {
                "family": fam_name,
                "tracks": [r["track"] for r in fam_rows],
                "roles": role_assignments,
                "family_means_db": [round(v, 3) for v in family_means_db],
                "track_results": [],
                "errors": fam_errors,
            }

            # 3c cont. Per-track complementary pass
            for row in fam_rows:
                track_name = row["track"]
                role = roles_by_index.get(int(row["index"]), "support")
                target_index = row.get("index")

                # Read FX chain for masking constraints and stale comp removal
                masking_constraints: dict[int, float] = {}
                preflight_fx = send_command_fn(
                    "get_track_fx", track=int(target_index), strict=True, timeout=10
                )
                preflight_chain = []
                if preflight_fx.get("status") == "ok":
                    preflight_chain = preflight_fx.get("fx_chain", [])
                    masking_constraints = read_masking_constraints(
                        preflight_chain, calibration
                    )

                # Always remove stale [AutoEQ-Comp] first
                if preflight_chain:
                    existing_comp = find_tagged_fx(preflight_chain, AUTO_EQ_COMPL_TAG)
                    if existing_comp:
                        remove_result = send_command_fn(
                            "remove_fx", track=target_index,
                            fx_index=existing_comp[0], timeout=10
                        )
                        if remove_result.get("status") != "ok":
                            all_compl_errors.append(
                                f"Remove stale AutoEQ-Comp on '{track_name}': "
                                f"{'; '.join(str(e) for e in remove_result.get('errors', ['Unknown']))}"
                            )

                details = _compute_complementary_moves_with_details(
                    family=fam_name,
                    role=role,
                    track_bands=row["bands"],
                    family_means_db=family_means_db,
                    max_cut_db=max_cut_db,
                    max_boost_db=max_boost_db,
                    max_moves=COMPL_MAX_MOVES,
                    masking_constraints=masking_constraints,
                    band_to_reaeq=_band_to_reaeq_map(calibration),
                    boost_only=True,
                    lane_constraints=lane_result["lane_constraints"].get(fam_name, {}),
                )
                moves = details["moves"]
                band_decisions = details["band_decisions"]
                merge_conflicts = _find_reaeq_merge_conflicts(moves, calibration)

                # Determine skip reason when no boosts survive
                skip_reason = None
                if not moves:
                    has_masked_skips = any(
                        bd.get("reason") == "masked_constraint"
                        for bd in band_decisions
                    )
                    has_lane_skips = any(
                        bd.get("reason") == "lane_blocked"
                        for bd in band_decisions
                    )
                    if has_masked_skips:
                        skip_reason = "owned_bands_masked"
                    elif has_lane_skips:
                        skip_reason = "lane_blocked"
                    else:
                        skip_reason = "no_boost_candidates"
                    compl_results.append({
                        "track": track_name,
                        "family": fam_name,
                        "role": role,
                        "index": target_index,
                        "moves": [],
                        "message": f"No boosts applied ({skip_reason})",
                    })
                    family_audit["track_results"].append({
                        "track": track_name,
                        "index": target_index,
                        "role": role,
                        "masking_constraints": masking_constraints or None,
                        "band_decisions": band_decisions,
                        "selected_moves": [],
                        "skip_reason": skip_reason,
                        "message": f"No boosts applied ({skip_reason})",
                    })
                    continue

                # Build and apply new [AutoEQ-Comp] ReaEQ
                eq_params = build_eq_params_for_moves(moves, calibration)
                vis_params, eq_body_params = _split_visible_bands_param(eq_params)
                post_params = [*vis_params, *eq_body_params]
                plan_steps = [{"action": "add_fx", "fx_name": "ReaEQ"}]
                plan_steps.extend(_build_reaeq_layout_steps(calibration, fx_index=0))
                plan = {
                    "title": f"AutoEQ-Comp: {track_name} ({fam_name}/{role})",
                    "steps": plan_steps,
                }
                apply_result = send_command_fn(
                    "apply_plan", track=target_index, plan=plan, timeout=30
                )
                apply_errors_compl = [str(e) for e in apply_result.get("errors", [])]
                if apply_errors_compl:
                    all_compl_errors.append(
                        f"Apply compl on '{track_name}': {'; '.join(apply_errors_compl)}"
                    )
                track_apply_status = apply_result.get("status", "unknown")

                if apply_result.get("status") in ("ok", "partial"):
                    added = apply_result.get("added_fx_indices", [])
                    if added:
                        new_fx_idx = added[0]
                        if post_params:
                            param_result = send_command_fn(
                                "set_param",
                                track=target_index,
                                fx_index=new_fx_idx,
                                params=post_params,
                                strict=True,
                                timeout=10,
                            )
                            param_status = param_result.get("status")
                            param_errors = [str(e) for e in param_result.get("errors", [])]
                            if param_status not in ("ok", "partial"):
                                if track_apply_status in ("ok", "partial"):
                                    track_apply_status = "partial"
                                err_text = "; ".join(param_errors) if param_errors else "Unknown"
                                all_compl_errors.append(
                                    f"Set compl params on '{track_name}': {err_text}"
                                )
                                if param_errors:
                                    apply_errors_compl.extend(param_errors)
                            elif param_status == "partial":
                                if track_apply_status == "ok":
                                    track_apply_status = "partial"
                                if param_errors:
                                    all_compl_errors.append(
                                        f"Set compl params partial on '{track_name}': "
                                        f"{'; '.join(param_errors)}"
                                    )
                                    apply_errors_compl.extend(param_errors)
                        rename_result = send_command_fn(
                            "rename_fx",
                            track=target_index,
                            fx_index=new_fx_idx,
                            name=f"ReaEQ {AUTO_EQ_COMPL_TAG}",
                            timeout=10,
                        )
                        if rename_result.get("status") != "ok":
                            all_compl_errors.append(
                                f"Rename AutoEQ-Comp on '{track_name}': "
                                f"{'; '.join(str(e) for e in rename_result.get('errors', ['Unknown']))}"
                            )

                compl_results.append({
                    "track": track_name,
                    "family": fam_name,
                    "role": role,
                    "index": target_index,
                    "moves": moves,
                    "merge_conflicts": merge_conflicts,
                    "eq_params": eq_params,
                    "apply_status": track_apply_status,
                    "apply_errors": apply_errors_compl,
                })
                family_audit["track_results"].append({
                    "track": track_name,
                    "index": target_index,
                    "role": role,
                    "masking_constraints": masking_constraints or None,
                    "band_decisions": band_decisions,
                    "selected_moves": moves,
                    "merge_conflicts": merge_conflicts,
                    "eq_params": eq_params,
                    "apply_status": track_apply_status,
                    "apply_errors": apply_errors_compl,
                })

            compl_audit_families.append(family_audit)

    # -----------------------------------------------------------------------
    # Status computation — combine Phase 1 and Phase 2 errors
    # -----------------------------------------------------------------------
    combined_errors = all_errors + all_compl_errors
    status = "error" if (not all_results and combined_errors) else "ok"
    if all_results and combined_errors:
        status = "partial"

    # Log detailed summary to REAPER console
    summary_lines = [f"=== Auto-EQ All complete (level: {resolved_level}, strategy: {strategy}) ==="]
    for r in all_results:
        cuts = r.get("cuts", [])
        pt = r.get("priority_track", "?")
        if cuts:
            desc = _describe_cuts(cuts)
            max_db = min(c["cut_db"] for c in cuts)
            summary_lines.append(
                f"  Ducked {r['track']}'s {desc} to get out of {pt}'s way "
                f"(up to {max_db:.1f} dB)"
            )
    eq_count = sum(1 for r in all_results if r.get("cuts"))
    skip_count = sum(1 for r in all_results if not r.get("cuts"))
    summary_lines.append(f"  ---")
    summary_lines.append(f"  {eq_count} EQ'd, {skip_count} no masking, {len(all_errors)} errors")

    # Complementary boost summary (hybrid only)
    if strategy == "hybrid" and compl_results:
        summary_lines.append(f"  === Complementary boosts (hybrid) ===")
        for fam_name in sorted({r["family"] for r in compl_results}):
            fam_tracks = [r for r in compl_results if r["family"] == fam_name]
            roles_desc = ", ".join(
                f"{r['track']}={r['role']}" for r in fam_tracks
            )
            summary_lines.append(f"    {fam_name}: {roles_desc}")
            for r in fam_tracks:
                for m in r.get("moves", []):
                    lo = m.get("lo", 0)
                    hi = m.get("hi", 0)
                    db = m.get("gain_db", 0)
                    action = "boost" if db > 0 else "cut"
                    summary_lines.append(
                        f"      {action.title()} {r['track']} "
                        f"{lo}-{hi}Hz {db:+.1f} dB"
                    )
        boost_count = sum(1 for r in compl_results if r.get("moves"))
        summary_lines.append(
            f"    {boost_count} tracks boosted, {len(all_compl_errors)} compl errors"
        )

    send_command_fn("log", message="\n".join(summary_lines), timeout=5)

    audit_payload = {
        "mode": "auto_eq_all",
        "created_at_utc": _now_utc_iso(),
        "params": {
            "max_cut_db": max_cut_db,
            "aggressiveness": aggressiveness,
            "max_cuts": max_cuts,
            "makeup_mode": makeup_mode,
            "level_requested": (level or "auto").lower(),
            "level_resolved": resolved_level,
            "time_start": time_start,
            "time_end": time_end,
            "strategy": strategy,
            "family": family,
            "max_boost_db": max_boost_db,
            "role_overrides": role_overrides,
        },
        "targets": [t[0] for t in scored],
        "band_edges": BAND_EDGES,
        "band_labels": BAND_LABELS,
        "pairs": audit_pairs,
        "status": status,
        "errors": combined_errors,
    }
    if strategy == "hybrid":
        audit_payload["compl_families"] = compl_audit_families
        audit_payload["compl_track_results"] = compl_results
        if lane_result.get("contested_bands"):
            audit_payload["lane_assignment"] = {
                "lane_owners": {str(k): v for k, v in lane_result["lane_owners"].items()},
                "lane_constraints": lane_result["lane_constraints"],
                "contested_bands": lane_result["contested_bands"],
            }
    audit_path = _write_audit_artifact("auto_eq_all", audit_payload)

    return _finalize_with_preflight(
        {
            "status": status,
            "strategy": strategy,
            "level_requested": (level or "auto").lower(),
            "level_resolved": resolved_level,
            "audit_path": audit_path,
            "targets": [t[0] for t in scored],
            "results": all_results,
            "compl_results": compl_results,
            "errors": combined_errors,
        },
        preflight,
        send_command_fn,
        "auto_eq_all",
    )


def auto_eq_compl(
    family: str = "all",
    level: str = "leaf",
    max_cut_db: float = -3.0,
    max_boost_db: float = COMPL_MAX_BOOST_DB,
    max_moves: int = COMPL_MAX_MOVES,
    role_overrides: dict[str, str] | None = None,
    role_hints: dict[str, str] | None = None,
    time_start: float | None = None,
    time_end: float | None = None,
    send_command_fn: Callable = _default_send_command,
) -> dict:
    """Complementary EQ mode for same-family layering (guitar / keys-synth)."""
    family_requested = (family or "all").lower()
    if family_requested not in SUPPORTED_COMPL_FAMILIES:
        family_requested = "all"

    max_cut_db = -abs(max_cut_db) if max_cut_db != 0 else -3.0
    max_boost_db = min(COMPL_MAX_BOOST_DB, abs(max_boost_db))
    max_moves = max(1, min(int(max_moves), 6))
    normalized_overrides = {
        str(k): str(v)
        for k, v in (role_overrides or {}).items()
        if _normalize_role_name(str(v)) is not None
    }
    normalized_hints = {
        str(k): str(v)
        for k, v in (role_hints or {}).items()
        if _normalize_role_name(str(v)) is not None
    }

    ctx = send_command_fn("get_context", timeout=10)
    if ctx.get("status") != "ok":
        return {"status": "error", "errors": ["Failed to get context"]}

    tracks = ctx.get("tracks", [])
    tracks = [
        t for t in tracks
        if not t["name"].startswith("__tmp_")
        and not t.get("muted")
        and not is_helper_track(t["name"])
    ]
    selected_tracks, resolved_level = _select_auto_eq_all_targets(tracks, level)
    if len(selected_tracks) < 2:
        return {"status": "error", "errors": ["Need at least 2 tracks"]}

    grouped: dict[str, list[dict]] = {"guitar": [], "keys-synth": [], "vocal": []}
    for track in selected_tracks:
        fam = _classify_compl_family(track["name"])
        if fam is not None:
            grouped.setdefault(fam, []).append(track)

    if family_requested != "all":
        grouped = {family_requested: grouped.get(family_requested, [])}

    grouped = {fam: items for fam, items in grouped.items() if len(items) >= 2}
    if not grouped:
        return {
            "status": "error",
            "errors": ["No supported family groups with at least 2 active tracks"],
        }

    preflight = _prepare_autoeq_preflight(send_command_fn)

    try:
        return _auto_eq_compl_inner(
            tracks, selected_tracks, resolved_level, grouped, preflight,
            family_requested=family_requested, normalized_overrides=normalized_overrides,
            normalized_hints=normalized_hints,
            max_cut_db=max_cut_db, max_boost_db=max_boost_db, max_moves=max_moves,
            level=level, time_start=time_start, time_end=time_end,
            send_command_fn=send_command_fn,
        )
    except Exception as exc:
        return _finalize_with_preflight(
            {"status": "error", "errors": [f"Auto-EQ-compl failed: {exc}"]},
            preflight, send_command_fn, "auto_eq_compl",
        )


def _auto_eq_compl_inner(
    tracks, selected_tracks, resolved_level, grouped, preflight,
    *, family_requested, normalized_overrides, normalized_hints=None,
    max_cut_db, max_boost_db, max_moves,
    level, time_start, time_end, send_command_fn,
):
    """Inner body of auto_eq_compl — extracted for exception-safe preflight restore."""
    calibration = ensure_calibration(send_command_fn, require_boost_range=True)

    # Analysis cache: keyed by track INDEX to avoid duplicate-name collisions.
    analysis_cache: dict[int, dict] = {}
    analysis_targets = {
        int(t["index"])
        for fam_tracks in grouped.values()
        for t in fam_tracks
        if isinstance(t.get("index"), (int, float))
    }
    analysis_total = len(analysis_targets)
    analysis_progress = _make_analysis_progress_logger(
        send_command_fn,
        "Auto-EQ Compl analysis",
        analysis_total,
    )
    _safe_log(send_command_fn, f"Auto-EQ Compl analysis: analyzed 0/{analysis_total} tracks")

    # Build an index->track lookup for folder-aware analysis.
    _track_by_idx_compl: dict[int, dict] = {
        int(t["index"]): t for t in tracks
        if isinstance(t.get("index"), (int, float))
    }

    def cached_analyze(track_name: str, track_idx: int) -> dict:
        """Folder-aware cached analysis for compl mode, keyed by index."""
        if track_idx in analysis_cache:
            return analysis_cache[track_idx]
        analysis_progress(track_name, track_idx)
        track_meta = _track_by_idx_compl.get(track_idx)
        if track_meta and _is_folder_track(track_meta):
            result = _analyze_folder_aggregate(
                track_meta, tracks, time_start, time_end,
                send_command_fn, analysis_cache,
            )
        else:
            result = analyze_track(
                track_name, time_start, time_end, send_command_fn,
                track_index=track_idx,
            )
        analysis_cache[track_idx] = result
        return result

    all_results: list[dict] = []
    all_errors: list[str] = []
    audit_families: list[dict] = []

    # Pre-pass: analyze ALL family tracks for inter-family lane assignment
    compl_family_analysis: dict[str, list[dict]] = {}
    for fam_name_pre, fam_tracks_pre in grouped.items():
        rows_pre: list[dict] = []
        for ft in fam_tracks_pre:
            analyzed = cached_analyze(ft["name"], int(ft["index"]))
            if analyzed.get("status") == "ok":
                rows_pre.append(analyzed)
        if rows_pre:
            compl_family_analysis[fam_name_pre] = rows_pre

    compl_lane_result: dict = {"lane_owners": {}, "lane_constraints": {}, "contested_bands": []}
    if len(compl_family_analysis) >= 2:
        compl_spectra = _compute_family_aggregate_spectrum(compl_family_analysis)
        compl_lane_result = _assign_spectral_lanes(compl_spectra, compl_family_analysis)

    for fam_name, fam_tracks in grouped.items():
        fam_track_names = [t["name"] for t in fam_tracks]
        fam_rows = []
        fam_errors = []
        for ft in fam_tracks:
            track_name = ft["name"]
            track_idx = int(ft["index"])
            analyzed = cached_analyze(track_name, track_idx)
            if analyzed.get("status") != "ok":
                msg = f"Analysis failed for '{track_name}': {analyzed.get('errors', ['Unknown'])}"
                fam_errors.append(msg)
                all_errors.append(msg)
                continue
            fam_rows.append(analyzed)

        if len(fam_rows) < 2:
            all_errors.append(
                f"Family '{fam_name}' skipped: need at least 2 analyzable tracks"
            )
            audit_families.append({
                "family": fam_name,
                "tracks": fam_track_names,
                "errors": fam_errors + ["Need at least 2 analyzable tracks"],
            })
            continue

        roles_by_index = _assign_complementary_roles(
            fam_name, fam_rows, normalized_overrides, normalized_hints
        )
        role_assignments = [
            {
                "index": int(r["index"]),
                "track": r["track"],
                "role": roles_by_index.get(int(r["index"]), "support"),
            }
            for r in fam_rows
            if isinstance(r.get("index"), (int, float))
        ]
        # Build means on successful analyses only.
        family_means_db = _family_band_means(fam_rows)
        family_audit = {
            "family": fam_name,
            "tracks": [r["track"] for r in fam_rows],
            "roles": role_assignments,
            "family_means_db": [round(v, 3) for v in family_means_db],
            "track_results": [],
            "errors": fam_errors,
        }

        for row in fam_rows:
            track_name = row["track"]
            role = roles_by_index.get(int(row["index"]), "support")
            target_index = row.get("index")

            # Read existing FX chain to detect masking constraints from [AutoEQ].
            masking_constraints: dict[int, float] = {}
            preflight_fx = send_command_fn(
                "get_track_fx", track=int(target_index), strict=True, timeout=10
            )
            if preflight_fx.get("status") == "ok":
                preflight_chain = preflight_fx.get("fx_chain", [])
                masking_constraints = read_masking_constraints(
                    preflight_chain, calibration
                )

            details = _compute_complementary_moves_with_details(
                family=fam_name,
                role=role,
                track_bands=row["bands"],
                family_means_db=family_means_db,
                max_cut_db=max_cut_db,
                max_boost_db=max_boost_db,
                max_moves=max_moves,
                masking_constraints=masking_constraints,
                band_to_reaeq=_band_to_reaeq_map(calibration),
                lane_constraints=compl_lane_result["lane_constraints"].get(fam_name, {}),
            )
            moves = details["moves"]
            band_decisions = details["band_decisions"]
            merge_conflicts = _find_reaeq_merge_conflicts(moves, calibration)

            if not moves:
                result_row = {
                    "track": track_name,
                    "family": fam_name,
                    "role": role,
                    "index": target_index,
                    "moves": [],
                    "message": "No complementary shaping needed",
                }
                all_results.append(result_row)
                family_audit["track_results"].append({
                    "track": track_name,
                    "index": target_index,
                    "role": role,
                    "masking_constraints": masking_constraints if masking_constraints else None,
                    "band_decisions": band_decisions,
                    "selected_moves": [],
                    "message": "No complementary shaping needed",
                })
                continue

            eq_params = build_eq_params_for_moves(moves, calibration)

            # Reuse preflight FX chain read (already done for masking constraints).
            if preflight_fx.get("status") == "ok":
                existing = find_tagged_fx(preflight_chain, AUTO_EQ_COMPL_TAG)
                if existing:
                    remove_result = send_command_fn(
                        "remove_fx", track=target_index, fx_index=existing[0], timeout=10
                    )
                    if remove_result.get("status") != "ok":
                        all_errors.append(
                            f"Remove existing AutoEQ-Comp on '{track_name}': "
                            f"{'; '.join(str(e) for e in remove_result.get('errors', ['Unknown']))}"
                        )
            else:
                all_errors.append(
                    f"Pre-flight FX read failed for '{track_name}': "
                    f"{'; '.join(str(e) for e in preflight_fx.get('errors', ['Unknown']))}"
                )

            vis_params, eq_body_params = _split_visible_bands_param(eq_params)
            post_params = [*vis_params, *eq_body_params]
            plan_steps = [{"action": "add_fx", "fx_name": "ReaEQ"}]
            plan_steps.extend(_build_reaeq_layout_steps(calibration, fx_index=0))
            plan = {
                "title": f"AutoEQ-Comp: {track_name} ({fam_name}/{role})",
                "steps": plan_steps,
            }
            apply_result = send_command_fn(
                "apply_plan", track=target_index, plan=plan, timeout=30
            )
            apply_errors = [str(e) for e in apply_result.get("errors", [])]
            if apply_errors:
                all_errors.append(f"Apply on '{track_name}': {'; '.join(apply_errors)}")
            track_apply_status = apply_result.get("status", "unknown")

            if apply_result.get("status") in ("ok", "partial"):
                added = apply_result.get("added_fx_indices", [])
                if added:
                    new_fx_idx = added[0]
                    if post_params:
                        param_result = send_command_fn(
                            "set_param",
                            track=target_index,
                            fx_index=new_fx_idx,
                            params=post_params,
                            strict=True,
                            timeout=10,
                        )
                        param_status = param_result.get("status")
                        param_errors = [str(e) for e in param_result.get("errors", [])]
                        if param_status not in ("ok", "partial"):
                            if track_apply_status in ("ok", "partial"):
                                track_apply_status = "partial"
                            err_text = "; ".join(param_errors) if param_errors else "Unknown error"
                            all_errors.append(f"Set params on '{track_name}': {err_text}")
                            if param_errors:
                                apply_errors.extend(param_errors)
                        elif param_status == "partial":
                            if track_apply_status == "ok":
                                track_apply_status = "partial"
                            if param_errors:
                                all_errors.append(
                                    f"Set params partial on '{track_name}': "
                                    f"{'; '.join(param_errors)}"
                                )
                                apply_errors.extend(param_errors)
                    rename_result = send_command_fn(
                        "rename_fx",
                        track=target_index,
                        fx_index=new_fx_idx,
                        name=f"ReaEQ {AUTO_EQ_COMPL_TAG}",
                        timeout=10,
                    )
                    if rename_result.get("status") != "ok":
                        all_errors.append(
                            f"Rename AutoEQ-Comp on '{track_name}': "
                            f"{'; '.join(str(e) for e in rename_result.get('errors', ['Unknown']))}"
                        )

            result_row = {
                "track": track_name,
                "family": fam_name,
                "role": role,
                "index": target_index,
                "moves": moves,
                "merge_conflicts": merge_conflicts,
                "eq_params": eq_params,
                "apply_status": track_apply_status,
                "apply_errors": apply_errors,
            }
            all_results.append(result_row)
            family_audit["track_results"].append({
                "track": track_name,
                "index": target_index,
                "role": role,
                "masking_constraints": masking_constraints if masking_constraints else None,
                "band_decisions": band_decisions,
                "selected_moves": moves,
                "merge_conflicts": merge_conflicts,
                "eq_params": eq_params,
                "apply_status": track_apply_status,
                "apply_errors": apply_errors,
            })

        audit_families.append(family_audit)

    status = "error" if (not all_results and all_errors) else "ok"
    if all_results and all_errors:
        status = "partial"

    summary_lines = [
        f"=== Auto-EQ Complementary complete (family: {family_requested}, level: {resolved_level}) ==="
    ]
    for row in all_results:
        moves = row.get("moves", [])
        track_name = row.get("track", "?")
        role = row.get("role", "support")
        if moves:
            max_mag = max(abs(m.get("gain_db", 0)) for m in moves)
            summary_lines.append(
                f"  Shaped {track_name} ({row.get('family')}/{role}): "
                f"{_describe_moves(moves)} (max {max_mag:.1f} dB)"
            )
        else:
            summary_lines.append(
                f"  {track_name} ({row.get('family')}/{role}): no complementary shaping needed"
            )
    send_command_fn("log", message="\n".join(summary_lines), timeout=5)

    audit_payload = {
        "mode": "auto_eq_compl",
        "created_at_utc": _now_utc_iso(),
        "params": {
            "family_requested": family_requested,
            "level_requested": (level or "auto").lower(),
            "level_resolved": resolved_level,
            "max_cut_db": max_cut_db,
            "max_boost_db": max_boost_db,
            "max_moves": max_moves,
            "role_overrides": normalized_overrides,
            "time_start": time_start,
            "time_end": time_end,
        },
        "band_edges": BAND_EDGES,
        "band_labels": BAND_LABELS,
        "families": audit_families,
        "status": status,
        "errors": all_errors,
    }
    if compl_lane_result.get("contested_bands"):
        audit_payload["lane_assignment"] = {
            "lane_owners": {str(k): v for k, v in compl_lane_result["lane_owners"].items()},
            "lane_constraints": compl_lane_result["lane_constraints"],
            "contested_bands": compl_lane_result["contested_bands"],
        }
    audit_path = _write_audit_artifact("auto_eq_compl", audit_payload)

    return _finalize_with_preflight(
        {
            "status": status,
            "mode": "auto_eq_compl",
            "family_requested": family_requested,
            "level_requested": (level or "auto").lower(),
            "level_resolved": resolved_level,
            "audit_path": audit_path,
            "results": all_results,
            "errors": all_errors,
        },
        preflight,
        send_command_fn,
        "auto_eq_compl",
    )


# ---------------------------------------------------------------------------
# Section-based Auto-EQ (time-varying automation envelopes)
# ---------------------------------------------------------------------------

# Defaults for section mode
SECTIONS_MAX_CUTS = 5
SECTIONS_MAX_CUT_DB = -6.0
SECTIONS_AGGRESSIVENESS = 1.5
SECTIONS_MAX_SECTIONS = 20
SECTIONS_MAX_TOTAL_DURATION = 600  # seconds
SECTIONS_MAX_LABEL_LEN = 64


def _validate_sections(
    sections: list[tuple[str, float, float]],
) -> list[str]:
    """Validate section list. Returns list of error strings (empty = ok)."""
    errors: list[str] = []
    if not sections:
        errors.append("At least one section is required")
        return errors
    if len(sections) > SECTIONS_MAX_SECTIONS:
        errors.append(f"Too many sections ({len(sections)}), max {SECTIONS_MAX_SECTIONS}")
        return errors
    total_dur = 0.0
    prev_end = -1.0
    for i, (label, start, end) in enumerate(sections):
        if not isinstance(label, str) or not label.strip():
            errors.append(f"Section {i}: missing label")
        elif len(label) > SECTIONS_MAX_LABEL_LEN:
            errors.append(f"Section {i}: label too long ({len(label)} chars)")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            errors.append(f"Section {i} ({label}): start/end must be numbers")
            continue
        if start < 0:
            errors.append(f"Section {i} ({label}): start must be >= 0")
        if end <= start:
            errors.append(f"Section {i} ({label}): end must be > start")
        if start < prev_end:
            errors.append(
                f"Section {i} ({label}): overlaps previous section "
                f"(starts at {start}, previous ends at {prev_end})"
            )
        prev_end = end
        total_dur += max(0, end - start)
    if total_dur > SECTIONS_MAX_TOTAL_DURATION:
        errors.append(
            f"Total analyzed duration {total_dur:.0f}s exceeds "
            f"max {SECTIONS_MAX_TOTAL_DURATION}s"
        )
    return errors


def auto_eq_sections(
    sections: list[tuple[str, float, float]],
    max_cut_db: float = SECTIONS_MAX_CUT_DB,
    aggressiveness: float = SECTIONS_AGGRESSIVENESS,
    max_cuts: int = SECTIONS_MAX_CUTS,
    level: str = "leaf",
    analyze_range: tuple[float, float] | None = None,
    makeup_mode: str = "auto",
    strategy: str = "hybrid",
    family: str = "all",
    max_boost_db: float = 6.0,
    role_overrides: dict[str, str] | None = None,
    write_mode: str = "auto",
    hybrid_selective_makeup: bool = False,
    send_command_fn: Callable = _default_send_command,
) -> dict:
    """Run per-section auto-EQ with automation envelopes.

    Each section is analyzed independently. One ReaEQ [AutoEQ] is placed on
    each yield track with static band layout, then Gain automation envelopes
    are written so the EQ changes at section boundaries.

    Args:
        sections: List of (label, start_seconds, end_seconds).
        max_cut_db: Maximum cut depth in dB (negative).
        aggressiveness: Masking aggressiveness factor.
        max_cuts: Maximum number of cuts per section per track.
        level: Target selection level ("leaf", "bus", "auto").
        analyze_range: Optional (start, end) seconds to override the analysis
            window.  Envelope points still use the section start/end times.
        makeup_mode: "auto" to apply selective makeup on uncut bands, "off" for none.
        strategy: "subtractive" (cuts only) or "hybrid" (cuts + family boosts).
        family: Restrict hybrid boosts to this family ("all", "guitar", "keys-synth").
        max_boost_db: Maximum boost magnitude for hybrid mode.
        role_overrides: Optional dict of track_name -> role for hybrid mode.
        write_mode: "replace" (full rebuild), "merge" (incremental), "auto" (detect).
        hybrid_selective_makeup: Allow selective makeup in hybrid strategy.
        send_command_fn: IPC command function.
    """
    # Validate sections
    validation_errors = _validate_sections(sections)
    if validation_errors:
        return {"status": "error", "errors": validation_errors}

    # Validate analyze_range if provided
    if analyze_range is not None:
        ar_start, ar_end = analyze_range
        ar_errors = []
        if ar_start < 0:
            ar_errors.append("analyze_range start must be >= 0")
        if ar_end <= ar_start:
            ar_errors.append("analyze_range end must be > start")
        if (ar_end - ar_start) > SECTIONS_MAX_TOTAL_DURATION:
            ar_errors.append(
                f"analyze_range duration {ar_end - ar_start:.0f}s exceeds "
                f"max {SECTIONS_MAX_TOTAL_DURATION}s"
            )
        if ar_errors:
            return {"status": "error", "errors": ar_errors}

    # Validate write_mode
    if write_mode not in ("replace", "merge", "auto"):
        return {"status": "error", "errors": [f"Invalid write_mode: {write_mode!r}"]}

    # Get context
    ctx = send_command_fn("get_context", timeout=10)
    if ctx.get("status") != "ok":
        return {"status": "error", "errors": ["Failed to get context"]}

    tracks = ctx.get("tracks", [])
    tracks = [
        t for t in tracks
        if not t["name"].startswith("__tmp_")
        and not t.get("muted")
        and not is_helper_track(t["name"])
    ]
    selected_tracks, resolved_level = _select_auto_eq_all_targets(tracks, level)
    if len(selected_tracks) < 2:
        return {"status": "error", "errors": ["Need at least 2 tracks"]}

    # Verify daemon support before any preflight mutation.
    probe = send_command_fn(
        "set_fx_envelopes", track=0, fx_index=0, envelopes=[], strict=True, timeout=10
    )
    probe_errs = "; ".join(str(e) for e in probe.get("errors", []))
    if "Unknown operation" in probe_errs:
        return {
            "status": "error",
            "errors": [
                "Daemon needs updating — copy reaper_daemon.lua "
                "and restart the script in REAPER."
            ],
        }

    preflight = _prepare_autoeq_preflight(send_command_fn)

    try:
        return _auto_eq_sections_inner(
            tracks, selected_tracks, resolved_level, preflight,
            sections=sections,
            max_cut_db=max_cut_db, aggressiveness=aggressiveness,
            max_cuts=max_cuts, level=level,
            analyze_range=analyze_range,
            makeup_mode=makeup_mode,
            strategy=strategy, family=family,
            max_boost_db=max_boost_db, role_overrides=role_overrides,
            write_mode=write_mode,
            hybrid_selective_makeup=hybrid_selective_makeup,
            send_command_fn=send_command_fn,
        )
    except Exception as exc:
        return _finalize_with_preflight(
            {"status": "error", "errors": [f"Auto-EQ-sections failed: {exc}"]},
            preflight, send_command_fn, "auto_eq_sections",
        )


def _auto_eq_sections_inner(
    tracks, selected_tracks, resolved_level, preflight,
    *, sections, max_cut_db, aggressiveness, max_cuts, level,
    analyze_range, makeup_mode, strategy, family, max_boost_db,
    role_overrides, write_mode, hybrid_selective_makeup, send_command_fn,
):
    """Inner body of auto_eq_sections — exception-safe preflight restore."""

    if strategy == "hybrid":
        calibration = ensure_calibration(send_command_fn, require_boost_range=True)
    else:
        calibration = ensure_calibration(send_command_fn)
    gain_cal = calibration["gain"]
    freq_cal = calibration.get("freq", [])
    band_type_norm = calibration.get("band_type_norm")
    band_names = _reaeq_band_names(calibration)
    flat_gain_norm = db_to_normalized(0.0, gain_cal)

    # Clamp boost cap
    max_boost_db = min(max_boost_db, COMPL_MAX_BOOST_DB)

    # Score and sort by priority
    scored = [
        (t["name"], t["index"], match_track_priority(t["name"]))
        for t in selected_tracks
    ]
    scored.sort(key=lambda x: x[2], reverse=True)

    _track_by_idx: dict[int, dict] = {
        int(t["index"]): t for t in tracks
        if isinstance(t.get("index"), (int, float))
    }

    all_errors: list[str] = []

    # -----------------------------------------------------------------------
    # Phase 1: Setup — remove old [AutoEQ], add fresh ReaEQ, static layout
    # -----------------------------------------------------------------------
    # Identify yield tracks (all that have at least one higher-priority track)
    yield_tracks: list[tuple[str, int, float, list[tuple[str, int]]]] = []
    for i, (name, _idx, priority) in enumerate(scored):
        higher = [(s[0], s[1]) for s in scored[:i] if s[2] > priority]
        if not higher:
            continue
        yield_tracks.append((name, int(_idx), priority, higher))

    if not yield_tracks:
        return _finalize_with_preflight(
            {"status": "error", "errors": ["No yield tracks found (need priority hierarchy)"]},
            preflight, send_command_fn, "auto_eq_sections",
        )

    # Pre-cleanup: detect existing [AutoEQ] per yield track for mode resolution
    existing_autoeq: dict[int, tuple[int, str]] = {}  # track_idx -> (fx_index, fx_name)
    existing_has_envelopes = False  # True if any existing [AutoEQ] has envelope points
    for _name, idx, _pri, _higher in yield_tracks:
        fx_result = send_command_fn(
            "get_track_fx", track=idx, strict=True, timeout=10,
        )
        if fx_result.get("status") == "ok":
            found = find_auto_eq_fx(fx_result.get("fx_chain", []))
            if found:
                existing_autoeq[idx] = found
                # Check if the FX has envelope points (sections-based vs static)
                env_check = send_command_fn(
                    "has_fx_envelopes", track=idx, fx_index=found[0],
                    strict=True, timeout=10,
                )
                if env_check.get("has_envelopes"):
                    existing_has_envelopes = True

    # Resolve write mode
    # auto: merge only if existing [AutoEQ] has envelopes (from prior sections run).
    #        Static [AutoEQ] (from auto-eq-all/pair) triggers replace (transition).
    effective_mode = write_mode
    if write_mode == "auto":
        if existing_autoeq and existing_has_envelopes:
            effective_mode = "merge"
        else:
            effective_mode = "replace"

    section_write_start = min(s[1] for s in sections)
    section_write_end = max(s[2] for s in sections)

    # Detect transition from static auto-eq-all to sections
    is_transition = (
        effective_mode == "replace"
        and existing_autoeq
        and not existing_has_envelopes
    )

    log_prefix = f"[auto_eq_sections] write_mode={write_mode} -> {effective_mode}"
    if effective_mode == "merge":
        log_prefix += f"  range={section_write_start:.3f}-{section_write_end:.3f}s"
    if is_transition:
        log_prefix += "  (transition from static auto-eq-all)"
    send_command_fn("log", message=log_prefix, timeout=5)

    # Stale cleanup — mode-dependent.
    # replace: remove old [AutoEQ] + [AutoEQ-Gain] from ALL yield tracks.
    #          On transition from static, also remove [AutoEQ-Comp] to prevent stacking.
    # merge:   keep [AutoEQ], only remove [AutoEQ-Gain] (gain makeup rebuilt).
    created_this_run: set[int] = set()
    for _name, idx, _pri, _higher in yield_tracks:
        fx_result = send_command_fn(
            "get_track_fx", track=idx, strict=True, timeout=10,
        )
        if fx_result.get("status") == "ok":
            chain = fx_result.get("fx_chain", [])
            if effective_mode == "replace":
                existing = find_auto_eq_fx(chain)
                if existing:
                    send_command_fn("remove_fx", track=idx, fx_index=existing[0], timeout=10)
                    # Re-fetch chain since indices shifted
                    fx_result = send_command_fn(
                        "get_track_fx", track=idx, strict=True, timeout=10,
                    )
                    chain = fx_result.get("fx_chain", []) if fx_result.get("status") == "ok" else []
                # Transition: also remove [AutoEQ-Comp] (static comp stacks
                # against section automation and is no longer meaningful).
                if is_transition:
                    existing_comp = find_tagged_fx(chain, AUTO_EQ_COMPL_TAG)
                    if existing_comp:
                        send_command_fn("remove_fx", track=idx, fx_index=existing_comp[0], timeout=10)
                        fx_result = send_command_fn(
                            "get_track_fx", track=idx, strict=True, timeout=10,
                        )
                        chain = fx_result.get("fx_chain", []) if fx_result.get("status") == "ok" else []
            existing_gain = find_tagged_fx(chain, AUTOEQ_GAIN_TAG)
            if existing_gain:
                fx_result2 = send_command_fn(
                    "get_track_fx", track=idx, strict=True, timeout=10,
                )
                if fx_result2.get("status") == "ok":
                    eg2 = find_tagged_fx(fx_result2.get("fx_chain", []), AUTOEQ_GAIN_TAG)
                    if eg2:
                        send_command_fn("remove_fx", track=idx, fx_index=eg2[0], timeout=10)

    # After cleanup, refresh existing_autoeq for merge (indices may have shifted)
    if effective_mode == "merge":
        existing_autoeq.clear()
        for _name, idx, _pri, _higher in yield_tracks:
            fx_result = send_command_fn(
                "get_track_fx", track=idx, strict=True, timeout=10,
            )
            if fx_result.get("status") == "ok":
                found = find_auto_eq_fx(fx_result.get("fx_chain", []))
                if found:
                    existing_autoeq[idx] = found

    # Volume floor check — skip tracks that are effectively silent
    # across all sections (e.g. volume automation at -132 dB).
    silent_tracks: set[int] = set()
    for name, idx, _pri, _higher in yield_tracks:
        is_loud_in_any = False
        for _label, sec_start, sec_end in sections:
            vol_db = _track_volume_db_at_range(
                idx, sec_start, sec_end, send_command_fn,
            )
            if vol_db is None or vol_db > VOLUME_FLOOR_DB:
                is_loud_in_any = True
                break
        if not is_loud_in_any:
            silent_tracks.add(idx)
            _safe_log(
                send_command_fn,
                f"Auto-EQ Sections: skipping '{name}' — volume below "
                f"{VOLUME_FLOOR_DB} dB in all sections",
            )
    if silent_tracks:
        yield_tracks = [
            yt for yt in yield_tracks if yt[1] not in silent_tracks
        ]
    if not yield_tracks:
        return _finalize_with_preflight(
            {"status": "error", "errors": ["All yield tracks are silent"] + all_errors},
            preflight, send_command_fn, "auto_eq_sections",
        )

    analysis_targets = set()
    for _name, idx, _pri, higher in yield_tracks:
        analysis_targets.add(int(idx))
        for _ref_name, ref_idx in higher:
            analysis_targets.add(int(ref_idx))
    analysis_total = len(analysis_targets)

    # Per-yield-track state: fx_index, param_index map for Gain bands
    track_fx_info: dict[int, dict] = {}  # idx -> {fx_index, gain_param_indices}

    for name, idx, _pri, _higher in yield_tracks:
        # Merge path: reuse existing [AutoEQ] if present
        if effective_mode == "merge" and idx in existing_autoeq:
            reuse_fx_idx = existing_autoeq[idx][0]
            # Resolve Gain param indices from existing FX
            fx_info = send_command_fn(
                "get_track_fx", track=idx, strict=True, timeout=10
            )
            gain_params: dict[int, int] = {}
            if fx_info.get("status") == "ok":
                for fx in fx_info.get("fx_chain", []):
                    if fx.get("index") != reuse_fx_idx:
                        continue
                    params_list = fx.get("params", [])
                    for reaeq_idx in range(len(band_names)):
                        target_name = f"Gain-{band_names[reaeq_idx]}"
                        target_key = _normalize_param_key(target_name)
                        for p in params_list:
                            if _normalize_param_key(p.get("name", "")) == target_key:
                                gain_params[reaeq_idx] = p["index"]
                                break
            if not gain_params:
                all_errors.append(
                    f"Existing [AutoEQ] on '{name}' has incompatible layout "
                    f"(0 gain params resolved) — skipping track"
                )
                continue
            track_fx_info[idx] = {
                "fx_index": reuse_fx_idx,
                "gain_params": gain_params,
                "name": name,
            }
            continue

        # Fresh setup path (replace mode, or merge mode with no existing FX)
        # Add fresh ReaEQ via apply_plan (static band setup)
        # Build static params: Freq, Type per band (no Gain yet — that's automated)
        plan_steps: list[dict] = [{"action": "add_fx", "fx_name": "ReaEQ"}]
        plan_steps.extend(_build_reaeq_layout_steps(calibration, fx_index=0))
        post_layout_params: list[dict] = []

        # Set visible bands if calibration provides it
        visible_bands_norm = calibration.get("visible_bands_norm")
        if isinstance(visible_bands_norm, (int, float)):
            post_layout_params.append({"name": "Visible bands", "value": float(visible_bands_norm)})

        # Set static Freq and Type for each band (Gain set separately after
        # apply_plan to avoid preset deferred-update race)
        static_params: list[dict] = []
        gain_params_for_later: list[dict] = []
        num_bands = min(len(band_names), len(BAND_EDGES) - 1)
        band_map = _band_to_reaeq_map(calibration)
        for analysis_band_idx in range(len(BAND_EDGES) - 1):
            reaeq_idx = band_map.get(analysis_band_idx)
            if reaeq_idx is None or reaeq_idx >= len(band_names):
                continue
            bname = band_names[reaeq_idx]
            lo = BAND_EDGES[analysis_band_idx]
            hi = BAND_EDGES[analysis_band_idx + 1]
            target_hz = math.sqrt(lo * hi)
            freq_norm = hz_to_normalized(target_hz, freq_cal)
            if band_type_norm is not None:
                static_params.append({"name": f"Type-{bname}", "value": band_type_norm})
            static_params.append({"name": f"Freq-{bname}", "value": freq_norm})
            # Collect gain params for separate call after preset settles
            gain_params_for_later.append({"name": f"Gain-{bname}", "value": flat_gain_norm})

        if static_params:
            post_layout_params.extend(static_params)

        plan = {
            "title": f"AutoEQ Sections: {name}",
            "steps": plan_steps,
        }
        apply_result = send_command_fn(
            "apply_plan", track=idx, plan=plan, timeout=30
        )
        if apply_result.get("status") not in ("ok", "partial"):
            all_errors.append(
                f"Setup failed for '{name}': "
                f"{'; '.join(str(e) for e in apply_result.get('errors', []))}"
            )
            continue
        if apply_result.get("status") == "partial":
            setup_errs = apply_result.get("errors", [])
            if setup_errs:
                all_errors.append(
                    f"Setup partial for '{name}': "
                    f"{'; '.join(str(e) for e in setup_errs)}"
                )

        # Tag the new FX
        added = apply_result.get("added_fx_indices", [])
        if not added:
            all_errors.append(f"No FX added for '{name}'")
            continue
        new_fx_idx = added[0]
        send_command_fn(
            "rename_fx", track=idx, fx_index=new_fx_idx,
            name=f"ReaEQ {AUTO_EQ_TAG}", timeout=10,
        )

        # Apply static params in a separate call after apply_plan.
        if post_layout_params:
            layout_result = send_command_fn(
                "set_param",
                track=idx,
                fx_index=new_fx_idx,
                params=post_layout_params,
                strict=True,
                timeout=10,
            )
            if layout_result.get("status") not in ("ok", "partial"):
                all_errors.append(
                    f"Static layout params failed for '{name}': "
                    f"{'; '.join(str(e) for e in layout_result.get('errors', []))}"
                )
            elif layout_result.get("status") == "partial":
                all_errors.append(
                    f"Static layout params partial for '{name}': "
                    f"{'; '.join(str(e) for e in layout_result.get('errors', []))}"
                )

        # Set Gain params in a separate call AFTER the preset has settled.
        # Uses strict index targeting to avoid name-collision risk, and
        # checks the result so a silent failure doesn't leave gains at
        # the preset's +6 dB default.
        if gain_params_for_later:
            gain_result = send_command_fn(
                "set_param", track=idx, fx_index=new_fx_idx,
                params=gain_params_for_later, strict=True, timeout=10,
            )
            if gain_result.get("status") not in ("ok", "partial"):
                all_errors.append(
                    f"Gain reset failed for '{name}': "
                    f"{'; '.join(str(e) for e in gain_result.get('errors', []))}"
                )
            elif gain_result.get("status") == "partial":
                all_errors.append(
                    f"Gain reset partial for '{name}': "
                    f"{'; '.join(str(e) for e in gain_result.get('errors', []))}"
                )

        # Resolve Gain param indices from the newly-added FX
        fx_info = send_command_fn(
            "get_track_fx", track=idx, strict=True, timeout=10
        )
        gain_params: dict[int, int] = {}  # reaeq_band_idx -> param_index
        if fx_info.get("status") == "ok":
            for fx in fx_info.get("fx_chain", []):
                if fx.get("index") != new_fx_idx:
                    continue
                params_list = fx.get("params", [])
                for reaeq_idx in range(len(band_names)):
                    target_name = f"Gain-{band_names[reaeq_idx]}"
                    target_key = _normalize_param_key(target_name)
                    for p in params_list:
                        if _normalize_param_key(p.get("name", "")) == target_key:
                            gain_params[reaeq_idx] = p["index"]
                            break
        if not gain_params:
            all_errors.append(
                f"No Gain param indices resolved for '{name}' — "
                f"FX may be misconfigured; skipping track"
            )
            cleanup = send_command_fn(
                "remove_fx", track=idx, fx_index=new_fx_idx, strict=True, timeout=10
            )
            if cleanup.get("status") != "ok":
                all_errors.append(
                    f"Cleanup failed for '{name}': "
                    f"{'; '.join(str(e) for e in cleanup.get('errors', ['Unknown error']))}"
                )
            continue

        created_this_run.add(idx)
        track_fx_info[idx] = {
            "fx_index": new_fx_idx,
            "gain_params": gain_params,
            "name": name,
        }

    if not track_fx_info:
        return _finalize_with_preflight(
            {"status": "error", "errors": ["No tracks set up successfully"] + all_errors},
            preflight, send_command_fn, "auto_eq_sections",
        )

    # -----------------------------------------------------------------------
    # Phase 1b: Family grouping for hybrid strategy
    # -----------------------------------------------------------------------
    # family_groups[family_name] = [track_idx, ...] — only families with 2+ members
    family_groups: dict[str, list[int]] = {}
    if strategy == "hybrid":
        if makeup_mode == "auto" and not hybrid_selective_makeup:
            _safe_log(
                send_command_fn,
                "Auto-EQ Sections: selective makeup disabled in hybrid mode "
                "(complementary boosts only).",
            )
        elif makeup_mode == "auto" and hybrid_selective_makeup:
            _safe_log(
                send_command_fn,
                "Auto-EQ Sections: hybrid selective makeup enabled.",
            )
        raw_groups: dict[str, list[int]] = {}
        for idx, info in track_fx_info.items():
            fam = _classify_compl_family(info["name"])
            if fam is None:
                continue
            if family != "all" and fam != family:
                continue
            raw_groups.setdefault(fam, []).append(idx)
        # Keep only families with 2+ members
        family_groups = {k: v for k, v in raw_groups.items() if len(v) >= 2}

    # -----------------------------------------------------------------------
    # Phase 2: Per-section analysis and masking computation
    # -----------------------------------------------------------------------
    # For each section, analyze all tracks and compute masking.
    # Collect per-track per-band gain points across sections.
    #
    # envelope_points[track_idx][reaeq_band_idx] = list of {time, value, shape}
    envelope_points: dict[int, dict[int, list[dict]]] = {
        idx: {bi: [] for bi in info["gain_params"]}
        for idx, info in track_fx_info.items()
    }

    section_summaries: list[dict] = []
    # Per-track cut results for summary output: {track_idx: [{section, cuts}]}
    track_cut_results: dict[int, list[dict]] = {idx: [] for idx in track_fx_info}

    band_map = _band_to_reaeq_map(calibration)
    _safe_log(
        send_command_fn,
        f"Auto-EQ Sections: band_map indices = {band_map}"
        f" (calibrated={calibration is not None})",
    )
    total_sections = len(sections)
    for sec_num, (sec_label, sec_start, sec_end) in enumerate(sections, start=1):
        section_tag = f"{sec_num}/{total_sections} {sec_label}"
        section_progress = _make_analysis_progress_logger(
            send_command_fn,
            "Auto-EQ Sections analysis",
            analysis_total,
            section_label=section_tag,
        )
        _safe_log(
            send_command_fn,
            f"Auto-EQ Sections analysis [{section_tag}]: analyzed 0/{analysis_total} tracks",
        )

        # Analysis window: use analyze_range override if provided
        a_start = analyze_range[0] if analyze_range else sec_start
        a_end = analyze_range[1] if analyze_range else sec_end

        # Fresh analysis cache per section
        analysis_cache: dict[int, dict] = {}

        def cached_analyze_section(track_name: str, track_idx: int) -> dict:
            if track_idx in analysis_cache:
                return analysis_cache[track_idx]
            section_progress(track_name, track_idx)
            track_meta = _track_by_idx.get(track_idx)
            if track_meta and _is_folder_track(track_meta):
                result = _analyze_folder_aggregate(
                    track_meta, tracks, a_start, a_end,
                    send_command_fn, analysis_cache,
                )
            else:
                result = analyze_track(
                    track_name, a_start, a_end, send_command_fn,
                    track_index=track_idx,
                )
            analysis_cache[track_idx] = result
            return result

        sec_eq_count = 0

        # Per-section lane assignment for hybrid mode
        sec_lane_result: dict = {"lane_owners": {}, "lane_constraints": {}, "contested_bands": []}
        if strategy == "hybrid" and len(family_groups) >= 2:
            sec_family_analysis: dict[str, list[dict]] = {}
            for fam, members in family_groups.items():
                rows = []
                for fam_idx in members:
                    fam_name_s = track_fx_info.get(fam_idx, {}).get("name", "")
                    if not fam_name_s:
                        continue
                    result = cached_analyze_section(fam_name_s, fam_idx)
                    if result.get("status") == "ok":
                        rows.append(result)
                if rows:
                    sec_family_analysis[fam] = rows
            if len(sec_family_analysis) >= 2:
                sec_spectra = _compute_family_aggregate_spectrum(sec_family_analysis)
                sec_lane_result = _assign_spectral_lanes(sec_spectra, sec_family_analysis)

        for name, idx, _pri, higher in yield_tracks:
            if idx not in track_fx_info:
                continue
            info = track_fx_info[idx]

            # Analyze priority tracks
            ok_priority_results: list[dict] = []
            inactive_priority_names: list[str] = []
            for ref_name, ref_idx in higher:
                ref_result = cached_analyze_section(ref_name, int(ref_idx))
                if ref_result.get("status") == "ok":
                    if _is_active_priority_reference(ref_result):
                        ok_priority_results.append(ref_result)
                    else:
                        inactive_priority_names.append(ref_name)

            active_higher_names: list[str] = []
            for ref in ok_priority_results:
                ref_name = str(ref.get("track", "")).strip()
                if ref_name and ref_name not in active_higher_names:
                    active_higher_names.append(ref_name)
            if not active_higher_names and ok_priority_results:
                active_higher_names = [h[0] for h in higher]

            if not ok_priority_results:
                # No priority data — flat for this section
                for bi in info["gain_params"]:
                    envelope_points[idx][bi].append({
                        "time": sec_start, "value": flat_gain_norm, "shape": 1,
                    })
                continue
            if inactive_priority_names:
                _safe_log(
                    send_command_fn,
                    (
                        f"Auto-EQ Sections [{sec_label}]: ignored inactive refs for "
                        f"'{name}' (floor {PRIORITY_REFERENCE_ACTIVE_FLOOR_DB:.1f} dB): "
                        f"{', '.join(inactive_priority_names)}"
                    ),
                )

            # Aggregate priority bands
            if len(ok_priority_results) == 1:
                priority_bands = ok_priority_results[0]["bands"]
            else:
                priority_bands = _aggregate_bands(ok_priority_results)
                if priority_bands is None:
                    for bi in info["gain_params"]:
                        envelope_points[idx][bi].append({
                            "time": sec_start, "value": flat_gain_norm, "shape": 1,
                        })
                    continue

            # Analyze yield track
            yield_result = cached_analyze_section(name, idx)
            if yield_result.get("status") != "ok":
                for bi in info["gain_params"]:
                    envelope_points[idx][bi].append({
                        "time": sec_start, "value": flat_gain_norm, "shape": 1,
                    })
                continue

            yield_bands = yield_result["bands"]

            # Compute masking
            masking = _compute_masking_with_details(
                priority_bands=priority_bands,
                yield_bands=yield_bands,
                max_cut_db=max_cut_db,
                aggressiveness=aggressiveness,
                max_cuts=max_cuts,
            )
            cuts = _annotate_cut_contributors(
                masking["cuts"],
                priority_bands,
                ok_priority_results,
            )

            # Map cuts to ReaEQ bands
            reaeq_cuts = _merge_cuts_to_reaeq(cuts, calibration)

            # Hybrid strategy: compute complementary moves for family tracks
            reaeq_boosts: dict[int, dict] = {}
            no_boost_reasons: dict[str, int] = {}  # reason -> count
            # Gate: skip complementary boosts if yield track is near-silent
            yield_peak_db = max(
                (b.get("avg_db", -120.0) for b in yield_bands), default=-120.0
            )
            yield_is_silent = yield_peak_db < PRIORITY_ENERGY_FLOOR_DB

            if strategy == "hybrid" and yield_is_silent:
                no_boost_reasons["yield_silent_gate"] = 1
            if strategy == "hybrid" and not yield_is_silent:
                track_family = None
                for fam, members in family_groups.items():
                    if idx in members:
                        track_family = fam
                        break
                if track_family is None:
                    no_boost_reasons["no_family_group"] = 1
                if track_family is not None:
                    # Build analysis rows for the family in this section
                    fam_analysis_rows = []
                    for fam_idx in family_groups[track_family]:
                        fam_result = cached_analyze_section(
                            track_fx_info[fam_idx]["name"], fam_idx,
                        )
                        if fam_result.get("status") == "ok":
                            fam_analysis_rows.append({
                                "index": fam_idx,
                                "track": track_fx_info[fam_idx]["name"],
                                "bands": fam_result["bands"],
                            })
                    if len(fam_analysis_rows) >= 2:
                        roles = _assign_complementary_roles(
                            track_family, fam_analysis_rows,
                            role_overrides=role_overrides,
                        )
                        role = roles.get(idx, "support")
                        fam_means = _family_band_means(fam_analysis_rows)
                        # Masking constraints: block boosts on bands where
                        # this track already got a masking cut.
                        masking_constraints = {
                            bi: reaeq_cuts[bi]["cut_db"]
                            for bi in reaeq_cuts
                        }
                        # Weighted priority pressure: for each band, count
                        # what fraction of priority tracks are "active" there
                        # (within PRIORITY_LANE_GAP_DB of their own peak and
                        # above PRIORITY_LANE_FLOOR_DB absolute).  Use this
                        # to scale down — not block — complementary boosts.
                        priority_pressure: dict[int, float] = {}
                        n_refs = len(ok_priority_results)
                        if n_refs > 0:
                            # Track per-ref, per-reaeq-band: is this ref active
                            # in ANY analysis band that maps to this reaeq band?
                            # Use a set to avoid double-counting when multiple
                            # analysis bands map to the same ReaEQ band.
                            reaeq_active_refs: dict[int, set[int]] = {}
                            for ref_i, ref in enumerate(ok_priority_results):
                                ref_bands = ref.get("bands", [])
                                if not ref_bands:
                                    continue
                                ref_peak = max(
                                    max(
                                        rb.get("avg_db", -120.0),
                                        rb.get("avg_db_l", -120.0),
                                        rb.get("avg_db_r", -120.0),
                                    )
                                    for rb in ref_bands
                                )
                                activity_floor = max(
                                    PRIORITY_LANE_FLOOR_DB,
                                    ref_peak - PRIORITY_LANE_GAP_DB,
                                )
                                for b_idx, rb in enumerate(ref_bands):
                                    reaeq_bi = band_map.get(b_idx)
                                    if reaeq_bi is None:
                                        continue
                                    band_db = max(
                                        rb.get("avg_db", -120.0),
                                        rb.get("avg_db_l", -120.0),
                                        rb.get("avg_db_r", -120.0),
                                    )
                                    if band_db >= activity_floor:
                                        if reaeq_bi not in reaeq_active_refs:
                                            reaeq_active_refs[reaeq_bi] = set()
                                        reaeq_active_refs[reaeq_bi].add(ref_i)
                            for bi, ref_set in reaeq_active_refs.items():
                                # pressure = fraction of priority tracks active
                                # 0.0 = lane is open, 1.0 = fully occupied
                                priority_pressure[bi] = len(ref_set) / n_refs
                        compl = _compute_complementary_moves_with_details(
                            family=track_family,
                            role=role,
                            track_bands=yield_bands,
                            family_means_db=fam_means,
                            max_cut_db=max_cut_db,
                            max_boost_db=max_boost_db,
                            max_moves=COMPL_MAX_MOVES,
                            masking_constraints=masking_constraints,
                            band_to_reaeq=band_map,
                            lane_constraints=sec_lane_result["lane_constraints"].get(track_family, {}),
                        )
                        # Only take boosts from complementary (cuts come from masking).
                        # Scale each boost by (1 - pressure) so boosts are
                        # attenuated in bands crowded by priority tracks.
                        compl_boosts_raw = [
                            m for m in compl.get("moves", [])
                            if m.get("kind") == "boost"
                        ]
                        if not compl_boosts_raw:
                            no_boost_reasons["role_support_or_no_target"] = 1
                        compl_boosts = []
                        for m in compl_boosts_raw:
                            bi = m.get("band_index", 0)
                            reaeq_bi = band_map.get(bi, bi)
                            pressure = priority_pressure.get(reaeq_bi, 0.0)
                            openness = 1.0 - pressure
                            if openness < 0.1:
                                # Band is >90% occupied — skip boost entirely
                                no_boost_reasons["pressure_blocked"] = no_boost_reasons.get("pressure_blocked", 0) + 1
                                continue
                            scaled = dict(m)
                            scaled["gain_db"] = round(m["gain_db"] * openness, 2)
                            if abs(scaled["gain_db"]) >= COMPL_MIN_MOVE_DB:
                                compl_boosts.append(scaled)
                            else:
                                no_boost_reasons["below_min_move"] = no_boost_reasons.get("below_min_move", 0) + 1
                        if compl_boosts:
                            reaeq_boosts = _merge_moves_to_reaeq(
                                compl_boosts, calibration,
                            )

            # Selective makeup (subtractive-only): boost uncut bands.
            makeup_profile = compute_reaeq_makeup_profile(
                cuts,
                calibration,
                makeup_mode=makeup_mode,
                allow_selective_makeup=(strategy != "hybrid" or hybrid_selective_makeup),
            )
            selective_boosts = makeup_profile.get("boosts_by_reaeq_band", {})
            makeup_db = makeup_profile.get("target_makeup_db", 0.0)
            makeup_applied_db = makeup_profile.get("applied_makeup_db", 0.0)
            makeup_unapplied_db = makeup_profile.get("unapplied_makeup_db", 0.0)
            makeup_policy = makeup_profile.get("policy", "off")

            # Build envelope points for each band (cuts + hybrid boosts + selective makeup).
            for bi in info["gain_params"]:
                if bi in reaeq_cuts:
                    # Masking cut always wins
                    gain_norm = db_to_normalized(reaeq_cuts[bi]["cut_db"], gain_cal)
                elif bi in reaeq_boosts:
                    # Complementary boost (only if no masking cut on this band)
                    gain_norm = db_to_normalized(reaeq_boosts[bi]["gain_db"], gain_cal)
                elif bi in selective_boosts:
                    gain_norm = db_to_normalized(float(selective_boosts[bi]), gain_cal)
                else:
                    gain_norm = flat_gain_norm
                envelope_points[idx][bi].append({
                    "time": sec_start, "value": gain_norm, "shape": 1,
                })

            has_activity = bool(reaeq_cuts) or bool(reaeq_boosts) or bool(selective_boosts)
            if has_activity:
                sec_eq_count += 1
                # Collect boost moves for logging
                boost_moves_compl = [
                    {"band_index": reaeq_boosts[bi].get("band_index", bi), "gain_db": reaeq_boosts[bi]["gain_db"]}
                    for bi in reaeq_boosts
                ]
                boost_moves_makeup = [
                    {"band_index": bi, "gain_db": float(selective_boosts[bi])}
                    for bi in sorted(selective_boosts)
                ]
                sec_result_entry: dict = {
                    "section": sec_label,
                    "cuts": cuts,
                    "boosts": boost_moves_compl + boost_moves_makeup,
                    "complementary_boosts": boost_moves_compl,
                    "selective_makeup_boosts": boost_moves_makeup,
                    "makeup_db": round(makeup_db, 2),
                    "makeup_applied_db": round(makeup_applied_db, 2),
                    "makeup_unapplied_db": round(makeup_unapplied_db, 2),
                    "makeup_policy": makeup_policy,
                    "higher_priority_tracks": active_higher_names,
                    # Backward-compatible alias.
                    "yielding_to": active_higher_names,
                }
                if no_boost_reasons:
                    sec_result_entry["no_boost_reasons"] = dict(no_boost_reasons)
                track_cut_results[idx].append(sec_result_entry)

        section_summaries.append({
            "label": sec_label,
            "start": sec_start,
            "end": sec_end,
            "eq_count": sec_eq_count,
        })

    # -----------------------------------------------------------------------
    # Phase 3: Gap handling and envelope writing
    # -----------------------------------------------------------------------
    for idx, info in track_fx_info.items():
        for bi, points in envelope_points[idx].items():
            # Sort by time
            points.sort(key=lambda p: p["time"])

            # Gap handling: flat at t=0 if first section doesn't start there
            # In merge mode, skip the t=0 reset to preserve prior automation.
            if effective_mode != "merge":
                if sections[0][1] > 0 and (not points or points[0]["time"] > 0):
                    points.insert(0, {"time": 0.0, "value": flat_gain_norm, "shape": 1})

            # Flat reset at every section_end to handle gaps + after last section
            extra_points: list[dict] = []
            for _label, _start, sec_end in sections:
                # Check if there's already a point at sec_end
                has_end = any(abs(p["time"] - sec_end) < 0.001 for p in points)
                if not has_end:
                    extra_points.append({
                        "time": sec_end, "value": flat_gain_norm, "shape": 1,
                    })
            points.extend(extra_points)
            points.sort(key=lambda p: p["time"])

    # Remove ReaEQ from tracks that got zero cuts across all sections
    # (they'd otherwise be left with a useless flat 11-band EQ).
    no_cut_idxs = []
    for idx, info in track_fx_info.items():
        all_flat = all(
            all(abs(p["value"] - flat_gain_norm) < 1e-6 for p in points)
            for points in envelope_points[idx].values()
            if points
        )
        if all_flat:
            # In merge mode, only remove FX that were freshly created this run.
            # Pre-existing [AutoEQ] may have useful points outside processed range.
            if effective_mode == "replace" or idx in created_this_run:
                no_cut_idxs.append(idx)
    for idx in no_cut_idxs:
        info = track_fx_info[idx]
        rm_result = send_command_fn(
            "remove_fx", track=idx, fx_index=info["fx_index"],
            strict=True, timeout=10,
        )
        if rm_result.get("status") == "ok":
            track_fx_info.pop(idx)
            del envelope_points[idx]
        else:
            all_errors.append(
                f"Failed to remove flat AutoEQ from '{info['name']}': "
                f"{'; '.join(str(e) for e in rm_result.get('errors', ['Unknown']))}"
            )

    # Write envelopes via batched daemon command
    tracks_written = 0
    total_envelopes = 0
    total_points = 0

    for idx, info in track_fx_info.items():
        env_entries: list[dict] = []
        for bi, points in envelope_points[idx].items():
            if not points:
                continue
            pi = info["gain_params"].get(bi)
            if pi is None:
                continue
            entry = {
                "param_index": pi,
                "param_name": f"Gain-{band_names[bi]}" if bi < len(band_names) else None,
                "points": points,
                "clear_first": (effective_mode == "replace"),
            }
            if effective_mode == "merge":
                entry["clear_range_start"] = section_write_start - 1e-4
                entry["clear_range_end"] = section_write_end + 1e-4
            env_entries.append(entry)


        if not env_entries:
            continue

        result = send_command_fn(
            "set_fx_envelopes",
            track=idx,
            fx_index=info["fx_index"],
            envelopes=env_entries,
            strict=True,
            timeout=30,
        )

        if result.get("status") in ("ok", "partial"):
            tracks_written += 1
            total_envelopes += int(result.get("envelopes_written", 0))
            total_points += int(result.get("points_total", 0))
            # Surface partial errors (some bands failed)
            if result.get("status") == "partial":
                partial_errs = result.get("errors", [])
                if partial_errs:
                    all_errors.append(
                        f"Partial envelope write for '{info['name']}': "
                        f"{'; '.join(str(e) for e in partial_errs)}"
                    )
        else:
            err_detail = result.get("errors", ["Unknown error"])
            # Check for daemon-needs-updating pattern
            err_str = "; ".join(str(e) for e in err_detail)
            if "Unknown operation" in err_str:
                all_errors.append(
                    "Daemon needs updating — copy reaper_daemon.lua "
                    "and restart the script in REAPER."
                )
                break
            all_errors.append(
                f"Envelope write failed for '{info['name']}': {err_str}"
            )

    # Determine status
    status = "ok"
    if not tracks_written and all_errors:
        status = "error"
    elif all_errors:
        status = "partial"

    # Log summary
    summary_lines = [
        f"=== Auto-EQ Sections complete ({len(sections)} sections, "
        f"{tracks_written} tracks) ==="
    ]
    for ss in section_summaries:
        summary_lines.append(
            f"  {ss['label']} ({ss['start']:.0f}s-{ss['end']:.0f}s): "
            f"{ss['eq_count']} tracks EQ'd"
        )
    for idx, info in track_fx_info.items():
        sections_with_cuts = track_cut_results.get(idx, [])
        if not sections_with_cuts:
            continue
        for sec in sections_with_cuts:
            sec_label = sec["section"]
            sec_makeup = sec.get("makeup_applied_db", sec.get("makeup_db", 0.0))
            higher_refs = sec.get("higher_priority_tracks", sec.get("yielding_to", []))
            for c in sec["cuts"]:
                bi = c.get("band_index", 0)
                label = BAND_LABELS[bi] if bi < len(BAND_LABELS) else f"Band {bi}"
                db = c.get("cut_db", 0)
                contributors = c.get("contributors", [])
                if contributors:
                    detail_suffix = f" (contributors: {', '.join(contributors)})"
                elif higher_refs:
                    detail_suffix = f" (higher-priority refs: {', '.join(higher_refs)})"
                else:
                    detail_suffix = ""
                summary_lines.append(
                    f"  [{sec_label}] ducked {label.lower()} {db:+.1f} dB from {info['name']}{detail_suffix}"
                )
            for b in sec.get("boosts", []):
                bi = b.get("band_index", 0)
                label = BAND_LABELS[bi] if bi < len(BAND_LABELS) else f"Band {bi}"
                db = b.get("gain_db", 0)
                summary_lines.append(
                    f"  [{sec_label}] boosted {label.lower()} {db:+.1f} dB on {info['name']}"
                )
            if sec_makeup > 0:
                summary_lines.append(
                    f"  [{sec_label}] selective makeup +{sec_makeup:.1f} dB on {info['name']}"
                )
            # No-boost reason summary (hybrid mode only)
            reasons = sec.get("no_boost_reasons")
            if reasons and not sec.get("boosts"):
                reason_parts = [f"{v} {k}" for k, v in reasons.items()]
                summary_lines.append(
                    f"  [{sec_label}] no boosts on {info['name']}: {', '.join(reason_parts)}"
                )
    send_command_fn("log", message="\n".join(summary_lines), timeout=5)

    # Audit artifact
    audit_payload = {
        "mode": "auto_eq_sections",
        "created_at_utc": _now_utc_iso(),
        "params": {
            "max_cut_db": max_cut_db,
            "aggressiveness": aggressiveness,
            "max_cuts": max_cuts,
            "level": level,
            "makeup_mode": makeup_mode,
            "analyze_range": list(analyze_range) if analyze_range else None,
            "strategy": strategy,
            "family": family,
            "max_boost_db": max_boost_db,
            "write_mode": write_mode,
            "write_mode_resolved": effective_mode,
            "hybrid_selective_makeup": hybrid_selective_makeup,
        },
        "sections": [
            {"label": l, "start": s, "end": e}
            for l, s, e in sections
        ],
        "section_summaries": section_summaries,
        "targets": [t[0] for t in scored],
        "yield_tracks": [info["name"] for info in track_fx_info.values()],
        "tracks_written": tracks_written,
        "total_envelopes": total_envelopes,
        "total_points": total_points,
        "status": status,
        "errors": all_errors,
    }
    audit_path = _write_audit_artifact("auto_eq_sections", audit_payload)

    return _finalize_with_preflight(
        {
            "status": status,
            "mode": "auto_eq_sections",
            "strategy": strategy,
            "level_requested": (level or "auto").lower(),
            "level_resolved": resolved_level,
            "write_mode_requested": write_mode,
            "write_mode_resolved": effective_mode,
            "write_range": [section_write_start, section_write_end],
            "audit_path": audit_path,
            "sections": section_summaries,
            "targets": [t[0] for t in scored],
            "tracks_written": tracks_written,
            "total_envelopes": total_envelopes,
            "total_points": total_points,
            "results": [
                {
                    "track": info["name"],
                    "higher_priority_tracks": sorted(set(
                        name
                        for sec in track_cut_results.get(idx, [])
                        for name in sec.get(
                            "higher_priority_tracks",
                            sec.get("yielding_to", []),
                        )
                    )),
                    # Backward-compatible alias.
                    "yielding_to": sorted(set(
                        name
                        for sec in track_cut_results.get(idx, [])
                        for name in sec.get(
                            "higher_priority_tracks",
                            sec.get("yielding_to", []),
                        )
                    )),
                    "sections": track_cut_results.get(idx, []),
                    "cuts": [
                        c for sec in track_cut_results.get(idx, [])
                        for c in sec["cuts"]
                    ],
                    "boosts": [
                        b for sec in track_cut_results.get(idx, [])
                        for b in sec.get("boosts", [])
                    ],
                    "makeup_db": max(
                        (sec.get("makeup_db", 0.0)
                         for sec in track_cut_results.get(idx, [])),
                        default=0.0,
                    ),
                    "makeup_applied_db": max(
                        (sec.get("makeup_applied_db", 0.0)
                         for sec in track_cut_results.get(idx, [])),
                        default=0.0,
                    ),
                    "makeup_unapplied_db": max(
                        (sec.get("makeup_unapplied_db", 0.0)
                         for sec in track_cut_results.get(idx, [])),
                        default=0.0,
                    ),
                    "makeup_policy": (
                        "selective"
                        if any(
                            sec.get("makeup_policy") == "selective"
                            for sec in track_cut_results.get(idx, [])
                        )
                        else (
                            "hybrid_disabled"
                            if any(
                                sec.get("makeup_policy") == "hybrid_disabled"
                                for sec in track_cut_results.get(idx, [])
                            )
                            else "off"
                        )
                    ),
                }
                for idx, info in track_fx_info.items()
                if track_cut_results.get(idx)
            ],
            "errors": all_errors,
        },
        preflight,
        send_command_fn,
        "auto_eq_sections",
    )
