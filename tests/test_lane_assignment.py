"""Tests for inter-family spectral lane assignment."""

import math

import pytest

from bridge.auto_eq import (
    BAND_EDGES,
    BAND_LABELS,
    COMPL_MEAN_ACTIVITY_FLOOR_DB,
    COMPL_ROLE_BANDS,
    COMPL_SUPPORT_BOOST_SCALE,
    FAMILY_LANE_FREQ,
    LANE_ACTIVITY_FLOOR_DB,
    LANE_CONTESTED_BANDS,
    LANE_DOMINANCE_THRESHOLD_DB,
    MAKEUP_IMPORTANCE_FLOOR_DB,
    MAKEUP_IMPORTANCE_GAP_DB,
    MAKEUP_PER_BAND_CAP_DB,
    PRIORITY_LANE_FLOOR_DB,
    PRIORITY_LANE_GAP_DB,
    _assign_spectral_lanes,
    _compute_spectral_importance,
    _compute_contested_bands,
    _compute_family_aggregate_spectrum,
    _compute_complementary_moves_with_details,
    _compute_makeup_importance,
    _compute_selective_makeup_boosts,
    _family_band_means,
    _representative_freq,
    _spread_intra_family_boosts,
    _sub_lane_freq,
    build_eq_params_for_moves,
    build_reaeq_gain_moves,
    compute_reaeq_makeup_profile,
    match_track_priority,
)

NUM_BANDS = len(BAND_EDGES) - 1  # 10


def _make_spectrum(values: list[float]) -> list[float]:
    """Pad or truncate a list to NUM_BANDS."""
    result = list(values)
    while len(result) < NUM_BANDS:
        result.append(-120.0)
    return result[:NUM_BANDS]


def _make_bands(values: list[float]) -> list[dict]:
    """Build analysis band dicts from dB values."""
    bands = []
    for i, db in enumerate(values):
        lo = BAND_EDGES[i]
        hi = BAND_EDGES[i + 1]
        bands.append({"lo": lo, "hi": hi, "avg_db": db})
    return bands


def _make_analysis_row(values: list[float]) -> dict:
    """Build a full analysis result dict from dB values."""
    return {"bands": _make_bands(_make_spectrum(values))}


# ---------------------------------------------------------------------------
# _compute_contested_bands
# ---------------------------------------------------------------------------

class TestContestedBands:
    def test_known_contested_bands(self):
        result = _compute_contested_bands()
        # Bands 4-9 should be contested (guitar + keys-synth + vocal + cymbal overlap)
        for b in range(4, 10):
            assert b in result, f"Band {b} should be contested"
        # Band 0 (Sub) — no family boost targets
        # Band 1 (Bass) — no family targets
        # Band 2 (Low-Mid) — no family targets
        # Band 3 (Lower-Mid) — only vocal anchor (single family, not contested)
        assert 0 not in result
        assert 1 not in result
        assert 2 not in result
        assert 3 not in result

    def test_result_matches_module_constant(self):
        assert LANE_CONTESTED_BANDS == _compute_contested_bands()


# ---------------------------------------------------------------------------
# _compute_family_aggregate_spectrum
# ---------------------------------------------------------------------------

class TestFamilyAggregateSpectrum:
    def test_basic_aggregation(self):
        """Max-envelope: takes max dB across tracks per band."""
        rows_guitar = [
            _make_analysis_row([-20.0] * 10),
            _make_analysis_row([-30.0] * 10),
        ]
        rows_keys = [
            _make_analysis_row([-15.0] * 10),
        ]
        result = _compute_family_aggregate_spectrum({
            "guitar": rows_guitar,
            "keys-synth": rows_keys,
        })
        assert "guitar" in result
        assert "keys-synth" in result
        assert len(result["guitar"]) == NUM_BANDS
        # Guitar max should be max(-20, -30) = -20 (not mean -25)
        assert result["guitar"][0] == pytest.approx(-20.0)
        assert result["keys-synth"][0] == pytest.approx(-15.0)

    def test_max_envelope_not_diluted(self):
        """One loud track + one quiet track → result is the loud value."""
        rows = [
            _make_analysis_row([-10.0] * 10),
            _make_analysis_row([-50.0] * 10),
        ]
        result = _compute_family_aggregate_spectrum({"guitar": rows})
        assert result["guitar"][0] == pytest.approx(-10.0)


# ---------------------------------------------------------------------------
# _assign_spectral_lanes
# ---------------------------------------------------------------------------

