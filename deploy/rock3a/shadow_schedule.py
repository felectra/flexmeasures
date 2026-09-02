"""Shadow storage scheduler for the ROCK 3A pilot (labems-sc9, rubіж G2).

COMPUTE-ONLY by default.
This never sends a command and never publishes to the broker — it instantiates no MQTT client at all.
It asks FlexMeasures' StorageScheduler for a battery schedule on the live labems-3oh sensors,
records the inputs and the resulting schedule to a reproducible log, and — only with --commit —
stores the schedule as beliefs on a dedicated, strictly validated output sensor.
Storing is a write to FlexMeasures' own database only; nothing reaches the broker, the bridge, the
inverter, or the BMS boards.

belief_time defaults to now and is RECORDED in every run; pass --belief-time to pin it.
Reproducibility is proven by re-running with the SAME recorded belief_time: FlexMeasures then reads
exactly the same beliefs, and the deterministic HiGHS solver returns an identical schedule.
Run --prove-reproducible to compute twice on the same pinned inputs and ENFORCE that the two outputs
match (a non-proof — empty, all-NaN, or wrong length — exits non-zero).

Objective (versioned in shadow-g2-config.json): shadow peak-shaving of grid import
(deye/ac/total_grid_power, Office) expressed as a priced site-peak-consumption commitment.
The peak baseline and price, the bank capacity, the start SOC, and the efficiencies are DECLARED
PARAMETERS, not measurements — see shadow-g2.md.

Usage (read-only broker; writes the FlexMeasures DB only with --commit):
    podman cp deploy/rock3a/shadow_schedule.py rock3a_server_1:/tmp/shadow_schedule.py
    podman cp deploy/rock3a/shadow-g2-config.json rock3a_server_1:/tmp/shadow-g2-config.json
    podman exec -i rock3a_server_1 python /tmp/shadow_schedule.py --config /tmp/shadow-g2-config.json
"""

import argparse
import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone

# Never provision template assets on startup: that commits (app.py), and the compute path must not
# write anything to the database.
os.environ.setdefault("FLEXMEASURES_CREATE_TEMPLATE_ASSETS_ON_STARTUP", "false")

from flexmeasures.app import create as create_app  # noqa: E402

# Sources that must never feed a schedule: the inverter SOC lies, and the deye/bms/* group is zeros.
FORBIDDEN_TOPIC_PREFIXES = ("deye/battery/soc", "deye/bms/")
BATTERYBANK_ASSET_ID = 6


def is_forbidden(topic):
    return bool(topic) and any(
        topic == p or topic.startswith(p) for p in FORBIDDEN_TOPIC_PREFIXES
    )


def _isoparse(text: str, field: str) -> datetime:
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise SystemExit(
            f"{field} must be timezone-aware (e.g. 2026-09-02T08:00:00+00:00): {text!r}"
        )
    return dt.astimezone(timezone.utc)


def _floor_to(dt: datetime, resolution: timedelta) -> datetime:
    step = int(resolution.total_seconds())
    epoch = int(dt.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % step), tz=timezone.utc)


def _iso_duration_to_timedelta(text: str) -> timedelta:
    # Minimal ISO-8601 duration reader for the shapes we version (PT15M, PT1H, PT24H).
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", text)
    if not m:
        raise ValueError(f"Unsupported duration: {text!r}")
    hours, minutes, seconds = (int(g) if g else 0 for g in m.groups())
    return timedelta(hours=hours, minutes=minutes, seconds=seconds)


