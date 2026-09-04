"""Pure, FlexMeasures-free helpers for the labems-sc9 shadow scheduler.

Import-light (standard library only), so the window math, coverage, and reproducibility checks are unit-tested without the FlexMeasures app or a database.
shadow_schedule.py imports this module for every decision that does not touch the ORM.
"""

import math
import re
from datetime import datetime, timedelta, timezone

# Sources that must never feed a schedule: the inverter SOC lies, and the deye/bms/* group is zeros.
FORBIDDEN_TOPIC_PREFIXES = ("deye/battery/soc", "deye/bms/")


def is_forbidden(topic):
    """Return True for a topic in the forbidden deny-list (deye/battery/soc, deye/bms/*)."""
    return bool(topic) and any(
        topic == p or topic.startswith(p) for p in FORBIDDEN_TOPIC_PREFIXES
    )


def isoparse(text, field):
    """Parse a timezone-aware ISO datetime, raising a clear error on a naive value."""
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError(
            f"{field} must be timezone-aware (e.g. 2026-09-02T08:00:00+00:00): {text!r}"
        )
    return dt.astimezone(timezone.utc)


def floor_to(dt, resolution):
    """Floor a datetime to the resolution boundary at or before it (UTC)."""
    step = int(resolution.total_seconds())
    epoch = int(dt.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % step), tz=timezone.utc)


def iso_duration_to_timedelta(text):
    """Read a minimal ISO-8601 duration (PT15M, PT1H, PT24H) into a timedelta."""
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", text)
    if not m:
        raise ValueError(f"Unsupported duration: {text!r}")
    hours, minutes, seconds = (int(g) if g else 0 for g in m.groups())
    return timedelta(hours=hours, minutes=minutes, seconds=seconds)