class TestAssignSpectralLanes:
    def test_clear_dominance(self):
        """Different spectral shapes → each family owns its peak bands."""
        # Guitar peaks in band 7 (High Presence), keys peaks in band 4 (Mid)
        # Both bands are contested between guitar and keys-synth.
        guitar_spec = [-30.0] * 10
        guitar_spec[7] = -10.0  # strong in band 7
        keys_spec = [-30.0] * 10
        keys_spec[4] = -10.0  # strong in band 4

        spectra = {
            "guitar": _make_spectrum(guitar_spec),
            "keys-synth": _make_spectrum(keys_spec),
        }
        result = _assign_spectral_lanes(spectra)
        # Guitar should own band 7 (its peak)
        assert result["lane_owners"].get(7) == "guitar"
        # Keys should own band 4 (its peak)
        assert result["lane_owners"].get(4) == "keys-synth"

    def test_within_threshold_shared(self):
        """Two families within threshold → shared, no blocking."""
        spectra = {
            "guitar": _make_spectrum([-20.0] * 10),
            "keys-synth": _make_spectrum([-21.5] * 10),  # 1.5 dB margin < 3.0
        }
        result = _assign_spectral_lanes(spectra)
        # Same shape → normalization makes them identical → all shared
        for b, owner in result["lane_owners"].items():
            assert owner is None, f"Band {b} should be shared (margin < threshold)"
        # No constraints when shared
        for fam in ("guitar", "keys-synth"):
            assert not result["lane_constraints"].get(fam, {})

    def test_exact_tie_shared(self):
        """Exact same energy → shared."""
        spectra = {
            "guitar": _make_spectrum([-20.0] * 10),
            "keys-synth": _make_spectrum([-20.0] * 10),
        }
        result = _assign_spectral_lanes(spectra)
        for b, owner in result["lane_owners"].items():
            assert owner is None, f"Band {b} should be shared on exact tie"

    def test_single_family_no_constraints(self):
        """Single family → no contested bands, no constraints."""
        spectra = {
            "guitar": _make_spectrum([-20.0] * 10),
        }
        result = _assign_spectral_lanes(spectra)
        assert result["lane_owners"] == {}
        assert result["lane_constraints"] == {}
        assert result["contested_bands"] == []

    def test_three_families_one_dominant(self):
        """Three families, vocal has distinct peak → vocal owns that band."""
        # Vocal peaks in band 5, guitar and keys are flat
        vocal_spec = [-30.0] * 10
        vocal_spec[5] = -10.0  # strong in band 5 (Upper-Mid)

        guitar_spec = [-25.0] * 10  # flat, louder overall
        keys_spec = [-25.0] * 10    # flat

        spectra = {
            "vocal": _make_spectrum(vocal_spec),
            "guitar": _make_spectrum(guitar_spec),
            "keys-synth": _make_spectrum(keys_spec),
        }
        result = _assign_spectral_lanes(spectra)
        # Vocal has a peak in band 5 that differs from its own mean → owns it
        assert result["lane_owners"].get(5) == "vocal"

    def test_empty_spectra(self):
        """Empty input → graceful no-op."""
        result = _assign_spectral_lanes({})
        assert result["lane_owners"] == {}
        assert result["lane_constraints"] == {}

    def test_contested_bands_audit_detail(self):
        """Audit detail includes energies, normalized_energies, and margins."""
        guitar_spec = [-30.0] * 10
        guitar_spec[6] = -10.0
        keys_spec = [-30.0] * 10
        keys_spec[3] = -10.0

        spectra = {
            "guitar": _make_spectrum(guitar_spec),
            "keys-synth": _make_spectrum(keys_spec),
        }
        result = _assign_spectral_lanes(spectra)
        assert len(result["contested_bands"]) > 0
        detail = result["contested_bands"][0]
        assert "band_index" in detail
        assert "margin_db" in detail
        assert "energies" in detail
        assert "normalized_energies" in detail
        assert "decision" in detail

    def test_mixed_dominance(self):
        """Different families dominate different bands."""
        guitar_spec = [-40.0] * 10  # quiet everywhere
        guitar_spec[5] = -10.0     # loud in band 5 (upper-mid)
        keys_spec = [-40.0] * 10
        keys_spec[3] = -10.0       # loud in band 3 (lower-mid)
        spectra = {
            "guitar": _make_spectrum(guitar_spec),
            "keys-synth": _make_spectrum(keys_spec),
        }
        result = _assign_spectral_lanes(spectra)
        # Band 5 should be owned by guitar (30 dB margin)
        if 5 in result["lane_owners"]:
            assert result["lane_owners"][5] == "guitar"
        # Band 3 should be owned by keys-synth
        if 3 in result["lane_owners"]:
            assert result["lane_owners"][3] == "keys-synth"

    # --- New normalization/activity tests ---

    def test_volume_difference_same_shape(self):
        """Identical spectral shape but 70 dB volume difference → all shared."""
        spectra = {
            "guitar": _make_spectrum([-10.0] * 10),
            "keys-synth": _make_spectrum([-80.0] * 10),  # 70 dB quieter, same shape
        }
        # keys-synth max is -80, but if no analysis rows provided, fallback
        # checks max(spectrum) >= -45 → -80 < -45 → keys excluded → < 2 active
        # So we need to pass analysis rows to override:
        family_analysis_rows = {
            "guitar": [_make_analysis_row([-10.0] * 10)],
            "keys-synth": [_make_analysis_row([-20.0] * 10)],  # peak above floor
        }
        result = _assign_spectral_lanes(spectra, family_analysis_rows)
        # Same shape → normalization removes offset → all shared
        for b, owner in result["lane_owners"].items():
            assert owner is None, f"Band {b} should be shared (same shape)"

    def test_different_shapes_different_volumes(self):
        """Guitar peaks in band 7, keys peaks in band 4, 30 dB volume diff → split."""
        guitar_spec = [-10.0] * 10
        guitar_spec[7] = 0.0  # peak in band 7 (High Presence)

        keys_spec = [-40.0] * 10
        keys_spec[4] = -30.0  # peak in band 4 (Mid, still 30 dB quieter overall)

        spectra = {
            "guitar": _make_spectrum(guitar_spec),
            "keys-synth": _make_spectrum(keys_spec),
        }
        family_analysis_rows = {
            "guitar": [_make_analysis_row(guitar_spec)],
            "keys-synth": [_make_analysis_row(keys_spec)],
        }
        result = _assign_spectral_lanes(spectra, family_analysis_rows)
        # Each owns their peak band despite volume difference
        assert result["lane_owners"].get(7) == "guitar"
        assert result["lane_owners"].get(4) == "keys-synth"

    def test_activity_floor_gate(self):
        """Family with all bands below activity floor → excluded entirely."""
        spectra = {
            "guitar": _make_spectrum([-10.0] * 10),
            "keys-synth": _make_spectrum([-80.0] * 10),  # far below floor
        }
        family_analysis_rows = {
            "guitar": [_make_analysis_row([-10.0] * 10)],
            "keys-synth": [_make_analysis_row([-80.0] * 10)],  # peak -80 < -45
        }
        result = _assign_spectral_lanes(spectra, family_analysis_rows)
        # Only one active family → no contests
        assert result["lane_owners"] == {}
        assert result["lane_constraints"] == {}
        assert result["contested_bands"] == []

    def test_activity_floor_fallback_no_rows(self):
        """Without analysis rows, fallback uses max(spectrum) for activity check."""
        spectra = {
            "guitar": _make_spectrum([-10.0] * 10),
            "keys-synth": _make_spectrum([-80.0] * 10),  # max = -80 < -45
        }
        # No family_analysis_rows → fallback
        result = _assign_spectral_lanes(spectra)
        assert result["lane_owners"] == {}
        assert result["lane_constraints"] == {}

    def test_audit_has_normalized_energies(self):
        """Contested band entries contain both energies and normalized_energies."""
        guitar_spec = [-30.0] * 10
        guitar_spec[6] = -10.0
        keys_spec = [-30.0] * 10
        keys_spec[3] = -10.0
        spectra = {
            "guitar": _make_spectrum(guitar_spec),
            "keys-synth": _make_spectrum(keys_spec),
        }
        result = _assign_spectral_lanes(spectra)
        for detail in result["contested_bands"]:
            assert "energies" in detail, "Missing raw energies"
            assert "normalized_energies" in detail, "Missing normalized energies"
            # Normalized values should differ from raw (mean subtracted)
            for fam in detail["energies"]:
                assert fam in detail["normalized_energies"]

    def test_nan_inf_sanitization(self):
        """Spectrum with inf/NaN values → clamped to -120, no crash."""
        spectra = {
            "guitar": _make_spectrum([float("inf"), float("-inf"), float("nan")] + [-20.0] * 7),
            "keys-synth": _make_spectrum([-25.0] * 10),
        }
        result = _assign_spectral_lanes(spectra)
        # Should not crash; result should be valid
        assert "lane_owners" in result
        assert "lane_constraints" in result
        assert "contested_bands" in result
        # Verify no NaN/inf in audit output
        for detail in result["contested_bands"]:
            for v in detail["energies"].values():
                assert math.isfinite(v)
            for v in detail["normalized_energies"].values():
                assert math.isfinite(v)
            assert math.isfinite(detail["margin_db"])


