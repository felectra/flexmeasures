"""Seed the dedicated output sensor for the labems-sc9 shadow scheduler (rubіж G2).

DO NOT RUN YET.
Creating this sensor is gated on YellowHeron confirming that a scheduling output sensor on the
EXISTING BatteryBank (asset id 6) is within the directive's "no new assets" boundary — a sensor is
not an asset, and labems-3oh set the precedent that sensors on existing assets are allowed.
Owner authorization to write the live FlexMeasures DB is already given for sc9; this seed still waits
for the boundary confirmation, and it refuses to do anything without the explicit --i-have-approval
flag.

Why a dedicated sensor: the labems-3oh measured sensors are all event_resolution = 0 (instantaneous),
but a schedule needs a sensor with a non-zero resolution (PT15M here).
Writing schedule beliefs onto a measured sensor would also mix scheduled and measured series.
This sensor is the StorageScheduler target and the place the shadow schedule is stored.

Idempotent — safe to re-run once creation is authorized.
Importing this module has NO side effects; it only acts under main().
Run inside the server container, from the repo root on the board:
    podman exec -i rock3a_server_1 python - --i-have-approval < deploy/rock3a/seed-shadow-sensor.py
"""

import argparse
from datetime import timedelta

BATTERYBANK_ASSET_ID = 6
SENSOR_NAME = "shadow schedule power"
UNIT = "kW"
RESOLUTION = timedelta(minutes=15)
TZ = "Europe/Kyiv"
MARKER = "sc9-shadow-schedule"


def main():
    parser = argparse.ArgumentParser(
        description="Seed the labems-sc9 shadow output sensor."
    )
    parser.add_argument(
        "--i-have-approval",
        action="store_true",
        help="Required. Confirms YellowHeron's boundary approval and the owner's DB-write authorization.",
    )
    args = parser.parse_args()
    if not args.i_have_approval:
        raise SystemExit(
            "Refusing to run: creation is gated on YellowHeron. Re-run with --i-have-approval only once approved."
        )

    from flexmeasures.app import create as create_app

    app = create_app()
    with app.app_context():
        from flexmeasures.data import db
        from flexmeasures.data.models.generic_assets import GenericAsset
        from flexmeasures.data.models.time_series import Sensor

        asset = db.session.get(GenericAsset, BATTERYBANK_ASSET_ID)
        if asset is None or asset.name != "BatteryBank":
            raise SystemExit(
                f"Asset {BATTERYBANK_ASSET_ID} is not BatteryBank (got {asset and asset.name!r})."
            )

        existing = (
            db.session.query(Sensor)
            .filter_by(name=SENSOR_NAME, generic_asset_id=asset.id)
            .first()
        )
        if existing is not None:
            attrs = existing.attributes or {}
            if attrs.get("source_topic") is not None:
                raise SystemExit(
                    f"Sensor {existing.id} carries source_topic {attrs.get('source_topic')!r} — a measured sensor; refusing."
                )
            # Reconcile the output sensor to the desired state, so a rerun repairs drift.
            changed = False
            if existing.unit != UNIT:
                existing.unit = UNIT
                changed = True
            if existing.event_resolution != RESOLUTION:
                existing.event_resolution = RESOLUTION
                changed = True
            if existing.timezone != TZ:
                existing.timezone = TZ
                changed = True
            desired = {"labems": MARKER, "consumption_is_positive": True}
            if {k: attrs.get(k) for k in desired} != desired:
                merged = dict(attrs)
                merged.update(desired)
                existing.attributes = merged
                changed = True
            db.session.commit()
            print(
                f"[{'updated' if changed else 'exists '}] BatteryBank id={existing.id} {SENSOR_NAME} {existing.unit} res={existing.event_resolution}"
            )
            return

        sensor = Sensor(
            name=SENSOR_NAME,
            generic_asset=asset,
            unit=UNIT,
            event_resolution=RESOLUTION,
            timezone=TZ,
            attributes={"labems": MARKER, "consumption_is_positive": True},
        )
        db.session.add(sensor)
        db.session.flush()
        db.session.commit()
        print(
            f"[created] BatteryBank id={sensor.id} {SENSOR_NAME} {UNIT} res={RESOLUTION}"
        )


if __name__ == "__main__":
    main()