def compute_window(mode, now, resolution, horizon, earliest=None, start_override=None):
    """Compute (start, end, expected_steps) for the schedule window.

    retrospective: end = floor(now); start = start_override, else max(end - horizon, earliest).
    The window is the observed past up to the horizon cap, so it is fully covered by real data and grows toward the full horizon as data accumulates.
    future: start = start_override, else floor(now); end = start + horizon.
    expected_steps is derived from the ACTUAL window, not a hardcoded horizon, so coverage is correct for a growing backcast.
    Raises ValueError on an empty window or, in retrospective mode with no override, when no observed data exists yet.
    """
    if mode == "retrospective":
        end = floor_to(now, resolution)
        if start_override is not None:
            # Fail closed: align the override to the resolution grid and never let it exceed the horizon.
            start = max(floor_to(start_override, resolution), end - horizon)
        elif earliest is not None:
            start = max(end - horizon, earliest)
        else:
            raise ValueError(
                "no observed inflexible data yet; refusing to schedule an empty window"
            )
    elif mode == "future":
        start = (
            start_override if start_override is not None else floor_to(now, resolution)
        )
        end = start + horizon
    else:
        raise ValueError(f"unknown window_mode {mode!r}")
    if end <= start:
        raise ValueError(
            f"empty window: start {start.isoformat()} >= end {end.isoformat()}"
        )
    expected_steps = int((end - start).total_seconds() // resolution.total_seconds())
    if expected_steps < 1:
        raise ValueError("window shorter than one resolution step")
    return start, end, expected_steps


def continuous_run_start(present_slots, end, resolution, max_gap_slots=2):
    """Return the earliest slot of the most recent unbroken run of data ending at end, or None.

    Walking back from end, the run continues while consecutive present slots are at most max_gap_slots resolutions apart.
    This excludes the sparse pre-t2t 3oh samples (separated from the continuous run by a large gap), so the retrospective window starts where continuous ingestion began, not at the oldest stray sample.
    """
    slots = sorted(s for s in present_slots if s <= end)
    if not slots:
        return None
    max_gap = resolution * max_gap_slots
    start = slots[-1]
    for i in range(len(slots) - 1, 0, -1):
        if slots[i] - slots[i - 1] <= max_gap:
            start = slots[i - 1]
        else:
            break
    return start


def derive_site_load(grid_by_slot, batt_by_slot):
    """Derive the real site load per slot: P_load[t] = grid[t] + batt[t].

    Uses each sensor's native sign: grid import is positive, battery discharge is positive.
    So a discharging battery (positive) that supplies the load leaves less to import, and load = grid + battery (import 540 + battery -78 while charging = 462 real load).
    Only slots where BOTH grid and battery are known are returned (intersection), so a battery dropout lowers derived-load coverage instead of silently assuming a zero battery contribution.
    Returns {slot: p_load} for every slot present in both grid_by_slot and batt_by_slot.
    """
    return {
        t: grid_by_slot[t] + batt_by_slot[t] for t in grid_by_slot if t in batt_by_slot
    }


def peak_metrics(grid_by_slot, load_by_slot, batt_sched_discharge_by_slot):
    """Quantify peak-shaving over the window, all in the same power unit (kW).

    grid_by_slot: observed grid import per slot (positive = import).
    load_by_slot: derived real site load per slot (P_load = grid + battery).
    batt_sched_discharge_by_slot: the scheduled battery power per slot in DISCHARGE-positive convention.
    The shadow grid is what the import would have been under the scheduled dispatch: shadow_grid[t] = load[t] - batt_sched_discharge[t], because discharging (positive) lowers import.
    peak_before and peak_after are compared over the slots for which the load is known.
    Returns peak_kw_before, peak_kw_after, kw_shaved, pct_shaved (all None when there is no data).
    """
    slots = [t for t in load_by_slot if t in grid_by_slot]
    if not slots:
        return {
            "peak_kw_before": None,
            "peak_kw_after": None,
            "kw_shaved": None,
            "pct_shaved": None,
        }
    peak_before = max(grid_by_slot[t] for t in slots)
    peak_after = max(
        load_by_slot[t] - batt_sched_discharge_by_slot.get(t, 0.0) for t in slots
    )
    kw_shaved = peak_before - peak_after
    pct_shaved = (100.0 * kw_shaved / peak_before) if peak_before else 0.0
    return {
        "peak_kw_before": peak_before,
        "peak_kw_after": peak_after,
        "kw_shaved": kw_shaved,
        "pct_shaved": pct_shaved,
    }


def covered_slots(event_starts, start, resolution, expected_steps):
    """Return the set of window slot indices covered by the given event_starts."""
    step = resolution.total_seconds()
    covered = set()
    for ev in event_starts:
        slot = int((ev - start).total_seconds() // step)
        if 0 <= slot < expected_steps:
            covered.add(slot)
    return covered


def coverage_fraction(event_starts, start, resolution, expected_steps):
    """Return the fraction of window slots covered by the given event_starts."""
    if not expected_steps:
        return 0.0
    return (
        len(covered_slots(event_starts, start, resolution, expected_steps))
        / expected_steps
    )


def build_flex_model(cfg):
    """Turn the declared battery parameters into a StorageScheduler flex-model.

    Capacity, start SOC, the SOC band, and the efficiencies are explicit parameters (see the config's assumptions), not measurements.
    """
    b = cfg["battery"]
    cap = float(b["capacity_kwh"])
    return {
        "soc-at-start": f"{cap * float(b['soc_at_start_pct']) / 100.0} kWh",
        "soc-min": f"{cap * float(b['soc_min_pct']) / 100.0} kWh",
        "soc-max": f"{cap * float(b['soc_max_pct']) / 100.0} kWh",
        "power-capacity": f"{float(b['power_capacity_kw'])} kW",
        "charging-efficiency": f"{float(b['charging_efficiency_pct'])}%",
        "discharging-efficiency": f"{float(b['discharging_efficiency_pct'])}%",
    }


def is_nan(value):
    return value is None or (isinstance(value, float) and math.isnan(value))


def check_reproducible(records1, records2, expected_steps):
    """Enforce that a reproducibility proof is a real proof.

    Returns (ok, reason).
    An empty, all-NaN, or wrong-length schedule is rejected, so two all-NaN runs never pass as equal.
    The length check uses the ACTUAL expected_steps of the window, not a fixed horizon.
    """
    if len(records1) != expected_steps:
        return False, f"schedule has {len(records1)} steps, expected {expected_steps}"
    if len(records1) != len(records2):
        return False, "the two runs produced different lengths"
    if all(is_nan(r["value"]) for r in records1):
        return False, "schedule is entirely NaN — not a proof"
    for a, b in zip(records1, records2):
        if a["event_start"] != b["event_start"]:
            return False, "event timestamps differ between runs"
        av, bv = a["value"], b["value"]
        if is_nan(av) and is_nan(bv):
            continue
        if is_nan(av) != is_nan(bv) or av != bv:
            return False, f"values differ at {a['event_start']}: {av} vs {bv}"
    return True, "identical"