# ---------------------------------------------------------------------------
# Integration: lane_blocked in complementary moves
# ---------------------------------------------------------------------------

class TestLaneBlockedInComplementaryMoves:
    def test_lane_blocked_emitted(self):
        """Lane constraints cause skip_boost with reason lane_blocked."""
        track_bands = _make_bands(_make_spectrum([-30.0] * 10))
        family_means = _make_spectrum([-20.0] * 10)  # track is below mean → deficit

        # Block band 7 (High Presence) which is a guitar presence target {7, 8}
        lane_constraints = {7: "vocal"}

        result = _compute_complementary_moves_with_details(
            family="guitar",
            role="presence",
            track_bands=track_bands,
            family_means_db=family_means,
            max_cut_db=-3.0,
            max_boost_db=6.0,
            max_moves=4,
            lane_constraints=lane_constraints,
        )
        # Find band 7 decision
        band7_decisions = [
            bd for bd in result["band_decisions"]
            if bd["band_index"] == 7
        ]
        assert len(band7_decisions) == 1
        bd = band7_decisions[0]
        assert bd["decision"] == "skip_boost"
        assert bd["reason"] == "lane_blocked"
        assert bd["lane_owner"] == "vocal"

    def test_no_lane_constraints_no_blocking(self):
        """Without lane constraints, no lane_blocked decisions."""
        track_bands = _make_bands(_make_spectrum([-30.0] * 10))
        family_means = _make_spectrum([-20.0] * 10)

        result = _compute_complementary_moves_with_details(
            family="guitar",
            role="presence",
            track_bands=track_bands,
            family_means_db=family_means,
            max_cut_db=-3.0,
            max_boost_db=6.0,
            max_moves=4,
        )
        lane_blocked = [
            bd for bd in result["band_decisions"]
            if bd.get("reason") == "lane_blocked"
        ]
        assert len(lane_blocked) == 0


# ---------------------------------------------------------------------------
# _family_band_means — activity-filtered
# ---------------------------------------------------------------------------

class TestFamilyBandMeans:
    def test_basic_mean_all_active(self):
        rows = [_make_analysis_row([-20] * NUM_BANDS),
                _make_analysis_row([-30] * NUM_BANDS)]
        means = _family_band_means(rows)
        assert len(means) == NUM_BANDS
        for m in means:
            assert m == pytest.approx(-25.0)

    def test_silent_tracks_excluded(self):
        """Silent tracks (-120 dB) should not drag the mean down."""
        active = _make_analysis_row([-20] * NUM_BANDS)
        silent = _make_analysis_row([-120] * NUM_BANDS)
        means = _family_band_means([active, silent, silent, silent])
        # Mean should reflect only the active track
        for m in means:
            assert m == pytest.approx(-20.0)

    def test_all_silent_returns_floor(self):
        silent = _make_analysis_row([-120] * NUM_BANDS)
        means = _family_band_means([silent, silent])
        for m in means:
            assert m == -120.0

    def test_boundary_at_floor(self):
        """Track exactly at the activity floor should be included."""
        at_floor = _make_analysis_row([COMPL_MEAN_ACTIVITY_FLOOR_DB] * NUM_BANDS)
        below = _make_analysis_row([COMPL_MEAN_ACTIVITY_FLOOR_DB - 1] * NUM_BANDS)
        means = _family_band_means([at_floor, below])
        for m in means:
            assert m == pytest.approx(COMPL_MEAN_ACTIVITY_FLOOR_DB)

    def test_empty_rows(self):
        means = _family_band_means([])
        assert means == [-120.0] * NUM_BANDS


# ---------------------------------------------------------------------------
# Support role fallback boosts
# ---------------------------------------------------------------------------

