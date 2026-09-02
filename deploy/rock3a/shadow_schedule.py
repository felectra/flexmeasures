"""Shadow storage scheduler for the ROCK 3A pilot (labems-sc9, rubіж G2).

COMPUTE-ONLY.
This never sends a command and never publishes to the broker — it instantiates no MQTT client at all.
It asks FlexMeasures' StorageScheduler for a battery schedule on the live labems-3oh sensors,
records the inputs and the resulting schedule to a reproducible log, and — only with --commit —
stores the schedule as beliefs on a dedicated output sensor.
Storing is a write to FlexMeasures' own database only; nothing reaches the broker, the bridge, the
inverter, or the BMS boards.

Reproducibility: belief_time is pinned explicitly, so a re-run over the same window with the same
belief_time reads exactly the same inputs and, with the deterministic HiGHS solver, produces an
identical schedule.
Run --prove-reproducible to compute twice on the pinned inputs and assert the two outputs match.

Objective (versioned in shadow-g2-config.json): shadow peak-shaving of grid import
(deye/ac/total_grid_power, Office) expressed as a priced site-peak-consumption commitment.
The peak baseline and price, the bank capacity, and the start SOC are DECLARED PARAMETERS, not
measurements — see shadow-g2.md.

Usage (read-only broker; writes the FlexMeasures DB only with --commit):
    podman cp deploy/rock3a/shadow_schedule.py rock3a_server_1:/tmp/shadow_schedule.py
    podman cp deploy/rock3a/shadow-g2-config.json rock3a_server_1:/tmp/shadow-g2-config.json
    podman exec -i rock3a_server_1 python /tmp/shadow_schedule.py --config /tmp/shadow-g2-config.json
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone

from flexmeasures.app import create as create_app


def _isoparse(text: str) -> datetime:
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _floor_to(dt: datetime, resolution: timedelta) -> datetime:
    step = int(resolution.total_seconds())
    epoch = int(dt.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % step), tz=timezone.utc)


def _iso_duration_to_timedelta(text: str) -> timedelta:
    # Minimal ISO-8601 duration reader for the shapes we version (PT15M, PT1H, PT24H).
    import re

    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", text)
    if not m:
        raise ValueError(f"Unsupported duration: {text!r}")
    hours, minutes, seconds = (int(g) if g else 0 for g in m.groups())
    return timedelta(hours=hours, minutes=minutes, seconds=seconds)


def build_flex_model(cfg: dict) -> dict:
    """Turn the declared battery parameters into a StorageScheduler flex-model.

    Capacity, start SOC, and the SOC band are explicit parameters (see the config's assumptions),
    not measurements.
    """
    b = cfg["battery"]
    cap = float(b["capacity_kwh"])
    return {
        "soc-at-start": f"{cap * float(b['soc_at_start_pct']) / 100.0} kWh",
        "soc-min": f"{cap * float(b['soc_min_pct']) / 100.0} kWh",
        "soc-max": f"{cap * float(b['soc_max_pct']) / 100.0} kWh",
        "power-capacity": f"{float(b['power_capacity_kw'])} kW",
    }


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
        return results, scheduler
    for result in results:
        # The schedule is the result whose payload is a power Series tied to a sensor.
        # Analysis payloads (soft-constraint results, commitment costs) are dicts, so they are skipped.
        if "sensor" in result and isinstance(result.get("data"), pd.Series):
            return result["data"], scheduler
    raise RuntimeError("Scheduler returned no schedule series.")


def snapshot_inputs(cfg, start, end, belief_time):
    """Record the exact inflexible-load inputs the scheduler will see, as of belief_time.

    This is the reproducibility anchor: the same (window, belief_time) reads the same beliefs.
    """
    from flexmeasures.data.models.time_series import Sensor, TimedBelief

    snapshot = []
    for ref in cfg["flex_context"].get("inflexible-consumption", []):
        sid = ref["sensor"] if isinstance(ref, dict) else ref
        sensor = Sensor.query.get(sid)
        bdf = TimedBelief.search(
            sensors=sensor,
            event_starts_after=start,
            event_ends_before=end,
            beliefs_before=belief_time,
        )
        values = [float(v) for v in bdf.values.flatten()] if len(bdf) else []
        snapshot.append(
            {
                "sensor_id": sid,
                "sensor_name": sensor.name,
                "source_topic": (sensor.attributes or {}).get("source_topic"),
                "n_beliefs_in_window": len(values),
                "values": values,
            }
        )
    return snapshot


def series_to_records(series):
    if series is None or len(series) == 0:
        return []
    return [
        {"event_start": ts.isoformat(), "value": (None if v != v else float(v))}
        for ts, v in series.items()
    ]


def main():
    parser = argparse.ArgumentParser(
        description="labems-sc9 shadow storage scheduler (compute-only)."
    )
    parser.add_argument(
        "--config", required=True, help="Path to shadow-g2-config.json."
    )
    parser.add_argument(
        "--start",
        help="ISO start of the schedule window (default: next resolution boundary from now).",
    )
    parser.add_argument(
        "--belief-time", help="ISO belief_time to pin inputs (default: now, UTC)."
    )
    parser.add_argument(
        "--target-sensor",
        type=int,
        help="Override the sensor to schedule (validation only).",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Persist the schedule to the output sensor (writes the FM DB). Off by default.",
    )
    parser.add_argument(
        "--prove-reproducible",
        action="store_true",
        help="Compute twice on pinned inputs and assert identical output.",
    )
    parser.add_argument(
        "--log", default="/tmp/shadow-g2-log.jsonl", help="Append-only run log path."
    )
    args = parser.parse_args()

    with open(args.config) as f:
        config_text = f.read()
    cfg = json.loads(config_text)
    config_version = cfg.get("version", "unversioned")
    config_sha256 = hashlib.sha256(config_text.encode()).hexdigest()

    resolution = _iso_duration_to_timedelta(cfg["resolution"])
    horizon = timedelta(hours=float(cfg["horizon_hours"]))

    app = create_app()
    with app.app_context():
        import flexmeasures
        from flexmeasures.data import db  # noqa: F401
        from flexmeasures.data.models.time_series import Sensor
        from flexmeasures.data.models.planning.storage import StorageScheduler

        now = datetime.now(tz=timezone.utc)
        belief_time = _isoparse(args.belief_time) if args.belief_time else now
        start = _isoparse(args.start) if args.start else _floor_to(now, resolution)
        end = start + horizon

        target_id = (
            args.target_sensor
            or cfg.get("target_output_sensor_id")
            or cfg.get("scheduling_sensor_id_for_validation")
        )
        if target_id is None:
            print(
                "No target sensor: set target_output_sensor_id (once the output sensor exists) or pass --target-sensor.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        sensor = Sensor.query.get(target_id)
        if sensor is None:
            print(f"Target sensor {target_id} not found.", file=sys.stderr)
            raise SystemExit(2)

        flex_model = build_flex_model(cfg)
        flex_context = cfg["flex_context"]

        inputs = snapshot_inputs(cfg, start, end, belief_time)
        series, _scheduler = compute_schedule_series(
            sensor, start, end, resolution, belief_time, flex_model, flex_context
        )
        records = series_to_records(series)

        code_version = f"{flexmeasures.__version__}; StorageScheduler v{StorageScheduler.__version__}"

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
            },
            "belief_time": belief_time.isoformat(),
            "target_sensor_id": target_id,
            "flex_model": flex_model,
            "flex_context": flex_context,
            "inputs": inputs,
            "schedule": records,
            "code_version": code_version,
            "broker_publishes": 0,
            "hardware_commands": 0,
            "committed": bool(args.commit),
        }

        if args.prove_reproducible:
            series2, _ = compute_schedule_series(
                sensor, start, end, resolution, belief_time, flex_model, flex_context
            )
            records2 = series_to_records(series2)
            run["reproducible"] = records == records2

        with open(args.log, "a") as logf:
            logf.write(json.dumps(run) + "\n")

        if args.commit:
            if cfg.get("target_output_sensor_id") is None:
                print(
                    "--commit requires target_output_sensor_id in the config (pending YellowHeron). Refusing to write.",
                    file=sys.stderr,
                )
                raise SystemExit(3)
            from flexmeasures.data.services.scheduling import make_schedule
            from flexmeasures.data.services.utils import get_asset_or_sensor_ref

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

        # Human-readable summary to stdout.
        print(
            f"run_utc={now.isoformat()} config={config_version} sha256={config_sha256[:12]} "
            f"target_sensor={target_id} window={start.isoformat()}..{end.isoformat()} "
            f"belief_time={belief_time.isoformat()} schedule_steps={len(records)} "
            f"committed={bool(args.commit)} broker_publishes=0 hardware_commands=0"
        )
        if "reproducible" in run:
            print(f"reproducible={run['reproducible']}")
        nonzero = [r for r in records if r["value"] not in (None, 0.0)]
        print(f"nonzero_schedule_steps={len(nonzero)} of {len(records)}")
        for r in records[:8]:
            print(f"  {r['event_start']}  {r['value']}")


if __name__ == "__main__":
    main()