def build_flex_model(cfg: dict) -> dict:
    """Turn the declared battery parameters into a StorageScheduler flex-model.

    Capacity, start SOC, the SOC band, and the efficiencies are explicit parameters (see the config's
    assumptions), not measurements.
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


def resolve_inflexible_sensors(cfg, db, Sensor):
    """Resolve the flex-context inflexible sensors and enforce the input allowlist.

    Any sensor bound to a forbidden source (deye/battery/soc, deye/bms/*) is rejected, and the set of
    inflexible sensors must equal the configured expected ids (the Office grid sensor 7).
    """
    refs = cfg["flex_context"].get("inflexible-consumption", [])
    expected = set(cfg.get("expected_inflexible_sensor_ids", []))
    resolved, got_ids = [], set()
    for ref in refs:
        sid = ref["sensor"] if isinstance(ref, dict) else ref
        sensor = db.session.get(Sensor, sid)
        if sensor is None:
            raise SystemExit(f"Inflexible sensor {sid} not found.")
        topic = (sensor.attributes or {}).get("source_topic")
        if is_forbidden(topic):
            raise SystemExit(
                f"Refusing forbidden inflexible source {topic!r} on sensor {sid}."
            )
        resolved.append((sensor, topic))
        got_ids.add(sid)
    if expected and got_ids != expected:
        raise SystemExit(
            f"Inflexible sensors {sorted(got_ids)} do not match expected {sorted(expected)}."
        )
    return resolved


def asset_flex_context_chain(sensor):
    """Collect the DB-stored flex-context of the scheduled asset and its ancestors, for the log.

    A non-empty entry that references sensors would silently pull extra inputs into the schedule, so
    it is recorded for review.
    """
    chain, asset = [], sensor.generic_asset
    while asset is not None:
        chain.append(
            {
                "asset_id": asset.id,
                "asset_name": asset.name,
                "flex_context": dict(asset.flex_context or {}),
            }
        )
        asset = asset.parent_asset
    return chain


def snapshot_inputs(resolved, start, end, belief_time, resolution, expected_steps):
    """Record the inflexible-load inputs, both raw and resampled to the schedule resolution.

    Coverage is the fraction of PT15M slots that carry a real belief; the scheduler zero-fills the
    rest, so a low coverage means the schedule is dominated by invented zeros.
    """
    from flexmeasures.data.models.time_series import TimedBelief

    out, min_coverage = [], 1.0
    for sensor, topic in resolved:
        bdf = TimedBelief.search(
            sensors=sensor,
            event_starts_after=start,
            event_ends_before=end,
            beliefs_before=belief_time,
        )
        raw, covered = [], set()
        if len(bdf):
            df = bdf.reset_index()
            for _, row in df.iterrows():
                ev, val, src = row["event_start"], row["event_value"], row.get("source")
                raw.append(
                    {
                        "event_start": ev.isoformat(),
                        "belief_time": row["belief_time"].isoformat(),
                        "source_id": getattr(src, "id", None),
                        "value": None if val != val else float(val),
                    }
                )
                slot = int((ev - start).total_seconds() // resolution.total_seconds())
                if 0 <= slot < expected_steps:
                    covered.add(slot)
        resampled = []
        try:
            rbdf = TimedBelief.search(
                sensors=sensor,
                event_starts_after=start,
                event_ends_before=end,
                beliefs_before=belief_time,
                resolution=resolution,
                one_deterministic_belief_per_event=True,
            )
            if len(rbdf):
                rdf = rbdf.reset_index()
                for _, row in rdf.iterrows():
                    val = row["event_value"]
                    resampled.append(
                        {
                            "event_start": row["event_start"].isoformat(),
                            "value": None if val != val else float(val),
                            "source_id": getattr(row.get("source"), "id", None),
                        }
                    )
        except (
            Exception
        ) as exc:  # resampling is best-effort logging, not the schedule itself
            resampled = [{"error": f"{exc.__class__.__name__}: {exc}"}]
        coverage = len(covered) / expected_steps if expected_steps else 0.0
        min_coverage = min(min_coverage, coverage)
        out.append(
            {
                "sensor_id": sensor.id,
                "sensor_name": sensor.name,
                "source_topic": topic,
                "n_raw_beliefs": len(raw),
                "raw_beliefs": raw,
                "resampled_to_resolution": resampled,
                "covered_slots": len(covered),
                "expected_slots": expected_steps,
                "coverage": coverage,
                "note": "the scheduler zero-fills PT15M slots that carry no belief",
            }
        )
    return out, min_coverage


def compute_schedule_series(
    sensor, start, end, resolution, belief_time, flex_model, flex_context
):
    """Compute the schedule in memory and return its pandas Series — saving nothing.

    Uses the same scheduler instance FlexMeasures' make_schedule uses, but stops before any
    save_to_db, so no belief is ever written.
    """
    import pandas as pd
    from flexmeasures.data.models.planning.storage import StorageScheduler
    from flexmeasures.data.services.utils import get_scheduler_instance

    scheduler = get_scheduler_instance(
        scheduler_class=StorageScheduler,
        asset_or_sensor=sensor,
        scheduler_params=dict(
            start=start,
            end=end,
            resolution=resolution,
            belief_time=belief_time,
            flex_model=flex_model,
            flex_context=flex_context,
            return_multiple=True,
        ),
    )
    results = scheduler.compute()
    if isinstance(results, pd.Series):
        # A scheduler may return a bare power Series for a single device.
        return results
    for result in results:
        # The schedule is the result whose payload is a power Series tied to a sensor.
        # Analysis payloads (soft-constraint results, commitment costs) are dicts, so they are skipped.
        if "sensor" in result and isinstance(result.get("data"), pd.Series):
            return result["data"]
    raise RuntimeError("Scheduler returned no schedule series.")


def series_to_records(series):
    if series is None or len(series) == 0:
        return []
    return [
        {"event_start": ts.isoformat(), "value": (None if v != v else float(v))}
        for ts, v in series.items()
    ]


def _is_nan(value):
    return value is None or (isinstance(value, float) and math.isnan(value))


def check_reproducible(records1, records2, expected_steps):
    """Enforce that a reproducibility proof is a real proof.

    Returns (ok, reason).
    An empty, all-NaN, or wrong-length schedule is rejected, so two all-NaN runs never pass as equal.
    """
    if len(records1) != expected_steps:
        return False, f"schedule has {len(records1)} steps, expected {expected_steps}"
    if len(records1) != len(records2):
        return False, "the two runs produced different lengths"
    if all(_is_nan(r["value"]) for r in records1):
        return False, "schedule is entirely NaN — not a proof"
    for a, b in zip(records1, records2):
        if a["event_start"] != b["event_start"]:
            return False, "event timestamps differ between runs"
        av, bv = a["value"], b["value"]
        if _is_nan(av) and _is_nan(bv):
            continue
        if _is_nan(av) != _is_nan(bv) or av != bv:
            return False, f"values differ at {a['event_start']}: {av} vs {bv}"
    return True, "identical"


def validate_commit_target(sensor, cfg, resolution, coverage, min_coverage):
    """Refuse --commit unless the target is exactly the approved, marked PT15M output sensor.

    This makes it impossible for --commit to write a measured sensor (e.g. sensor 17).
    """
    tid = cfg.get("target_output_sensor_id")
    attrs = sensor.attributes or {}
    problems = []
    if tid is None:
        problems.append(
            "target_output_sensor_id is null (output sensor not created / not approved)"
        )
    elif sensor.id != tid:
        problems.append(
            f"target sensor {sensor.id} != config target_output_sensor_id {tid}"
        )
    if sensor.generic_asset_id != BATTERYBANK_ASSET_ID:
        problems.append(
            f"sensor asset {sensor.generic_asset_id} != BatteryBank {BATTERYBANK_ASSET_ID}"
        )
    if sensor.event_resolution != resolution:
        problems.append(f"sensor resolution {sensor.event_resolution} != {resolution}")
    if attrs.get("labems") != "sc9-shadow-schedule":
        problems.append("sensor missing labems=sc9-shadow-schedule marker")
    if attrs.get("source_topic") is not None:
        problems.append(
            f"sensor carries source_topic {attrs.get('source_topic')!r} — measured sensor, refusing"
        )
    if coverage < min_coverage:
        problems.append(
            f"input coverage {coverage:.3f} < min {min_coverage} (insufficient_input)"
        )
    if problems:
        raise SystemExit("Refusing --commit:\n  - " + "\n  - ".join(problems))


def main():  # noqa: C901
    parser = argparse.ArgumentParser(
        description="labems-sc9 shadow storage scheduler (compute-only by default)."
    )
    parser.add_argument(
        "--config", required=True, help="Path to shadow-g2-config.json."
    )
    parser.add_argument(
        "--start",
        help="ISO start of the schedule window (default: the resolution boundary at or before now, UTC).",
    )
    parser.add_argument(
        "--belief-time",
        help="ISO belief_time to pin inputs (default: now, UTC; must be timezone-aware).",
    )
    parser.add_argument(
        "--target-sensor",
        type=int,
        help="Override the sensor to schedule (validation only; forbidden with --commit).",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Persist the schedule to the approved output sensor (writes the FM DB). Off by default.",
    )
    parser.add_argument(
        "--prove-reproducible",
        action="store_true",
        help="Compute twice on the pinned inputs and ENFORCE identical, non-empty output.",
    )
    parser.add_argument(
        "--code-sha",
        help="Record the repo commit the code came from (the container has no git).",
    )
    parser.add_argument(
        "--log", default="/tmp/shadow-g2-log.jsonl", help="Append-only run log path."
    )
    args = parser.parse_args()

    if args.commit and args.target_sensor is not None:
        raise SystemExit(
            "--commit cannot be combined with --target-sensor: commit writes only the configured output sensor."
        )

    with open(args.config) as f:
        config_text = f.read()
    cfg = json.loads(config_text)
    config_version = cfg.get("version", "unversioned")
    config_sha256 = hashlib.sha256(config_text.encode()).hexdigest()

    resolution = _iso_duration_to_timedelta(cfg["resolution"])
    horizon = timedelta(hours=float(cfg["horizon_hours"]))
    expected_steps = int(horizon.total_seconds() // resolution.total_seconds())
    min_coverage = float(cfg.get("min_input_coverage", 0.9))

    app = create_app()
    with app.app_context():
        import flexmeasures
        from flexmeasures.data import db
        from flexmeasures.data.models.time_series import Sensor
        from flexmeasures.data.models.planning.storage import StorageScheduler

        now = datetime.now(tz=timezone.utc)
        belief_time = (
            _isoparse(args.belief_time, "--belief-time") if args.belief_time else now
        )
        start = (
            _isoparse(args.start, "--start")
            if args.start
            else _floor_to(now, resolution)
        )
        end = start + horizon

        target_id = (
            args.target_sensor
            or cfg.get("target_output_sensor_id")
            or cfg.get("scheduling_sensor_id_for_validation")
        )
        if target_id is None:
            raise SystemExit(
                "No target sensor: set target_output_sensor_id (once the output sensor exists) or pass --target-sensor."
            )
        sensor = db.session.get(Sensor, target_id)
        if sensor is None:
            raise SystemExit(f"Target sensor {target_id} not found.")

        flex_model = build_flex_model(cfg)
        flex_context = cfg["flex_context"]

        resolved = resolve_inflexible_sensors(cfg, db, Sensor)
        inputs, coverage = snapshot_inputs(
            resolved, start, end, belief_time, resolution, expected_steps
        )
        inherited_flex_context = asset_flex_context_chain(sensor)

        series = compute_schedule_series(
            sensor, start, end, resolution, belief_time, flex_model, flex_context
        )
        records = series_to_records(series)

        reproducible_ok, reproducible_reason = None, None
        if args.prove_reproducible:
            records2 = series_to_records(
                compute_schedule_series(
                    sensor,
                    start,
                    end,
                    resolution,
                    belief_time,
                    flex_model,
                    flex_context,
                )
            )
            reproducible_ok, reproducible_reason = check_reproducible(
                records, records2, expected_steps
            )

        insufficient_input = coverage < min_coverage

        # Commit path: strictly validated, outcome recorded honestly.
        commit_attempted = commit_succeeded = False
        commit_error = None
        if args.commit:
            validate_commit_target(sensor, cfg, resolution, coverage, min_coverage)
            from flexmeasures.data.services.scheduling import make_schedule
            from flexmeasures.data.services.utils import get_asset_or_sensor_ref

            commit_attempted = True
            try:
                make_schedule(
                    asset_or_sensor=get_asset_or_sensor_ref(sensor),
                    start=start,
                    end=end,
                    resolution=resolution,
                    belief_time=belief_time,
                    flex_model=flex_model,
                    flex_context=flex_context,
                    scheduler_specs={
                        "module": "flexmeasures.data.models.planning.storage",
                        "class": "StorageScheduler",
                    },
                    dry_run=False,
                )
                commit_succeeded = True
            except Exception as exc:
                commit_error = f"{exc.__class__.__name__}: {exc}"

        run = {
            "run_utc": now.isoformat(),
            "bead": "labems-sc9",
            "objective": cfg.get("objective"),
            "config_version": config_version,
            "config_sha256": config_sha256,
            "window": {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "resolution": cfg["resolution"],
                "steps": expected_steps,
            },
            "belief_time": belief_time.isoformat(),
            "target_sensor_id": target_id,
            "flex_model": flex_model,
            "flex_context": flex_context,
            "inherited_asset_flex_context": inherited_flex_context,
            "inputs": inputs,
            "input_coverage": coverage,
            "min_input_coverage": min_coverage,
            "insufficient_input": insufficient_input,
            "schedule": records,
            "solver_declared": cfg.get("solver"),
            "solver_actual": app.config.get("FLEXMEASURES_LP_SOLVER"),
            "code_version": f"{flexmeasures.__version__}; StorageScheduler v{StorageScheduler.__version__}",
            "code_sha": args.code_sha,
            "broker_publishes": 0,
            "hardware_commands": 0,
            "commit_attempted": commit_attempted,
            "commit_succeeded": commit_succeeded,
            "committed": commit_succeeded,
            "commit_error": commit_error,
        }
        if args.prove_reproducible:
            run["reproducible"] = reproducible_ok
            run["reproducible_reason"] = reproducible_reason

        # On the compute-only path, discard any pending (uncommitted) session state as a safety net.
        if not commit_succeeded:
            db.session.rollback()

        with open(args.log, "a") as logf:
            logf.write(json.dumps(run) + "\n")

        nonzero = [r for r in records if not _is_nan(r["value"]) and r["value"] != 0.0]
        print(
            f"run_utc={now.isoformat()} config={config_version} sha256={config_sha256[:12]} "
            f"target_sensor={target_id} window={start.isoformat()}..{end.isoformat()} "
            f"belief_time={belief_time.isoformat()} schedule_steps={len(records)} "
            f"coverage={coverage:.3f} insufficient_input={insufficient_input} "
            f"solver={app.config.get('FLEXMEASURES_LP_SOLVER')} "
            f"commit_attempted={commit_attempted} committed={commit_succeeded} "
            f"broker_publishes=0 hardware_commands=0"
        )
        if commit_error:
            print(f"commit_error={commit_error}")
        if args.prove_reproducible:
            print(f"reproducible={reproducible_ok} ({reproducible_reason})")
        print(f"nonzero_schedule_steps={len(nonzero)} of {len(records)}")
        for r in records[:6]:
            print(f"  {r['event_start']}  {r['value']}")

        if args.prove_reproducible and not reproducible_ok:
            sys.exit(1)


if __name__ == "__main__":
    main()