class TestSupportRoleBoosts:
    def test_support_gets_anchor_fallback(self):
        """Support track below family mean in anchor bands should get a boost."""
        # keys-synth anchor bands = {4, 5, 6, 7}
        # Track is well below family mean
        track_vals = [-40] * NUM_BANDS
        family_means = [-20] * NUM_BANDS  # track is -20 dB below mean
        track_bands = _make_bands(_make_spectrum(track_vals))
        result = _compute_complementary_moves_with_details(
            family="keys-synth",
            role="support",
            track_bands=track_bands,
            family_means_db=family_means,
            max_cut_db=-3.0,
            max_boost_db=6.0,
            max_moves=4,
        )
        boosts = [m for m in result["moves"] if m["kind"] == "boost"]
        assert len(boosts) > 0, "Support track should get fallback anchor boost"

    def test_support_boost_is_scaled(self):
        """Support fallback boosts should be at reduced magnitude."""
        # Use delta (-5 dB) so neither anchor nor support hits max_boost_db cap
        track_vals = [-25] * NUM_BANDS
        family_means = [-20] * NUM_BANDS
        track_bands = _make_bands(_make_spectrum(track_vals))
        # Support role
        support_result = _compute_complementary_moves_with_details(
            family="keys-synth",
            role="support",
            track_bands=track_bands,
            family_means_db=family_means,
            max_cut_db=-3.0,
            max_boost_db=6.0,
            max_moves=4,
        )
        # Anchor role (same bands, full scale)
        anchor_result = _compute_complementary_moves_with_details(
            family="keys-synth",
            role="anchor",
            track_bands=track_bands,
            family_means_db=family_means,
            max_cut_db=-3.0,
            max_boost_db=6.0,
            max_moves=4,
        )
        support_boosts = [m for m in support_result["moves"] if m["kind"] == "boost"]
        anchor_boosts = [m for m in anchor_result["moves"] if m["kind"] == "boost"]
        assert len(support_boosts) > 0
        assert len(anchor_boosts) > 0
        # Support boost should be roughly half of anchor boost
        assert support_boosts[0]["gain_db"] < anchor_boosts[0]["gain_db"]
        assert support_boosts[0]["gain_db"] == pytest.approx(
            anchor_boosts[0]["gain_db"] * COMPL_SUPPORT_BOOST_SCALE, abs=0.1)

    def test_non_support_unaffected(self):
        """Anchor role should not use fallback logic."""
        track_vals = [-40] * NUM_BANDS
        family_means = [-20] * NUM_BANDS
        track_bands = _make_bands(_make_spectrum(track_vals))
        result = _compute_complementary_moves_with_details(
            family="keys-synth",
            role="anchor",
            track_bands=track_bands,
            family_means_db=family_means,
            max_cut_db=-3.0,
            max_boost_db=6.0,
            max_moves=4,
        )
        for bd in result["band_decisions"]:
            assert bd.get("support_fallback") is False

    def test_support_cap_in_deficit_path(self, monkeypatch):
        """Support deficit-path boost should be clamped by COMPL_SUPPORT_MAX_BOOST_DB.

        With monkeypatched cap below the natural deficit-path slope output, the cap
        becomes the binding constraint; without the cap the raw value would exceed it.
        """
        from bridge import auto_eq
        # Large deficit so deficit-path slope (abs(delta) * 0.5) lands ~10 dB before cap
        track_vals = [-40] * NUM_BANDS
        family_means = [-20] * NUM_BANDS
        track_bands = _make_bands(_make_spectrum(track_vals))
        monkeypatch.setattr(auto_eq, "COMPL_SUPPORT_MAX_BOOST_DB", 1.5)
        result = _compute_complementary_moves_with_details(
            family="keys-synth",
            role="support",
            track_bands=track_bands,
            family_means_db=family_means,
            max_cut_db=-3.0,
            max_boost_db=6.0,
            max_moves=4,
        )
        boosts = [m for m in result["moves"] if m["kind"] == "boost"]
        assert len(boosts) > 0
        for m in boosts:
            assert m["gain_db"] <= 1.5 + 1e-6, (
                f"Support boost {m['gain_db']} dB exceeded cap of 1.5 dB"
            )

    def test_anti_refill_blocks_boost(self):
        """Anti-refill aggregate with cuts >= threshold blocks target-band boosts."""
        # Track far below family mean → would normally boost
        track_vals = [-40] * NUM_BANDS
        family_means = [-20] * NUM_BANDS
        track_bands = _make_bands(_make_spectrum(track_vals))

        # ReaEQ band index for guitar anchor band 4 (Mid 320-640) via DEFAULT map
        from bridge.auto_eq import DEFAULT_BAND_TO_REAEQ
        reaeq_idx = DEFAULT_BAND_TO_REAEQ[4]
        anti_refill = {
            reaeq_idx: {"cuts": 2, "total_db": -6.0, "tracks": ["snare", "vocal"]}
        }

        result = _compute_complementary_moves_with_details(
            family="guitar",
            role="anchor",
            track_bands=track_bands,
            family_means_db=family_means,
            max_cut_db=-3.0,
            max_boost_db=6.0,
            max_moves=4,
            anti_refill_blocks=anti_refill,
        )
        # Band 4 should be skipped with reason anti_refill
        band4 = next(bd for bd in result["band_decisions"] if bd["band_index"] == 4)
        assert band4["decision"] == "skip_boost"
        assert band4["reason"] == "anti_refill"
        assert band4["anti_refill_cuts"] == 2
        assert band4["anti_refill_total_db"] == -6.0
        # No boost should land on band 4
        assert not any(m.get("band_index") == 4 and m["kind"] == "boost"
                       for m in result["moves"])

    def test_anti_refill_allows_single_cut(self):
        """A single cut on a band (below thresholds) does not block boosts."""
        track_vals = [-40] * NUM_BANDS
        family_means = [-20] * NUM_BANDS
        track_bands = _make_bands(_make_spectrum(track_vals))

        from bridge.auto_eq import DEFAULT_BAND_TO_REAEQ
        reaeq_idx = DEFAULT_BAND_TO_REAEQ[4]
        # Below MIN_CUTS=2 and below MIN_TOTAL_DB=6
        anti_refill = {
            reaeq_idx: {"cuts": 1, "total_db": -3.0, "tracks": ["snare"]}
        }

        result = _compute_complementary_moves_with_details(
            family="guitar",
            role="anchor",
            track_bands=track_bands,
            family_means_db=family_means,
            max_cut_db=-3.0,
            max_boost_db=6.0,
            max_moves=4,
            anti_refill_blocks=anti_refill,
        )
        band4 = next(bd for bd in result["band_decisions"] if bd["band_index"] == 4)
        assert band4["reason"] != "anti_refill"

    def test_anti_refill_aggregates_non_family_cuts(self):
        """The anti-refill aggregate must cover singleton/non-family selected tracks.

        Guards the round-2 regression: family_groups filters to 2+-member families,
        so cuts on singletons (snaremic, etc.) would be invisible to the boost pass
        unless aggregation pulls from the full selection.
        """
        from bridge.auto_eq import (
            _build_anti_refill_aggregate,
            DEFAULT_BAND_TO_REAEQ,
        )

        # Selection: 1 family member (guitar) + 1 singleton (snare). The singleton is
        # the only track with [AutoEQ] cuts. Aggregation must still capture it.
        selected = [
            {"index": 1, "name": "ajguitar"},  # no cuts
            {"index": 5, "name": "snaremic"},  # has cuts at band 4
        ]

        # Mock daemon: returns an [AutoEQ] FX chain only for snaremic
        class FakeDaemon:
            def __init__(self):
                self.calls = []

            def __call__(self, command, **kwargs):
                self.calls.append((command, kwargs))
                if command != "get_track_fx":
                    return {"status": "ok"}
                tidx = kwargs.get("track")
                if tidx == 5:
                    # An [AutoEQ] ReaEQ with a -3 dB cut at ReaEQ band index 1 (Band 2)
                    return {
                        "status": "ok",
                        "fx_chain": [{
                            "name": "VST: ReaEQ (Cockos) [AutoEQ]",
                            "index": 0,
                            "params": [
                                # ReaEQ band 1 gain — encoded as the calibration expects
                            ],
                        }],
                    }
                return {"status": "ok", "fx_chain": []}

        # Calibration only needs to provide the band map indirectly used elsewhere.
        # read_masking_constraints does the actual parsing — we'll monkey-test by
        # patching it to return the cuts we want for the snare and {} otherwise.
        import bridge.auto_eq as ae

        original_read = ae.read_masking_constraints
        # ReaEQ band index 1 (whatever it maps to for analysis band 4)
        reaeq_band_for_b4 = DEFAULT_BAND_TO_REAEQ[4]
        def fake_read(chain, calibration):
            if chain and chain[0]["name"].startswith("VST: ReaEQ (Cockos) [AutoEQ]"):
                return {reaeq_band_for_b4: -3.0}
            return {}

        ae.read_masking_constraints = fake_read
        try:
            mc, pc, po, anti = _build_anti_refill_aggregate(
                selected, FakeDaemon(), calibration={}
            )
        finally:
            ae.read_masking_constraints = original_read

        # snaremic's cut must appear in the aggregate even though it's not in any family
        assert reaeq_band_for_b4 in anti
        assert anti[reaeq_band_for_b4]["cuts"] == 1
        assert anti[reaeq_band_for_b4]["total_db"] == -3.0
        assert "snaremic" in anti[reaeq_band_for_b4]["tracks"]
        # ajguitar should have empty constraints (no cuts on it)
        assert mc[1] == {}
        assert mc[5] == {reaeq_band_for_b4: -3.0}

    def test_anti_refill_param_optional(self):
        """Existing callers without anti_refill_blocks still work (no-op default)."""
        track_vals = [-40] * NUM_BANDS
        family_means = [-20] * NUM_BANDS
        track_bands = _make_bands(_make_spectrum(track_vals))
        # No anti_refill_blocks passed at all
        result = _compute_complementary_moves_with_details(
            family="guitar",
            role="anchor",
            track_bands=track_bands,
            family_means_db=family_means,
            max_cut_db=-3.0,
            max_boost_db=6.0,
            max_moves=4,
        )
        for bd in result["band_decisions"]:
            assert bd.get("reason") != "anti_refill"

    def test_starvation_fallback_fires_when_targets_blocked(self):
        """When all role-target bands are lane-blocked but a non-target band has
        high delta_db, a starvation_recovery move should be emitted."""
        from bridge.auto_eq import (
            COMPL_STARVATION_MAX_DB,
            COMPL_STARVATION_MIN_DELTA_DB,
        )
        # Guitar anchor target bands = {4,5,6,7}
        # Set the track strong on band 8 (Brilliance, non-target) at +8 dB above mean,
        # weak elsewhere; lane-block all anchor targets.
        track_vals = [-30] * NUM_BANDS
        track_vals[8] = -12  # delta = +8 vs family mean -20
        family_means = [-20] * NUM_BANDS
        track_bands = _make_bands(_make_spectrum(track_vals))
        # Lane-block all anchor target bands
        lane_constraints = {4: "vocal", 5: "vocal", 6: "vocal", 7: "cymbal"}

        result = _compute_complementary_moves_with_details(
            family="guitar",
            role="anchor",
            track_bands=track_bands,
            family_means_db=family_means,
            max_cut_db=-3.0,
            max_boost_db=6.0,
            max_moves=4,
            lane_constraints=lane_constraints,
        )
        recovery = [m for m in result["moves"] if m.get("reason") == "starvation_recovery"]
        assert len(recovery) == 1, f"Expected 1 starvation_recovery move, got {result['moves']}"
        rec = recovery[0]
        assert rec["band_index"] == 8
        assert rec["gain_db"] <= COMPL_STARVATION_MAX_DB + 1e-6
        assert rec["gain_db"] >= COMPL_STARVATION_MIN_DELTA_DB * 0.5 - 1e-6 \
            or rec["gain_db"] == COMPL_STARVATION_MAX_DB

    def test_starvation_fallback_skips_when_no_qualifying_band(self):
        """No non-target band has delta >= threshold → no participation trophy."""
        # All bands flat at family mean, anchor targets lane-blocked
        track_vals = [-20] * NUM_BANDS
        family_means = [-20] * NUM_BANDS
        track_bands = _make_bands(_make_spectrum(track_vals))
        lane_constraints = {4: "vocal", 5: "vocal", 6: "vocal", 7: "cymbal"}
        result = _compute_complementary_moves_with_details(
            family="guitar",
            role="anchor",
            track_bands=track_bands,
            family_means_db=family_means,
            max_cut_db=-3.0,
            max_boost_db=6.0,
            max_moves=4,
            lane_constraints=lane_constraints,
        )
        recovery = [m for m in result["moves"] if m.get("reason") == "starvation_recovery"]
        assert len(recovery) == 0

    def test_starvation_trigger_requires_blocked_evidence(self):
        """Track with low energy and no blocking → no recovery (genuinely flat)."""
        track_vals = [-50] * NUM_BANDS
        family_means = [-50] * NUM_BANDS
        track_bands = _make_bands(_make_spectrum(track_vals))
        # No lane_constraints, no masking_constraints, no anti_refill_blocks → no blocking
        result = _compute_complementary_moves_with_details(
            family="guitar",
            role="anchor",
            track_bands=track_bands,
            family_means_db=family_means,
            max_cut_db=-3.0,
            max_boost_db=6.0,
            max_moves=4,
        )
        recovery = [m for m in result["moves"] if m.get("reason") == "starvation_recovery"]
        assert len(recovery) == 0

    def test_starvation_skips_support_role(self):
        """Support role already gets reduced treatment; no extra recovery boost."""
        track_vals = [-30] * NUM_BANDS
        track_vals[8] = -12  # strong on non-target band
        family_means = [-20] * NUM_BANDS
        track_bands = _make_bands(_make_spectrum(track_vals))
        lane_constraints = {4: "vocal", 5: "vocal", 6: "vocal", 7: "cymbal"}
        result = _compute_complementary_moves_with_details(
            family="guitar",
            role="support",
            track_bands=track_bands,
            family_means_db=family_means,
            max_cut_db=-3.0,
            max_boost_db=6.0,
            max_moves=4,
            lane_constraints=lane_constraints,
        )
        recovery = [m for m in result["moves"] if m.get("reason") == "starvation_recovery"]
        assert len(recovery) == 0

    def test_support_cap_in_ownership_path(self, monkeypatch):
        """Support ownership-path boost should be clamped by COMPL_SUPPORT_MAX_BOOST_DB.

        Default ownership formula yields 6.0 * 0.5 = 3.0 dB, equal to default cap, so
        the test would pass even without the cap. Monkeypatch cap to 1.0 dB to force
        the cap to be the binding constraint.
        """
        from bridge import auto_eq
        # Track at family mean: not deficit, ownership path fires
        track_vals = [-20] * NUM_BANDS
        family_means = [-20] * NUM_BANDS
        track_bands = _make_bands(_make_spectrum(track_vals))
        monkeypatch.setattr(auto_eq, "COMPL_SUPPORT_MAX_BOOST_DB", 1.0)
        result = _compute_complementary_moves_with_details(
            family="keys-synth",
            role="support",
            track_bands=track_bands,
            family_means_db=family_means,
            max_cut_db=-3.0,
            max_boost_db=6.0,
            max_moves=4,
        )
        boosts = [m for m in result["moves"] if m["kind"] == "boost"]
        # Support ownership boosts should exist (anchor bands at 50% scale = 3.0 raw)
        # and they should all be capped at 1.0
        for m in boosts:
            if m["reason"] == "support_ownership":
                assert m["gain_db"] <= 1.0 + 1e-6, (
                    f"Support ownership boost {m['gain_db']} dB exceeded cap of 1.0 dB"
                )


