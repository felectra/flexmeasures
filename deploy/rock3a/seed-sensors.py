"""Seed FlexMeasures sensors for labems-3oh — bind live deye/* and jkbms/* telemetry.

Idempotent — safe to re-run. Creates sensors on the EXISTING assets (never creates assets),
per telemetry-v1 and the 2026-09-01 capture. Forbidden sources are never bound: deye/battery/soc
(inverter estimate is wrong — 100 % at ~1/3 charge) and the whole deye/bms/* group (zeros, no CAN).

Readings are instantaneous point values, so event_resolution = 0. The nominal polling period is
recorded per sensor in attributes["sampling_period"] (deye 60 s; jkbms string_a 10 s / string_b
13 s), and the source MQTT topic in attributes["source_topic"] so the ingestion bridge maps
topic -> sensor. The receiver stamps arrival time (payloads carry no timestamp).

Run inside the server container, from the repo root on the board:
    podman exec -i rock3a_server_1 python - < deploy/rock3a/seed-sensors.py
"""

from datetime import timedelta
from flexmeasures.app import create as create_app

app = create_app()
with app.app_context():
    from flexmeasures.data import db
    from flexmeasures.data.models.generic_assets import GenericAsset
    from flexmeasures.data.models.time_series import Sensor

    TZ = "Europe/Kyiv"
    DEYE = "PT1M"

    # (asset_name, sensor_name, unit, source_topic, sampling_period)
    specs = [
        # Office — grid connection point <- deye/ac/*  (positive grid power = import)
        ("Office", "grid power", "W", "deye/ac/total_grid_power", DEYE),
        ("Office", "inverter ac power", "W", "deye/ac/total_power", DEYE),
        ("Office", "grid voltage L1", "V", "deye/ac/l1/voltage", DEYE),
        ("Office", "grid voltage L2", "V", "deye/ac/l2/voltage", DEYE),
        ("Office", "grid frequency", "Hz", "deye/ac/frequency", DEYE),
        ("Office", "inverter ac temperature", "degC", "deye/ac/temperature", DEYE),
        ("Office", "energy bought today", "kWh", "deye/ac/daily_energy_bought", DEYE),
        ("Office", "energy sold today", "kWh", "deye/ac/daily_energy_sold", DEYE),
        ("Office", "energy bought total", "kWh", "deye/ac/total_energy_bought", DEYE),
        ("Office", "energy sold total", "kWh", "deye/ac/total_energy_sold", DEYE),
        # BatteryBank — the battery as the inverter reports it <- deye/battery/* (NO soc, NO bms)
        ("BatteryBank", "battery power", "W", "deye/battery/power", DEYE),
        ("BatteryBank", "battery voltage", "V", "deye/battery/voltage", DEYE),
        ("BatteryBank", "battery current", "A", "deye/battery/current", DEYE),
        (
            "BatteryBank",
            "battery temperature",
            "degC",
            "deye/battery/temperature",
            DEYE,
        ),
        (
            "BatteryBank",
            "battery charge today",
            "kWh",
            "deye/battery/daily_charge",
            DEYE,
        ),
        (
            "BatteryBank",
            "battery discharge today",
            "kWh",
            "deye/battery/daily_discharge",
            DEYE,
        ),
        (
            "BatteryBank",
            "battery charge total",
            "kWh",
            "deye/battery/total_charge",
            DEYE,
        ),
        (
            "BatteryBank",
            "battery discharge total",
            "kWh",
            "deye/battery/total_discharge",
            DEYE,
        ),
    ]
    # String A / String B — per-string JK-BMS <- jkbms/string_<x>/sensor/<object_id>/state
    jk = [
        ("pack voltage", "V", "total_voltage"),
        ("current", "A", "current"),
        ("power", "W", "power"),
        ("state of charge", "%", "state_of_charge"),
        ("capacity remaining", "Ah", "capacity_remaining"),
        ("delta cell voltage", "V", "delta_cell_voltage"),
        ("temperature 1", "degC", "temperature_1"),
        ("temperature 2", "degC", "temperature_2"),
        ("mosfet temperature", "degC", "mosfet_temperature"),
        ("data age", "s", "data_age"),
    ]
    for asset_name, key, sp in (
        ("String A", "string_a", "PT10S"),
        ("String B", "string_b", "PT13S"),
    ):
        for sname, unit, obj in jk:
            specs.append(
                (asset_name, sname, unit, f"jkbms/{key}/sensor/{obj}/state", sp)
            )

    assets = {
        a.name: a
        for a in db.session.query(GenericAsset)
        .filter(GenericAsset.account_id == 1)
        .all()
    }

    def get_or_create(asset, name, unit, topic, sampling):
        existing = (
            db.session.query(Sensor)
            .filter_by(name=name, generic_asset_id=asset.id)
            .first()
        )
        if existing is not None:
            return existing, False
        sensor = Sensor(
            name=name,
            generic_asset=asset,
            unit=unit,
            event_resolution=timedelta(0),
            timezone=TZ,
            attributes={"source_topic": topic, "sampling_period": sampling},
        )
        db.session.add(sensor)
        db.session.flush()
        return sensor, True

    created = 0
    print("Sensors on Felectra assets (account 1):")
    for asset_name, sname, unit, topic, sampling in specs:
        sensor, was_new = get_or_create(
            assets[asset_name], sname, unit, topic, sampling
        )
        created += 1 if was_new else 0
        tag = "created" if was_new else "exists "
        print(
            f"  [{tag}] {asset_name:<12} id={sensor.id:<4} {sname:<24} {unit:<4} <- {topic}"
        )
    db.session.commit()
    print(
        f"\n{created} created, {len(specs) - created} already existed; {len(specs)} total."
    )
