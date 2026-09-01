"""MQTT -> FlexMeasures ingestion for the ROCK 3A pilot (labems-3oh).

READ-ONLY on the broker. Reads `topic payload` lines on stdin (exactly the format of
`mosquitto_sub -v`) and writes one TimedBelief per line whose topic matches a sensor's
attributes["source_topic"] (set by seed-sensors.py). Topics with no matching sensor are
ignored — so the forbidden sources (deye/battery/soc, deye/bms/*) are never ingested, because
no sensor binds them. Non-numeric payloads (text/binary/status/debug topics) are skipped.

The receiver stamps arrival time — payloads carry no timestamp. Sensors are instantaneous
(event_resolution = 0): event_start = belief_time = arrival time (UTC).

Usage on the board (broker read-only via the anonymous local listener; writes only the
FlexMeasures DB):

    podman cp deploy/rock3a/mqtt_ingest.py rock3a_server_1:/tmp/mqtt_ingest.py
    mosquitto_sub -h 127.0.0.1 -t 'deye/#' -t 'jkbms/#' -v -W 65 \
      | podman exec -i rock3a_server_1 python /tmp/mqtt_ingest.py

`-W <seconds>` bounds the read-only capture. It never publishes; it only subscribes.
"""

import sys
from datetime import datetime, timezone

from flexmeasures.app import create as create_app

app = create_app()
with app.app_context():
    from flexmeasures.data import db
    from flexmeasures.data.models.data_sources import DataSource
    from flexmeasures.data.models.time_series import Sensor, TimedBelief

    # Build topic -> sensor from the source_topic attribute set by seed-sensors.py.
    topic_to_sensor = {}
    for sensor in db.session.query(Sensor).all():
        topic = (sensor.attributes or {}).get("source_topic")
        if topic:
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
    skipped_nonnumeric = 0
    last_value = {}
    beliefs = []
    for raw in sys.stdin:
        line = raw.rstrip("\n")
        topic, sep, payload = line.partition(" ")
        if not sep:
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
        now = datetime.now(tz=timezone.utc)
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
        last_value[sensor.name] = (sensor.id, value)

    db.session.add_all(beliefs)
    db.session.commit()

    print(
        f"ingested={ingested} skipped_unmapped={skipped_unmapped} "
        f"skipped_nonnumeric={skipped_nonnumeric} distinct_sensors={len(last_value)}"
    )
    for name in sorted(last_value):
        sid, val = last_value[name]
        print(f"  id={sid:<4} {name:<24} last_value={val}")
