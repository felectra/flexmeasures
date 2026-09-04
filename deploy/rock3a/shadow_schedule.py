"""Shadow storage scheduler for the ROCK 3A pilot (labems-sc9, rubіж G2).

COMPUTE-ONLY by default.
This never sends a command and never publishes to the broker — it instantiates no MQTT client at all.
It quantifies peak-shaving on the DERIVED real site load over a RETROSPECTIVE (past) window, records the inputs, the schedule, and the value metric to a reproducible log, and — only with --commit — persists the derived load and the battery schedule to dedicated, strictly validated sensors.
Storing is a write to FlexMeasures' own database only; nothing reaches the broker, the bridge, the inverter, or the BMS boards.

Why the derived load: scheduling peak-shaving against the behind-meter deye/ac/total_grid_power is circular (the net grid already nets the battery), which gives coverage ~0.01 and a near-trivial schedule.
The honest inflexible load is P_load = P_grid + P_batt (grid import positive; battery discharge positive), computed from deye/ac/total_grid_power (7) and deye/battery/power (17); PV is ~0 at this bench.
The schedule then shaves the real grid-import peak by dispatching the battery, and the value metric reports the peak reduction — see shadow-g2.md.

Retrospective (backcast) window: there is no future load forecast, so the schedule is computed over the observed past — end = floor(now), start = the start of the continuous t2t run within the horizon (not the sparse pre-t2t 3oh samples).
It is a PERFECT-HINDSIGHT upper bound: the shadow knows the whole window's load, so the reported peak reduction is potential, not what a real-time EMS without a forecast could guarantee.

belief_time defaults to now (all past data visible) and is RECORDED; pass --belief-time to pin it.
Run --prove-reproducible to compute twice on the same window and ENFORCE identical, non-empty output (the length check uses the ACTUAL window steps).

Usage (read-only broker; writes the FM DB only with --commit):
    podman cp deploy/rock3a/sc9_core.py rock3a_server_1:/tmp/sc9_core.py
    podman cp deploy/rock3a/shadow_schedule.py rock3a_server_1:/tmp/shadow_schedule.py
    podman cp deploy/rock3a/shadow-g2-config.json rock3a_server_1:/tmp/shadow-g2-config.json
    podman exec -i rock3a_server_1 python /tmp/shadow_schedule.py --config /tmp/shadow-g2-config.json
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone

# Never provision template assets on startup: that commits (app.py), and the compute path must not
# write anything to the database.
# Force it off (not setdefault), so an inherited env cannot re-enable the committing provisioner.
os.environ["FLEXMEASURES_CREATE_TEMPLATE_ASSETS_ON_STARTUP"] = "false"

import sc9_core  # noqa: E402
from flexmeasures.app import create as create_app  # noqa: E402

OFFICE_ASSET_ID = 5
BATTERYBANK_ASSET_ID = 6
GRID_SENSOR_ID = 7  # deye/ac/total_grid_power, W, positive = import
BATT_SENSOR_ID = 17  # deye/battery/power, W, positive = discharge
DERIVED_SOURCE_NAME = "sc9-derived"
SCHEDULE_SOURCE_NAME = "sc9-schedule"


def _to_utc(dt):
    """Normalize a (pandas or python) datetime to a timezone-aware UTC python datetime."""
    if hasattr(dt, "to_pydatetime"):
        dt = dt.to_pydatetime()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def resample_by_slot(sensor, start, end, belief_time, resolution, scale=1.0):
    """Return {utc_slot_start: value * scale} for the sensor over [start, end] as of belief_time.

    One deterministic belief per slot, resampled to resolution; NaN slots are dropped.
    """
    from flexmeasures.data.models.time_series import TimedBelief

    out = {}
    bdf = TimedBelief.search(
        sensors=sensor,
        event_starts_after=start,
        event_ends_before=end,
        beliefs_before=belief_time,
        resolution=resolution,
        one_deterministic_belief_per_event=True,
    )
    if len(bdf):
        df = bdf.reset_index()
        for _, row in df.iterrows():
            val = row["event_value"]
            if val == val:  # not NaN
                out[_to_utc(row["event_start"])] = float(val) * scale
    return out


def get_or_create_source(db, DataSource, name, type_):
    """Get (or create, uncommitted) a dedicated DataSource by (name, type) — never the mqtt-ingest one."""
    src = db.session.query(DataSource).filter_by(name=name, type=type_).first()
    if src is None:
        src = DataSource(name=name, type=type_)
        db.session.add(src)
        db.session.flush()
    return src


def check_not_forbidden(sensor, role):
    """Refuse a sensor bound to a forbidden source (deye/battery/soc, deye/bms/*)."""
    topic = (sensor.attributes or {}).get("source_topic")
    if sc9_core.is_forbidden(topic):
        raise SystemExit(
            f"Refusing forbidden {role} source {topic!r} on sensor {sensor.id}."
        )


def asset_flex_context_chain(sensor):
    """Collect the DB-stored flex-context of the scheduled asset and its ancestors, for the log."""
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


def coverage_over_window(sensor, start, end, belief_time, resolution, expected_steps):
    """Fraction of window slots for which the sensor has a real belief (the scheduler zero-fills the rest)."""
    slots = resample_by_slot(sensor, start, end, belief_time, resolution)
    covered = sc9_core.covered_slots(list(slots), start, resolution, expected_steps)
    return len(covered) / expected_steps if expected_steps else 0.0


def compute_schedule_series(
    sensor, start, end, resolution, belief_time, flex_model, flex_context
):
    """Compute the schedule in memory and return its pandas Series — saving nothing."""
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
        return results
    for result in results:
        if "sensor" in result and isinstance(result.get("data"), pd.Series):
            return result["data"]
    raise RuntimeError("Scheduler returned no schedule series.")


def series_to_records(series):
    if series is None or len(series) == 0:
        return []
    return [
        {
            "event_start": _to_utc(ts).isoformat(),
            "value": (None if v != v else float(v)),
        }
        for ts, v in series.items()
    ]


def validate_commit_target(sensor, cfg, resolution, coverage, min_coverage):
    """Refuse --commit unless the target is exactly the approved, marked PT15M output sensor."""
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
        description="labems-sc9 shadow storage scheduler (retrospective derived-load peak-shaving)."
    )
    parser.add_argument(
        "--config", required=True, help="Path to shadow-g2-config.json."
    )
    parser.add_argument(
        "--start", help="ISO start override for the window (default: derived)."
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
        help="Persist the derived load and the schedule (writes the FM DB). Off by default.",
    )
    parser.add_argument(
        "--prove-reproducible",
        action="store_true",
        help="Compute twice on the same window and ENFORCE identical, non-empty output.",
    )
    parser.add_argument("--code-sha", help="Record the repo commit the code came from.")
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

    resolution = sc9_core.iso_duration_to_timedelta(cfg["resolution"])
    horizon = timedelta(hours=float(cfg["horizon_hours"]))
    min_coverage = float(cfg.get("min_input_coverage", 0.9))
    window_mode = cfg.get("window_mode", "retrospective")

    app = create_app()
    with app.app_context():
        import flexmeasures
        from flexmeasures.data import db
        from flexmeasures.data.models.data_sources import DataSource
        from flexmeasures.data.models.time_series import Sensor, TimedBelief
        from flexmeasures.data.models.planning.storage import StorageScheduler

        now = datetime.now(tz=timezone.utc)
        try:
            belief_time = (
                sc9_core.isoparse(args.belief_time, "--belief-time")
                if args.belief_time
                else now
            )
            start_override = (
                sc9_core.isoparse(args.start, "--start") if args.start else None
            )
        except ValueError as exc:
            raise SystemExit(str(exc))

        site_load_id = cfg.get("site_load_sensor_id")
        if site_load_id is None:
            raise SystemExit(
                "site_load_sensor_id is null: run seed-site-load-sensor.py (approved), then set it in the config."
            )
        site_load_sensor = db.session.get(Sensor, site_load_id)
        grid_sensor = db.session.get(Sensor, GRID_SENSOR_ID)
        batt_sensor = db.session.get(Sensor, BATT_SENSOR_ID)
        if site_load_sensor is None or grid_sensor is None or batt_sensor is None:
            raise SystemExit("site-load, grid, or battery sensor not found.")
        # Deny-list allowlist: neither the inflexible input nor the derivation inputs may be forbidden.
        check_not_forbidden(site_load_sensor, "inflexible")
        check_not_forbidden(grid_sensor, "grid")
        check_not_forbidden(batt_sensor, "battery")
        if (site_load_sensor.attributes or {}).get("source_topic") is not None:
            raise SystemExit(
                "site-load sensor unexpectedly carries a source_topic; refusing."
            )
        # Pin the sensor identities and units, so a mis-set config cannot write onto the wrong sensor
        # or feed the W->kW scale a non-W input.
        if (
            site_load_sensor.generic_asset_id != OFFICE_ASSET_ID
            or (site_load_sensor.attributes or {}).get("labems") != "sc9-site-load"
        ):
            raise SystemExit(
                "site-load sensor is not the approved Office sc9-site-load sensor; refusing."
            )
        if site_load_sensor.event_resolution != resolution:
            raise SystemExit(
                f"site-load sensor resolution {site_load_sensor.event_resolution} != {resolution}."
            )
        if site_load_sensor.unit != "kW":
            raise SystemExit(
                f"site-load sensor unit {site_load_sensor.unit!r} != 'kW'."
            )
        if grid_sensor.unit != "W" or batt_sensor.unit != "W":
            raise SystemExit(
                f"grid/battery unit not W (got {grid_sensor.unit!r}/{batt_sensor.unit!r}); the W->kW scale would be wrong."
            )

        end = sc9_core.floor_to(now, resolution)
        earliest = None
        if window_mode == "retrospective" and start_override is None:
            grid_lookback = resample_by_slot(
                grid_sensor, end - horizon, end, belief_time, resolution
            )
            earliest = sc9_core.continuous_run_start(
                list(grid_lookback), end, resolution
            )
        try:
            start, end, expected_steps = sc9_core.compute_window(
                window_mode, now, resolution, horizon, earliest, start_override
            )
        except ValueError as exc:
            raise SystemExit(f"cannot form schedule window: {exc}")

        target_id = (
            args.target_sensor
            or cfg.get("target_output_sensor_id")
            or cfg.get("scheduling_sensor_id_for_validation")
        )
        if target_id is None:
            raise SystemExit("No target sensor configured.")
        target_sensor = db.session.get(Sensor, target_id)
        if target_sensor is None:
            raise SystemExit(f"Target sensor {target_id} not found.")

        # Derive the real site load (kW) from grid (import +) and battery (discharge +), both in W.
        grid_by_slot = resample_by_slot(
            grid_sensor, start, end, belief_time, resolution, scale=0.001
        )
        batt_by_slot = resample_by_slot(
            batt_sensor, start, end, belief_time, resolution, scale=0.001
        )
        p_load = sc9_core.derive_site_load(grid_by_slot, batt_by_slot)

        # Write the derived load onto the site-load sensor within the session, so the scheduler reads it;
        # flush makes it visible in-session. It is committed only under --commit, else rolled back.
        derived_source = get_or_create_source(
            db, DataSource, DERIVED_SOURCE_NAME, "derived"
        )
        db.session.add_all(
            [
                TimedBelief(
                    sensor=site_load_sensor,
                    source=derived_source,
                    event_start=slot,
                    belief_time=belief_time,
                    event_value=value,
                )
                for slot, value in p_load.items()
            ]
        )
        db.session.flush()

        flex_model = sc9_core.build_flex_model(cfg)
        flex_context = dict(cfg["flex_context"])
        flex_context["inflexible-consumption"] = [{"sensor": site_load_id}]

        coverage = coverage_over_window(
            site_load_sensor, start, end, belief_time, resolution, expected_steps
        )
        inherited_flex_context = asset_flex_context_chain(target_sensor)

        series = compute_schedule_series(
            target_sensor, start, end, resolution, belief_time, flex_model, flex_context
        )
        records = series_to_records(series)

        # The scheduler output is consumption-positive (charging +); the value metric needs discharge-positive.
        batt_sched_discharge = {
            _to_utc(datetime.fromisoformat(r["event_start"])): -r["value"]
            for r in records
            if r["value"] is not None
        }
        metric = sc9_core.peak_metrics(grid_by_slot, p_load, batt_sched_discharge)

        reproducible_ok, reproducible_reason = None, None
        if args.prove_reproducible:
            records2 = series_to_records(
                compute_schedule_series(
                    target_sensor,
                    start,
                    end,
                    resolution,
                    belief_time,
                    flex_model,
                    flex_context,
                )
            )
            reproducible_ok, reproducible_reason = sc9_core.check_reproducible(
                records, records2, expected_steps
            )

        insufficient_input = coverage < min_coverage

        commit_attempted = commit_succeeded = False
        commit_error = None
        if args.commit and args.prove_reproducible and not reproducible_ok:
            commit_error = f"reproducibility proof failed ({reproducible_reason}); refusing to commit"
        elif args.commit:
            validate_commit_target(
                target_sensor, cfg, resolution, coverage, min_coverage
            )
            commit_attempted = True
            try:
                # Persist the derived load AND the schedule in ONE transaction, so a failure leaves
                # neither behind (atomic).
                # The schedule is stored from the already-computed series (consumption-positive, in the
                # output sensor's unit); the output sensor is consumption_is_positive, so the value is
                # stored as-is.
                # This deliberately avoids make_schedule's separate commit (which would break atomicity
                # with the derived-load write) and its persist_flex_model side effect (writing BatteryBank
                # SOC state) — see shadow-g2.md.
                sched_source = get_or_create_source(
                    db, DataSource, SCHEDULE_SOURCE_NAME, "scheduler"
                )
                db.session.add_all(
                    [
                        TimedBelief(
                            sensor=target_sensor,
                            source=sched_source,
                            event_start=_to_utc(ts),
                            belief_time=belief_time,
                            event_value=value,
                        )
                        for ts, value in series.items()
                        if value == value  # skip NaN
                    ]
                )
                db.session.commit()  # derived load (flushed) + schedule, atomically
                commit_succeeded = True
            except Exception as exc:
                db.session.rollback()
                commit_error = f"{exc.__class__.__name__}: {exc}"

        if not commit_succeeded:
            db.session.rollback()  # discard the flushed derived load — nothing persists

        run = {
            "run_utc": now.isoformat(),
            "bead": "labems-sc9",
            "objective": cfg.get("objective"),
            "window_mode": window_mode,
            "config_version": config_version,
            "config_sha256": config_sha256,
            "window": {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "resolution": cfg["resolution"],
                "steps": expected_steps,
            },
            "belief_time": belief_time.isoformat(),
            "site_load_sensor_id": site_load_id,
            "target_sensor_id": target_id,
            "flex_model": flex_model,
            "flex_context": flex_context,
            "inherited_asset_flex_context": inherited_flex_context,
            "input_coverage": coverage,
            "min_input_coverage": min_coverage,
            "insufficient_input": insufficient_input,
            "n_load_slots": len(p_load),
            "peak_shaving": metric,
            "schedule": records,
            "solver_declared": cfg.get("solver"),
            "solver_actual": app.config.get("FLEXMEASURES_LP_SOLVER"),
            "code_version": f"{flexmeasures.__version__}; StorageScheduler v{StorageScheduler.__version__}",
            "code_sha": args.code_sha,
            "perfect_hindsight_upper_bound": True,
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

        with open(args.log, "a") as logf:
            logf.write(json.dumps(run) + "\n")

        print(
            f"run_utc={now.isoformat()} config={config_version} sha256={config_sha256[:12]} "
            f"mode={window_mode} target_sensor={target_id} site_load_sensor={site_load_id} "
            f"window={start.isoformat()}..{end.isoformat()} steps={expected_steps} "
            f"belief_time={belief_time.isoformat()} load_slots={len(p_load)} "
            f"coverage={coverage:.3f} insufficient_input={insufficient_input} "
            f"peak_before={metric['peak_kw_before']} peak_after={metric['peak_kw_after']} "
            f"kw_shaved={metric['kw_shaved']} pct_shaved={metric['pct_shaved']} "
            f"commit_attempted={commit_attempted} committed={commit_succeeded} "
            f"broker_publishes=0 hardware_commands=0"
        )
        if commit_error:
            print(f"commit_error={commit_error}")
        if args.prove_reproducible:
            print(f"reproducible={reproducible_ok} ({reproducible_reason})")

        if args.prove_reproducible and not reproducible_ok:
            sys.exit(1)


if __name__ == "__main__":
    main()
