"""MQTT -> FlexMeasures ingestion for the ROCK 3A pilot (labems-3oh).

READ-ONLY on the broker.
Reads `topic payload` lines on stdin (exactly the format of `mosquitto_sub -v`),
and writes one TimedBelief per line whose topic matches a sensor's attributes["source_topic"] (set by seed-sensors.py).
Topics with no matching sensor are skipped.
As a second guardrail the forbidden sources (deye/battery/soc, and the whole deye/bms/* group) are denied here explicitly,
so even a mis-seeded sensor cannot ingest them.
Non-numeric and non-finite payloads (text, status, debug, or the `nan` a JK-BMS emits during a BLE dropout) are skipped.

The receiver stamps arrival time — payloads carry no timestamp.
Sensors are instantaneous (event_resolution = 0): event_start = belief_time = arrival time (UTC).
Within one run the arrival stamp is kept strictly increasing per sensor,
so two messages that land in the same microsecond do not collide on the TimedBelief primary key.

Usage on the board (broker read-only via the anonymous local listener; writes only the FlexMeasures DB):

    podman cp deploy/rock3a/mqtt_ingest.py rock3a_server_1:/tmp/mqtt_ingest.py
    mosquitto_sub -h 127.0.0.1 -t 'deye/#' -t 'jkbms/#' -v -W 65 \
      | podman exec -i rock3a_server_1 python /tmp/mqtt_ingest.py

`-W <seconds>` bounds the read-only capture.
It never publishes; it only subscribes.
"""

import math
import sys
from datetime import datetime, timedelta, timezone

from flexmeasures.app import create as create_app

# Sources that must never be ingested, enforced independently of what the DB happens to bind:
# the inverter SOC estimate is wrong (reads full at ~1/3 charge),
# and the deye/bms/* group is all zeros (no CAN link).
FORBIDDEN_TOPIC_PREFIXES = ("deye/battery/soc", "deye/bms/")


def is_forbidden(topic):
    return any(topic == p or topic.startswith(p) for p in FORBIDDEN_TOPIC_PREFIXES)


app = create_app()
with app.app_context():
    from flexmeasures.data import db
    from flexmeasures.data.models.data_sources import DataSource
    from flexmeasures.data.models.time_series import Sensor, TimedBelief

    # Build topic -> sensor from the source_topic attribute set by seed-sensors.py.
    # A topic bound to more than one sensor is ambiguous, so fail loudly rather than pick one.
    topic_to_sensor = {}
    for sensor in db.session.query(Sensor).all():
        topic = (sensor.attributes or {}).get("source_topic")
        if not topic:
            continue
        if topic in topic_to_sensor:
            raise SystemExit(
                f"Ambiguous binding: topic {topic!r} is on sensors "
                f"{topic_to_sensor[topic].id} and {sensor.id}."
            )
        topic_to_sensor[topic] = sensor

    source = (
        db.session.query(DataSource).filter_by(name="mqtt-ingest", type="mqtt").first()
    )
    if source is None:
        source = DataSource(name="mqtt-ingest", type="mqtt")
        db.session.add(source)
        db.session.flush()

    ingested = 0
    skipped_unmapped = 0
    skipped_forbidden = 0
    skipped_nonnumeric = 0
    skipped_nonfinite = 0
    last_ts = {}
    last_value = {}
    beliefs = []
    for raw in sys.stdin:
        line = raw.rstrip("\n")
        topic, sep, payload = line.partition(" ")
        if not sep:
            continue
        if is_forbidden(topic):
            skipped_forbidden += 1
            continue
        sensor = topic_to_sensor.get(topic)
        if sensor is None:
            skipped_unmapped += 1
            continue
        try:
            value = float(payload)
        except ValueError:
            skipped_nonnumeric += 1
            continue
        if not math.isfinite(value):
            # A JK-BMS publishes `nan` during a BLE dropout; never store that.
            skipped_nonfinite += 1
            continue
        now = datetime.now(tz=timezone.utc)
        # Keep the arrival stamp strictly increasing per sensor,
        # so a same-microsecond pair does not collide on the TimedBelief primary key and roll back the batch.
        earliest = last_ts.get(sensor.id)
        if earliest is not None and now <= earliest:
            now = earliest + timedelta(microseconds=1)
        last_ts[sensor.id] = now
        beliefs.append(
            TimedBelief(
                sensor=sensor,
                source=source,
                event_start=now,
                belief_time=now,
                event_value=value,
            )
        )
        ingested += 1
        # Key by sensor id, not name — String A and String B share sensor names,
        # so a name-keyed summary would collapse the two strings and undercount.
        last_value[sensor.id] = (sensor.generic_asset.name, sensor.name, value)

    db.session.add_all(beliefs)
    db.session.commit()

    print(
        f"ingested={ingested} skipped_unmapped={skipped_unmapped} "
        f"skipped_forbidden={skipped_forbidden} skipped_nonnumeric={skipped_nonnumeric} "
        f"skipped_nonfinite={skipped_nonfinite} distinct_sensors={len(last_value)}"
    )
    for sid in sorted(last_value):
        asset_name, name, val = last_value[sid]
        print(f"  id={sid:<4} {asset_name:<12} {name:<24} last_value={val}")