# ---------------------------------------------------------------------------
# Sub-lane frequency targeting
# ---------------------------------------------------------------------------

class TestSubLaneFrequency:
    def test_boost_has_target_hz(self):
        """Guitar boost moves should include family-specific target_hz."""
        track_bands = _make_bands(_make_spectrum([-30.0] * NUM_BANDS))
        family_means = _make_spectrum([-20.0] * NUM_BANDS)
        result = _compute_complementary_moves_with_details(
            family="guitar", role="anchor",
            track_bands=track_bands, family_means_db=family_means,
            max_cut_db=-3.0, max_boost_db=6.0, max_moves=4,
        )
        boosts = [m for m in result["moves"] if m["kind"] == "boost"]
        assert len(boosts) > 0
        for m in boosts:
            assert "target_hz" in m
            bi = m["band_index"]
            expected = FAMILY_LANE_FREQ["guitar"].get(bi)
            if expected:
                assert m["target_hz"] == expected

    def test_cut_has_no_target_hz(self):
        """Crowding cuts should NOT have target_hz."""
        # Track loud in non-target bands to trigger cuts
        track_vals = [-10.0] * NUM_BANDS
        family_means = [-20.0] * NUM_BANDS
        track_bands = _make_bands(_make_spectrum(track_vals))
        result = _compute_complementary_moves_with_details(
            family="guitar", role="anchor",
            track_bands=track_bands, family_means_db=family_means,
            max_cut_db=-3.0, max_boost_db=6.0, max_moves=4,
        )
        cuts = [m for m in result["moves"] if m["kind"] == "cut"]
        for m in cuts:
            assert "target_hz" not in m

    def test_representative_freq_uses_target_hz(self):
        """_representative_freq should prefer target_hz over geometric mean."""
        move = {"lo": 640, "hi": 1250, "target_hz": 750.0}
        assert _representative_freq(move) == 750.0

    def test_representative_freq_fallback(self):
        """Without target_hz, _representative_freq uses geometric mean."""
        move = {"lo": 640, "hi": 1250}
        expected = math.sqrt(640 * 1250)
        assert _representative_freq(move) == pytest.approx(expected)

    def test_representative_freq_bad_value_fallback(self):
        """Invalid target_hz values should fall back to geometric mean."""
        geo = math.sqrt(640 * 1250)
        for bad in [None, float("nan"), -100, 0, "abc"]:
            move = {"lo": 640, "hi": 1250, "target_hz": bad}
            assert _representative_freq(move) == pytest.approx(geo), f"Failed for target_hz={bad!r}"

    def test_families_get_different_freq_same_band(self):
        """Guitar and keys-synth in band 5 should get different sub-lane frequencies."""
        guitar_hz = FAMILY_LANE_FREQ["guitar"][5]
        keys_hz = FAMILY_LANE_FREQ["keys-synth"][5]
        assert guitar_hz != keys_hz
        assert abs(guitar_hz - keys_hz) > 200  # meaningful separation

    def test_sub_lane_freq_fallback(self):
        """Unknown family falls back to geometric mean."""
        geo = math.sqrt(640 * 1250)
        assert _sub_lane_freq("unknown", 5, 640, 1250) == pytest.approx(geo)


