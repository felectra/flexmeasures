"""Seed the dedicated output sensor for the labems-sc9 shadow scheduler (rubіж G2).

DO NOT RUN YET.
Creating this sensor is gated on YellowHeron confirming that a scheduling output sensor on the
EXISTING BatteryBank (asset 6) is within the directive's "no new assets" boundary — a sensor is not
an asset, and labems-3oh set the precedent that sensors on existing assets are allowed.
Owner authorization to write the live FlexMeasures DB is already given for sc9; this seed still
waits for the boundary confirmation.

Why a dedicated sensor: the labems-3oh measured sensors are all event_resolution = 0
(instantaneous), but a schedule needs a sensor with a non-zero resolution (PT15M here).
Writing schedule beliefs onto a measured sensor would also mix scheduled and measured series.
This sensor is the StorageScheduler target and the place the shadow schedule is stored.

Idempotent — safe to re-run once creation is authorized.
Run inside the server container, from the repo root on the board:
    podman exec -i rock3a_server_1 python - < deploy/rock3a/seed-shadow-sensor.py
"""

from datetime import timedelta
from flexmeasures.app import create as create_app

ASSET_NAME = "BatteryBank"
SENSOR_NAME = "shadow schedule power"
UNIT = "kW"
RESOLUTION = timedelta(minutes=15)
TZ = "Europe/Kyiv"

app = create_app()
with app.app_context():
    from flexmeasures.data import db
    from flexmeasures.data.models.generic_assets import GenericAsset
    from flexmeasures.data.models.time_series import Sensor

    asset = (
        db.session.query(GenericAsset)
        .filter(GenericAsset.account_id == 1, GenericAsset.name == ASSET_NAME)
        .one()
    )
    existing = (
        db.session.query(Sensor)
        .filter_by(name=SENSOR_NAME, generic_asset_id=asset.id)
        .first()
    )
    if existing is not None:
        print(f"[exists ] {ASSET_NAME} id={existing.id} {SENSOR_NAME} {existing.unit}")
    else:
        sensor = Sensor(
            name=SENSOR_NAME,
            generic_asset=asset,
            unit=UNIT,
            event_resolution=RESOLUTION,
            timezone=TZ,
            attributes={
                # Marks this as the sc9 shadow output; charging is stored as positive.
                "labems": "sc9-shadow-schedule",
                "consumption_is_positive": True,
            },
        )
        db.session.add(sensor)
        db.session.flush()
        db.session.commit()
        print(
            f"[created] {ASSET_NAME} id={sensor.id} {SENSOR_NAME} {UNIT} res={RESOLUTION}"
        )
