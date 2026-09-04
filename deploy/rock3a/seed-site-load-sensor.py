"""Seed the derived "site load" sensor for the labems-sc9 shadow scheduler (rubіж G2).

DO NOT RUN YET.
Creating this sensor is gated on YellowHeron confirming that a derived-load sensor on the EXISTING
Office asset (id 5) is within the directive's "no new assets" boundary — a sensor is not an asset, and
the labems-3oh / output-sensor precedent allows sensors on existing assets.
Owner authorization to write the live FlexMeasures DB is already given for sc9; this seed still waits
for the boundary confirmation, and it refuses to do anything without the explicit --i-have-approval flag.

Why a dedicated sensor: FlexMeasures reads the scheduler's inflexible-consumption from a sensor, so the
derived real site load (P_load = grid + battery) must live on its own sensor.
Scheduling against the raw behind-meter deye/ac/total_grid_power would be circular (the net grid already
nets the battery); the derived load is the honest inflexible input — see shadow-g2.md.

Idempotent — safe to re-run once creation is authorized.
Importing this module has NO side effects; it only acts under main().
Run inside the server container, from the repo root on the board:
    podman exec -i rock3a_server_1 python - --i-have-approval < deploy/rock3a/seed-site-load-sensor.py
"""

import argparse
from datetime import timedelta

OFFICE_ASSET_ID = 5
SENSOR_NAME = "site load"
UNIT = "kW"
RESOLUTION = timedelta(minutes=15)
TZ = "Europe/Kyiv"
MARKER = "sc9-site-load"


def main():
    parser = argparse.ArgumentParser(
        description="Seed the labems-sc9 derived site-load sensor."
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

        asset = db.session.get(GenericAsset, OFFICE_ASSET_ID)
        if asset is None or asset.name != "Office":
            raise SystemExit(
                f"Asset {OFFICE_ASSET_ID} is not Office (got {asset and asset.name!r})."
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
            # Reconcile the derived sensor to the desired state, so a rerun repairs drift.
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
            if (attrs.get("labems")) != MARKER:
                merged = dict(attrs)
                merged["labems"] = MARKER
                existing.attributes = merged
                changed = True
            db.session.commit()
            print(
                f"[{'updated' if changed else 'exists '}] Office id={existing.id} {SENSOR_NAME} {existing.unit} res={existing.event_resolution}"
            )
            print(f"Set site_load_sensor_id to {existing.id} in shadow-g2-config.json.")
            return

        sensor = Sensor(
            name=SENSOR_NAME,
            generic_asset=asset,
            unit=UNIT,
            event_resolution=RESOLUTION,
            timezone=TZ,
            attributes={"labems": MARKER},
        )
        db.session.add(sensor)
        db.session.flush()
        db.session.commit()
        print(f"[created] Office id={sensor.id} {SENSOR_NAME} {UNIT} res={RESOLUTION}")
        print(f"Set site_load_sensor_id to {sensor.id} in shadow-g2-config.json.")


if __name__ == "__main__":
    main()