def _make_boost_move(band_index: int, target_hz: float, lo: float, hi: float,
                     track_index: int = 0, gain_db: float = 6.0) -> dict:
    """Helper to create a boost move dict for spread tests."""
    return {
        "band_index": band_index,
        "kind": "boost",
        "gain_db": gain_db,
        "lo": lo,
        "hi": hi,
        "target_hz": target_hz,
        "_track_index": track_index,
    }


class TestIntraFamilySpread:
    def test_spread_single_track_unchanged(self):
        """One move in a band — target_hz unchanged."""
        move = _make_boost_move(4, 550, 320, 640, track_index=0)
        original_hz = move["target_hz"]
        _spread_intra_family_boosts([move])
        assert move["target_hz"] == original_hz

    def test_spread_two_tracks_symmetric(self):
        """Two moves — frequencies spread symmetrically around center."""
        center = 550.0
        m1 = _make_boost_move(4, center, 320, 640, track_index=0)
        m2 = _make_boost_move(4, center, 320, 640, track_index=1)
        _spread_intra_family_boosts([m1, m2])
        # Both should be different from center
        assert m1["target_hz"] != m2["target_hz"]
        # Symmetry in log space: log2(m1) + log2(m2) ≈ 2 * log2(center)
        log_mean = (math.log2(m1["target_hz"]) + math.log2(m2["target_hz"])) / 2
        assert log_mean == pytest.approx(math.log2(center), abs=0.01)
        # Both within band
        assert 320 <= m1["target_hz"] <= 640
        assert 320 <= m2["target_hz"] <= 640

    def test_spread_five_tracks_within_band(self):
        """Five moves — all stay within [lo, hi]."""
        center = 550.0
        moves = [_make_boost_move(4, center, 320, 640, track_index=i) for i in range(5)]
        _spread_intra_family_boosts(moves)
        freqs = [m["target_hz"] for m in moves]
        # All within band
        for f in freqs:
            assert 320 <= f <= 640
        # All distinct
        assert len(set(freqs)) == 5
        # Sorted (since track_index is ascending)
        assert freqs == sorted(freqs)

    def test_spread_log_spacing(self):
        """Equal intervals in log2 space."""
        center = 550.0
        moves = [_make_boost_move(4, center, 320, 640, track_index=i) for i in range(4)]
        _spread_intra_family_boosts(moves)
        log_freqs = [math.log2(m["target_hz"]) for m in moves]
        # Check spacing is uniform
        diffs = [log_freqs[i + 1] - log_freqs[i] for i in range(len(log_freqs) - 1)]
        for d in diffs:
            assert d == pytest.approx(diffs[0], abs=0.001)

    def test_spread_ignores_cuts(self):
        """Only boost moves are spread; cuts untouched."""
        cut = {"band_index": 4, "kind": "cut", "gain_db": -3.0,
               "lo": 320, "hi": 640, "target_hz": 550.0, "_track_index": 0}
        boost = _make_boost_move(4, 550.0, 320, 640, track_index=1)
        _spread_intra_family_boosts([cut, boost])
        # Cut unchanged
        assert cut["target_hz"] == 550.0
        # Only one boost — no spreading needed
        assert boost["target_hz"] == 550.0

    def test_spread_different_bands_independent(self):
        """Moves in different bands are spread independently."""
        # Two moves in band 4, two in band 5
        b4_moves = [_make_boost_move(4, 550.0, 320, 640, track_index=i) for i in range(2)]
        b5_moves = [_make_boost_move(5, 1100.0, 640, 1250, track_index=i) for i in range(2)]
        _spread_intra_family_boosts(b4_moves + b5_moves)
        # Band 4 moves spread around 550
        assert b4_moves[0]["target_hz"] != b4_moves[1]["target_hz"]
        assert 320 <= b4_moves[0]["target_hz"] <= 640
        # Band 5 moves spread around 1100
        assert b5_moves[0]["target_hz"] != b5_moves[1]["target_hz"]
        assert 640 <= b5_moves[0]["target_hz"] <= 1250

    def test_spread_off_center_no_edge_clamp(self):
        """Center near band edge uses asymmetric-safe spread — no clamping."""
        # Center very close to lo edge
        center = 340.0  # Only 20 Hz above lo=320
        moves = [_make_boost_move(4, center, 320, 640, track_index=i) for i in range(3)]
        _spread_intra_family_boosts(moves)
        freqs = [m["target_hz"] for m in moves]
        # All within band (no clamping to edge)
        for f in freqs:
            assert 320 <= f <= 640
        # All distinct
        assert len(set(round(f, 1) for f in freqs)) == 3

    def test_build_eq_params_spread_distinct_freqs(self):
        """Spread moves fed through build_eq_params_for_moves produce distinct freq params."""
        # Minimal calibration: gain list with 0 dB at 0.5, freq empty (fallback log)
        cal = {
            "gain": [
                {"normalized": 0.0, "db": -24.0},
                {"normalized": 0.5, "db": 0.0},
                {"normalized": 1.0, "db": 24.0},
            ],
            "freq": [],
        }
        center = 550.0
        moves_a = [_make_boost_move(4, center, 320, 640, track_index=0)]
        moves_b = [_make_boost_move(4, center, 320, 640, track_index=1)]
        all_moves = moves_a + moves_b
        _spread_intra_family_boosts(all_moves)
        # Clean up _track_index before passing to build_eq_params
        for m in all_moves:
            m.pop("_track_index", None)
        params_a = build_eq_params_for_moves(moves_a, cal)
        params_b = build_eq_params_for_moves(moves_b, cal)
        # Extract freq params
        freq_a = [p["value"] for p in params_a if p["name"].startswith("Freq-")]
        freq_b = [p["value"] for p in params_b if p["name"].startswith("Freq-")]
        assert len(freq_a) == 1
        assert len(freq_b) == 1
        assert freq_a[0] != pytest.approx(freq_b[0], abs=0.001)


