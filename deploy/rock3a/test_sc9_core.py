"""Self-contained regression tests for the labems-sc9 pure logic (sc9_core).

These import only sc9_core (standard-library only), so they run without the FlexMeasures app or a database.
Run them with `pytest deploy/rock3a/test_sc9_core.py`.
They cover the retrospective window math, the continuous-run start rule, the P_load derivation and sign, the peak-shaving value metric, and the reproducibility check over the actual window.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sc9_core  # noqa: E402

UTC = timezone.utc
RES = timedelta(minutes=15)
HORIZON = timedelta(hours=24)
NOW = datetime(2026, 9, 4, 12, 7, 30, tzinfo=UTC)
END = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)  # floor(NOW, 15min)


# --- retrospective / future window math ---


def test_floor_to():
    assert sc9_core.floor_to(NOW, RES) == END


def test_compute_window_retrospective_caps_start_to_earliest():
    earliest = END - timedelta(hours=3)  # only 3 h of continuous data
    start, end, steps = sc9_core.compute_window(
        "retrospective", NOW, RES, HORIZON, earliest=earliest
    )
    assert end == END
    assert start == earliest  # capped to the earliest datum, not end - horizon
    assert steps == 12  # (3 h) / 15 min


def test_compute_window_retrospective_caps_to_horizon():
    earliest = END - timedelta(hours=48)  # more data than the horizon
    start, end, steps = sc9_core.compute_window(
        "retrospective", NOW, RES, HORIZON, earliest=earliest
    )
    assert start == END - HORIZON  # horizon cap wins
    assert steps == 96  # 24 h / 15 min


def test_compute_window_retrospective_no_data_raises():
    with pytest.raises(ValueError):
        sc9_core.compute_window("retrospective", NOW, RES, HORIZON, earliest=None)


def test_compute_window_future():
    start, end, steps = sc9_core.compute_window("future", NOW, RES, HORIZON)
    assert start == END and end == END + HORIZON and steps == 96


def test_compute_window_override_capped_to_horizon_and_aligned():
    # An override older than the horizon is capped to end - horizon (fail closed).
    start, end, steps = sc9_core.compute_window(
        "retrospective", NOW, RES, HORIZON, start_override=END - timedelta(hours=48)
    )
    assert start == END - HORIZON and steps == 96
    # An off-grid override is floored to the resolution boundary.
    start2, _, _ = sc9_core.compute_window(
        "retrospective",
        NOW,
        RES,
        HORIZON,
        start_override=END - timedelta(hours=3, minutes=7),
    )
    assert start2 == END - timedelta(hours=3, minutes=15)  # floored to the 15-min grid


# --- continuous-run start rule (exclude sparse pre-t2t samples) ---


def test_continuous_run_start_excludes_sparse_old_samples():
    old = [END - timedelta(days=3), END - timedelta(days=3) + RES]  # sparse 3oh samples
    recent = [END - i * RES for i in range(8)]  # last 2 h, contiguous
    start = sc9_core.continuous_run_start(old + recent, END, RES)
    assert start == min(recent)  # the continuous run, not the stray old samples


def test_continuous_run_start_none_when_empty():
    assert sc9_core.continuous_run_start([], END, RES) is None


# --- P_load derivation and sign ---


def test_derive_site_load_sign():
    load = sc9_core.derive_site_load(
        {END: 0.540}, {END: -0.078}
    )  # 540 W import, 78 W charging
    assert abs(load[END] - 0.462) < 1e-9  # 540 - 78 = 462 W real load


def test_derive_site_load_requires_both_inputs():
    load = sc9_core.derive_site_load({END: 1.0, END + RES: 2.0}, {END: 0.5})
    assert END in load and load[END] == 1.5
    assert (
        END + RES
    ) not in load  # a slot with no battery datum is excluded, not zeroed


# --- peak-shaving value metric ---


def test_peak_metrics_discharge_shaves_peak():
    t0, t1 = END, END + RES
    grid = {t0: 1.0, t1: 2.0}
    load = {t0: 1.0, t1: 2.0}
    batt_sched_discharge = {t0: 0.0, t1: 0.5}  # discharge 0.5 kW at the peak
    m = sc9_core.peak_metrics(grid, load, batt_sched_discharge)
    assert m["peak_kw_before"] == 2.0
    assert m["peak_kw_after"] == 1.5  # 2.0 - 0.5
    assert m["kw_shaved"] == 0.5
    assert abs(m["pct_shaved"] - 25.0) < 1e-9


def test_peak_metrics_empty_is_none():
    assert sc9_core.peak_metrics({}, {}, {})["peak_kw_before"] is None


# --- coverage and reproducibility over the actual window ---


def test_coverage_full_when_window_covered():
    slots = [END + i * RES for i in range(4)]
    assert sc9_core.coverage_fraction(slots, END, RES, 4) == 1.0


def test_check_reproducible_uses_actual_expected_steps():
    recs = [{"event_start": "t", "value": 1.0}]
    ok, _ = sc9_core.check_reproducible(recs, recs, 1)  # actual window steps
    assert ok is True
    wrong, _ = sc9_core.check_reproducible(
        recs, recs, 96
    )  # a hardcoded 96 would be wrong here
    assert wrong is False


def test_check_reproducible_rejects_all_nan():
    recs = [{"event_start": "t", "value": None}]
    ok, _ = sc9_core.check_reproducible(recs, recs, 1)
    assert ok is False
