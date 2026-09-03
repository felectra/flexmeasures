"""Continuous MQTT -> FlexMeasures ingestion for the ROCK 3A pilot (labems-t2t).

SUBSCRIBE-ONLY.
This process instantiates no MQTT client:
it reads `topic payload` lines on stdin from a persistent `mosquitto_sub -v`, so it can never publish or command anything.
It writes one TimedBelief per accepted line to FlexMeasures' own database only;
nothing reaches the broker, the bridge, the inverter, or the BMS boards.

It is the continuous successor to the one-shot mqtt_ingest.py (labems-3oh), and keeps that tool's guards:
the topic -> sensor map from attributes["source_topic"], a single "mqtt-ingest" DataSource, the forbidden-source deny-list,
the math.isfinite drop, and a per-sensor strictly-increasing arrival stamp so beliefs never collide on the TimedBelief primary key.

Three behaviours are new for a never-ending stream:
- Batched commits, roughly every 200 lines or 10 seconds, whichever comes first;
  a failed batch is retried row by row so one bad row cannot lose the rest, and nothing crashes the service.
- Staleness gate: the latest data_age is tracked per string (jkbms/string_a, jkbms/string_b);
  a string is treated as stale (its readings skipped) until it reports a fresh data_age, and again whenever data_age exceeds 60 s,
  is missing, is nan, or is negative — so stale BLE data never enters FlexMeasures.
- Robust framing: stdin is read byte by byte with its own line buffer, so partial lines never block the timers,
  and invalid UTF-8 is replaced rather than crashing the reader.

A periodic stderr heartbeat prints the running counters, so `journalctl` shows the flow is healthy.
`committed` is the durable count; `ingested` counts lines accepted for writing (before the commit succeeds).

Run inside the server container, fed by a host-side subscriber (see flexmeasures-ingest.sh):
    mosquitto_sub -h 127.0.0.1 -t 'deye/#' -t 'jkbms/#' -v | podman exec -i rock3a_server_1 python -u /tmp/continuous_ingest.py
"""

import hashlib
import math
import os
import select
import signal
import sys
import time
from datetime import datetime, timedelta, timezone

from flexmeasures.app import create as create_app

# Sources that must never be ingested, enforced independently of what the DB happens to bind:
# the inverter SOC estimate is wrong (reads full at ~1/3 charge),
# and the deye/bms/* group is all zeros (no CAN link).
FORBIDDEN_TOPIC_PREFIXES = ("deye/battery/soc", "deye/bms/")
# The two per-string JK-BMS topic namespaces, used for the data_age staleness gate.
STRING_PREFIXES = ("jkbms/string_a", "jkbms/string_b")
STALE_AGE_SECONDS = 60.0

BATCH_MAX_LINES = 200
BATCH_MAX_SECONDS = 10.0
HEARTBEAT_SECONDS = 60.0
SELECT_TIMEOUT_SECONDS = 5.0
READ_CHUNK_BYTES = 65536
MAX_LINE_BUFFER_BYTES = 1_000_000


def is_forbidden(topic):
    """Return True for a topic in the forbidden deny-list (deye/battery/soc, deye/bms/*)."""
    return any(topic == p or topic.startswith(p) for p in FORBIDDEN_TOPIC_PREFIXES)


def string_prefix(topic):
    """Return the jkbms string namespace a topic belongs to, or None."""
    for prefix in STRING_PREFIXES:
        if topic.startswith(prefix + "/"):
            return prefix
    return None