class TestPriorityOrdering:
    """Verify critical priority relationships after stack reorder."""

    def test_guitar_above_cymbal(self):
        assert match_track_priority("guitar") > match_track_priority("cymbal")
        assert match_track_priority("gtr") > match_track_priority("hihat")

    def test_piano_above_guitar(self):
        assert match_track_priority("piano") > match_track_priority("guitar")

    def test_keys_above_guitar(self):
        assert match_track_priority("keys") > match_track_priority("guitar")

    def test_synth_above_cymbal(self):
        assert match_track_priority("synth") > match_track_priority("cymbal")

    def test_cymbal_above_default(self):
        """All cymbal entries must be above the 0.5 unknown-name default."""
        default = match_track_priority("xyzunknown")
        assert match_track_priority("hihat") > default
        assert match_track_priority("crash") > default
        assert match_track_priority("cymbal") > default
        assert match_track_priority("ride") > default

    def test_unknown_name_default(self):
        assert match_track_priority("xyzunknown") == 0.5


# ---------------------------------------------------------------------------
# Energy-weighted selective makeup
# ---------------------------------------------------------------------------

def _make_10band_calibration():
    """Minimal calibration dict for 10-band (expanded) ReaEQ layout."""
    return {
        "band_names": [f"Band {i}" for i in range(10)],
        "gain": [(0.0, -24.0), (0.5, 0.0), (1.0, 24.0)],
    }


def _make_5band_calibration():
    """Minimal calibration dict for 5-band (default) ReaEQ layout."""
    return {
        "band_names": ["Low Shelf", "Band 2", "Band 3", "High Shelf 4", "High Pass 5"],
        "gain": [(0.0, -24.0), (0.5, 0.0), (1.0, 24.0)],
    }