def main():  # noqa: C901
    """Run the continuous, subscribe-only ingestion loop until stdin closes or SIGTERM."""
    self_sha = "unknown"
    try:
        with open(os.path.abspath(__file__), "rb") as fh:
            self_sha = hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        pass

    app = create_app()
    with app.app_context():
        from sqlalchemy import text
        from sqlalchemy.exc import SQLAlchemyError
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

        # The data_age topic per string drives the staleness gate.
        data_age_topics = {f"{p}/sensor/data_age/state": p for p in STRING_PREFIXES}

        source = (
            db.session.query(DataSource)
            .filter_by(name="mqtt-ingest", type="mqtt")
            .first()
        )
        if source is None:
            source = DataSource(name="mqtt-ingest", type="mqtt")
            db.session.add(source)
            db.session.commit()

        # Seed the per-sensor stamp from the newest belief already stored, so monotonicity survives a
        # restart or a backward clock step and never collides with a stored belief.
        last_ts = {}
        rows = db.session.execute(
            text(
                "SELECT sensor_id, max(event_start) FROM timed_belief WHERE source_id = :sid GROUP BY sensor_id"
            ),
            {"sid": source.id},
        )
        for sid, mx in rows:
            if mx is not None:
                last_ts[sid] = mx if mx.tzinfo else mx.replace(tzinfo=timezone.utc)

        counters = {
            "lines": 0,
            "ingested": 0,
            "committed": 0,
            "skipped_unmapped": 0,
            "skipped_forbidden": 0,
            "skipped_nonnumeric": 0,
            "skipped_nonfinite": 0,
            "skipped_stale": 0,
            "skipped_malformed": 0,
            "committed_batches": 0,
            "commit_errors": 0,
            "commit_row_errors": 0,
        }
        last_data_age = {}
        batch = []

        def commit_batch():
            if not batch:
                return
            try:
                db.session.add_all(batch)
                db.session.commit()
                counters["committed"] += len(batch)
                counters["committed_batches"] += 1
            except SQLAlchemyError as exc:
                # A poisoned or transient batch must never kill the service.
                # Roll back, then retry row by row so one bad row cannot lose the whole batch.
                db.session.rollback()
                counters["commit_errors"] += 1
                print(
                    f"[t2t] batch commit failed ({exc.__class__.__name__}); salvaging row by row",
                    file=sys.stderr,
                    flush=True,
                )
                for belief in batch:
                    try:
                        db.session.add(belief)
                        db.session.commit()
                        counters["committed"] += 1
                    except SQLAlchemyError:
                        db.session.rollback()
                        counters["commit_row_errors"] += 1
            batch.clear()

        def heartbeat():
            up = int(time.monotonic() - start_mono)
            fields = " ".join(f"{k}={v}" for k, v in counters.items())
            print(
                f"[t2t] hb {fields} pending={len(batch)} uptime_s={up}",
                file=sys.stderr,
                flush=True,
            )

        def handle_line(raw):
            topic, sep, payload = raw.rstrip("\n").partition(" ")
            if not sep:
                counters["skipped_malformed"] += 1
                return
            if is_forbidden(topic):
                counters["skipped_forbidden"] += 1
                return
            # Maintain the staleness latch from every data_age line, before mapping or the gate,
            # and fail closed: an unparsable, non-finite, or negative age marks the string stale.
            gated_prefix = data_age_topics.get(topic)
            if gated_prefix is not None:
                try:
                    age = float(payload)
                except ValueError:
                    age = None
                if age is None or not math.isfinite(age) or age < 0:
                    last_data_age[gated_prefix] = math.inf
                else:
                    last_data_age[gated_prefix] = age
            sensor = topic_to_sensor.get(topic)
            if sensor is None:
                counters["skipped_unmapped"] += 1
                return
            try:
                value = float(payload)
            except ValueError:
                counters["skipped_nonnumeric"] += 1
                return
            if not math.isfinite(value):
                # A JK-BMS publishes `nan` during a BLE dropout; never store that.
                counters["skipped_nonfinite"] += 1
                return
            prefix = string_prefix(topic)
            if prefix is not None:
                # Fail closed: an unseen string (age None) is stale until it reports a fresh data_age.
                age = last_data_age.get(prefix)
                if age is None or age > STALE_AGE_SECONDS:
                    counters["skipped_stale"] += 1
                    return
            now = datetime.now(tz=timezone.utc)
            # Keep the arrival stamp strictly increasing per sensor,
            # so a same-microsecond message (or a backward clock step) never collides on the primary key.
            earliest = last_ts.get(sensor.id)
            if earliest is not None and now <= earliest:
                now = earliest + timedelta(microseconds=1)
            last_ts[sensor.id] = now
            batch.append(
                TimedBelief(
                    sensor=sensor,
                    source=source,
                    event_start=now,
                    belief_time=now,
                    event_value=value,
                )
            )
            counters["ingested"] += 1

        # A clean stop (systemd SIGTERM, if delivered) commits what we have and exits zero;
        # the primary graceful path is stdin EOF when the subscriber is killed (see the finally block).
        def _on_sigterm(signum, frame):
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, _on_sigterm)

        start_mono = time.monotonic()
        last_commit = start_mono
        last_heartbeat = start_mono
        exit_code = 0
        stdin_fd = sys.stdin.fileno()
        buf = b""
        print(
            f"[t2t] started sha256={self_sha[:12]} source_id={source.id} mapped_topics={len(topic_to_sensor)}",
            file=sys.stderr,
            flush=True,
        )
        try:
            while True:
                readable, _, _ = select.select(
                    [stdin_fd], [], [], SELECT_TIMEOUT_SECONDS
                )
                if readable:
                    try:
                        chunk = os.read(stdin_fd, READ_CHUNK_BYTES)
                    except OSError:
                        chunk = b""
                    if chunk == b"":
                        # EOF: the upstream mosquitto_sub ended; exit non-zero so systemd restarts.
                        exit_code = 1
                        break
                    buf += chunk
                    if len(buf) > MAX_LINE_BUFFER_BYTES:
                        # Drop a pathological unterminated blob rather than grow without bound.
                        counters["skipped_malformed"] += 1
                        buf = b""
                    while b"\n" in buf:
                        rawline, buf = buf.split(b"\n", 1)
                        counters["lines"] += 1
                        try:
                            handle_line(rawline.decode("utf-8", "replace"))
                        except (
                            Exception
                        ) as exc:  # one bad line must never crash the service
                            counters["skipped_malformed"] += 1
                            print(
                                f"[t2t] line error: {exc.__class__.__name__}: {exc}",
                                file=sys.stderr,
                                flush=True,
                            )
                now_mono = time.monotonic()
                if batch and (
                    len(batch) >= BATCH_MAX_LINES
                    or now_mono - last_commit >= BATCH_MAX_SECONDS
                ):
                    commit_batch()
                    last_commit = now_mono
                if now_mono - last_heartbeat >= HEARTBEAT_SECONDS:
                    heartbeat()
                    last_heartbeat = now_mono
        finally:
            commit_batch()
            heartbeat()
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