class TestEnergyWeightedMakeup:
    """Tests for energy-weighted selective makeup gain."""

    def test_bass_no_high_freq_makeup(self):
        """Bass-shaped spectrum: energy in bands 0-3, silence in 6-9.
        Bands 6-9 should get zero makeup boost."""
        cal = _make_10band_calibration()
        # Bass-like: strong low end, drops off sharply
        bass_bands = _make_bands(_make_spectrum(
            [-20, -18, -22, -25, -40, -55, -70, -80, -90, -100]
        ))
        cut_indices = {2}  # cut on band 2
        boosts, applied, unapplied = _compute_selective_makeup_boosts(
            target_makeup_db=1.5,
            cut_reaeq_indices=cut_indices,
            calibration=cal,
            yield_bands=bass_bands,
        )
        # Bands 6-9 should have zero boost (no meaningful energy)
        for bi in range(6, 10):
            assert boosts.get(bi, 0.0) == 0.0, f"Band {bi} should get no makeup"
        # At least some low bands should get boost
        low_boost = sum(boosts.get(bi, 0.0) for bi in range(5))
        assert low_boost > 0, "Low-frequency bands should receive makeup"

    def test_full_range_track_all_bands(self):
        """Full-range spectrum: most uncut bands should get makeup."""
        cal = _make_10band_calibration()
        full_bands = _make_bands(_make_spectrum(
            [-20, -22, -21, -23, -24, -25, -26, -28, -30, -32]
        ))
        cut_indices = {3}
        boosts, applied, unapplied = _compute_selective_makeup_boosts(
            target_makeup_db=1.5,
            cut_reaeq_indices=cut_indices,
            calibration=cal,
            yield_bands=full_bands,
        )
        # Most uncut bands should get some boost
        boosted_count = sum(1 for bi in range(10) if bi != 3 and boosts.get(bi, 0.0) > 0)
        assert boosted_count >= 6, f"Expected >=6 boosted bands, got {boosted_count}"

    def test_no_yield_bands_uniform_fallback(self):
        """When yield_bands is None, all eligible bands should get equal weight."""
        cal = _make_10band_calibration()
        cut_indices = {0}
        boosts, applied, unapplied = _compute_selective_makeup_boosts(
            target_makeup_db=1.5,
            cut_reaeq_indices=cut_indices,
            calibration=cal,
            yield_bands=None,
        )
        # All 9 uncut bands should get boost (uniform)
        boosted = [bi for bi in range(10) if bi != 0 and boosts.get(bi, 0.0) > 0]
        assert len(boosted) == 9
        # Check uniformity: all boosts should be approximately equal
        vals = [boosts[bi] for bi in boosted]
        assert max(vals) - min(vals) < 0.01, "Uniform weights should produce equal boosts"

    def test_zero_importance_bands_excluded(self):
        """A band below the absolute floor should be excluded from makeup."""
        cal = _make_10band_calibration()
        # Band 9 is below MAKEUP_IMPORTANCE_FLOOR_DB
        bands = _make_bands(_make_spectrum(
            [-20, -22, -24, -26, -28, -30, -32, -34, -36, -70]
        ))
        cut_indices = {0}
        boosts, _, _ = _compute_selective_makeup_boosts(
            target_makeup_db=1.5,
            cut_reaeq_indices=cut_indices,
            calibration=cal,
            yield_bands=bands,
        )
        assert boosts.get(9, 0.0) == 0.0, "Band below floor should get no makeup"

    def test_all_zero_importance_no_boosts(self):
        """When all eligible bands have zero importance, return empty boosts."""
        cal = _make_10band_calibration()
        # All bands below floor except band 0 which is cut
        silent_bands = _make_bands(_make_spectrum(
            [-30] + [-120] * 9
        ))
        cut_indices = {0}  # cut the only energetic band
        boosts, applied, unapplied = _compute_selective_makeup_boosts(
            target_makeup_db=1.5,
            cut_reaeq_indices=cut_indices,
            calibration=cal,
            yield_bands=silent_bands,
        )
        assert boosts == {}
        assert applied == 0.0
        assert unapplied == 1.5

    def test_5band_fallback_weighting(self):
        """In 5-band mode, max importance across merged analysis bands should work."""
        cal = _make_5band_calibration()
        # Bands 0,1 map to ReaEQ 0; bands 2,3 to ReaEQ 1; etc.
        # Band 0 has energy, band 1 is silent → ReaEQ 0 should still be eligible (max)
        bands = _make_bands(_make_spectrum(
            [-20, -120, -25, -120, -28, -120, -120, -120, -120, -120]
        ))
        cut_indices = {2}  # cut ReaEQ band 2
        boosts, _, _ = _compute_selective_makeup_boosts(
            target_makeup_db=1.5,
            cut_reaeq_indices=cut_indices,
            calibration=cal,
            yield_bands=bands,
        )
        # ReaEQ 0 (maps from analysis 0+1) should get boost because analysis band 0 has energy
        assert boosts.get(0, 0.0) > 0, "5-band merged band should use max importance"
        # ReaEQ 3 (maps from analysis 6+7) should get zero — both silent
        assert boosts.get(3, 0.0) == 0.0, "Band with all-silent children should get no makeup"

    def test_per_band_cap_respected(self):
        """With few eligible bands and large target, per-band cap should limit."""
        cal = _make_10band_calibration()
        # Only bands 0-1 have energy
        bands = _make_bands(_make_spectrum(
            [-20, -22, -120, -120, -120, -120, -120, -120, -120, -120]
        ))
        cut_indices = {0}  # cut band 0, only band 1 eligible
        boosts, _, _ = _compute_selective_makeup_boosts(
            target_makeup_db=2.5,
            cut_reaeq_indices=cut_indices,
            calibration=cal,
            yield_bands=bands,
        )
        for bi, db in boosts.items():
            assert db <= MAKEUP_PER_BAND_CAP_DB + 1e-6, f"Band {bi} exceeds per-band cap"

    def test_pair_mode_passes_yield_bands(self):
        """build_reaeq_gain_moves with yield_bands should differ from without."""
        cal = _make_10band_calibration()
        bass_bands = _make_bands(_make_spectrum(
            [-20, -18, -22, -25, -40, -55, -70, -80, -90, -100]
        ))
        cuts = [{"band_index": 2, "cut_db": -3.0, "lo": 160, "hi": 320}]
        moves_with, _ = build_reaeq_gain_moves(
            cuts, cal, makeup_mode="auto", yield_bands=bass_bands,
        )
        moves_without, _ = build_reaeq_gain_moves(
            cuts, cal, makeup_mode="auto", yield_bands=None,
        )
        # With yield_bands, high bands should NOT have boosts
        high_with = sum(moves_with.get(bi, 0.0) for bi in range(6, 10) if moves_with.get(bi, 0.0) > 0)
        high_without = sum(moves_without.get(bi, 0.0) for bi in range(6, 10) if moves_without.get(bi, 0.0) > 0)
        assert high_with < high_without, "Energy-weighted should have less high-freq makeup"

    def test_sections_mode_makeup_uses_yield_bands(self):
        """compute_reaeq_makeup_profile with yield_bands should limit boosts."""
        cal = _make_10band_calibration()
        bass_bands = _make_bands(_make_spectrum(
            [-20, -18, -22, -25, -40, -55, -70, -80, -90, -100]
        ))
        cuts = [{"band_index": 2, "cut_db": -3.0, "lo": 160, "hi": 320}]
        profile = compute_reaeq_makeup_profile(
            cuts, cal, makeup_mode="auto", yield_bands=bass_bands,
        )
        boosts = profile["boosts_by_reaeq_band"]
        # High bands should get no boost
        for bi in range(7, 10):
            assert boosts.get(bi, 0.0) == 0.0, f"Band {bi} should get no makeup with bass spectrum"


class TestSpectralImportance:
    """Verify _compute_spectral_importance matches daemon constants."""

    def _make_bands(self, db_values):
        return [{"avg_db": v, "avg_db_l": v, "avg_db_r": v} for v in db_values]

    def test_at_peak(self):
        bands = self._make_bands([-30, -40, -50])
        weights = _compute_spectral_importance(bands)
        assert weights[0] == 1.0  # band 0 is the peak

    def test_at_gap_boundary(self):
        peak = -20.0
        at_gap = peak - PRIORITY_LANE_GAP_DB  # exactly 22 dB below peak
        bands = self._make_bands([peak, at_gap])
        weights = _compute_spectral_importance(bands)
        assert weights[0] == 1.0
        assert weights[1] == 0.0  # at gap boundary → 0

    def test_linear_ramp(self):
        peak = -20.0
        halfway = peak - PRIORITY_LANE_GAP_DB / 2  # 11 dB below peak
        bands = self._make_bands([peak, halfway])
        weights = _compute_spectral_importance(bands)
        assert weights[0] == 1.0
        assert abs(weights[1] - 0.5) < 0.01  # linear ramp → ~0.5

    def test_below_absolute_floor(self):
        bands = self._make_bands([-10, PRIORITY_LANE_FLOOR_DB - 1])
        weights = _compute_spectral_importance(bands)
        assert weights[0] == 1.0
        assert weights[1] == 0.0  # below -55 dB absolute → always 0
