"""Continuous MQTT -> FlexMeasures ingestion for the ROCK 3A pilot (labems-t2t).

SUBSCRIBE-ONLY.
This process instantiates no MQTT client.
It reads one JSON object per line on stdin from a persistent `mosquitto_sub -F '%j'`, so it can never publish or command anything.
It writes one TimedBelief per accepted message to FlexMeasures' own database only; nothing reaches the broker, the bridge, the inverter, or the BMS boards.

The pure decision logic lives in t2t_core.py (import-light, unit-tested); this module is the thin shell that owns the app context and the database.
It keeps the labems-3oh guards (deny-list, math.isfinite, per-sensor monotonic arrival stamp) and adds, for a never-ending stream:
- JSON framing, so a newline inside a payload can never forge a topic and slip past the deny-list.
- An elapsed-time staleness gate per string, so a BLE dropout closes the gate even when no new data_age arrives, and retained messages never open it.
- Batched commits with a transient-vs-row policy: a poisoned row is salvaged row by row, while a database outage keeps the batch and exits so systemd retries.
- Circuit breakers: it exits non-zero after a storm of unexpected exceptions, or when lines keep flowing but nothing commits, so it never stays "active" while rejecting everything.

A periodic stderr heartbeat prints the running counters, so `journalctl` shows the flow is healthy.
`committed` is the durable count; `ingested` counts messages accepted for writing before the commit lands.

Run inside the server container, fed by a host-side subscriber (see flexmeasures-ingest.sh):
    mosquitto_sub -h 127.0.0.1 -t 'deye/#' -t 'jkbms/#' -F '%j' | podman exec -i rock3a_server_1 python -u /tmp/continuous_ingest.py
"""

import hashlib
import os
import select
import signal
import sys
import time
from datetime import datetime, timezone

import t2t_core
from flexmeasures.app import create as create_app

BATCH_MAX_LINES = 200
BATCH_MAX_SECONDS = 10.0
HEARTBEAT_SECONDS = 60.0
SELECT_TIMEOUT_SECONDS = 5.0
READ_CHUNK_BYTES = 65536
MAX_LINE_BYTES = 1_000_000
MAX_TRANSIENT_COMMIT_FAILURES = 5
TRANSIENT_BACKOFF_SECONDS = 2.0
MAX_UNEXPECTED_LINE_ERRORS = 100
# Health: if this many lines flow across a heartbeat with zero durable commits, count it unhealthy;
# after this many such heartbeats in a row, exit so systemd restarts (deye alone should always commit).
MIN_LINES_FOR_HEALTH = 20
MAX_ZERO_COMMIT_HEARTBEATS = 5


def main():  # noqa: C901
    """Run the continuous, subscribe-only ingestion loop until stdin closes or a stop is requested."""
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
        from flexmeasures.data.models.generic_assets import GenericAsset
        from flexmeasures.data.models.time_series import Sensor, TimedBelief

        # Build topic -> sensor from the source_topic attribute set by seed-sensors.py, scoped to the
        # pilot account (the 38 bound sensors).
        # A duplicate topic within scope is ambiguous, so it is logged and dropped entirely rather than
        # routed to an arbitrary sensor; ordering by id keeps the logging deterministic.
        topic_to_sensor = {}
        ambiguous = set()
        for sensor in (
            db.session.query(Sensor)
            .join(GenericAsset, GenericAsset.id == Sensor.generic_asset_id)
            .filter(GenericAsset.account_id == 1)
            .order_by(Sensor.id)
            .all()
        ):
            topic = (sensor.attributes or {}).get("source_topic")
            if not topic:
                continue
            if topic in topic_to_sensor or topic in ambiguous:
                ambiguous.add(topic)
                topic_to_sensor.pop(topic, None)
                print(
                    f"[t2t] ambiguous source_topic {topic!r} on more than one account-1 sensor; dropping it entirely",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            topic_to_sensor[topic] = sensor

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
        for sid, mx in db.session.execute(
            text(
                "SELECT sensor_id, max(event_start) FROM timed_belief WHERE source_id = :sid GROUP BY sensor_id"
            ),
            {"sid": source.id},
        ):
            if mx is not None:
                last_ts[sid] = mx if mx.tzinfo else mx.replace(tzinfo=timezone.utc)

        gate = t2t_core.StalenessGate()
        counters = {
            "lines": 0,
            "ingested": 0,
            "committed": 0,
            "skipped_unmapped": 0,
            "skipped_forbidden": 0,
            "skipped_retained": 0,
            "skipped_nonnumeric": 0,
            "skipped_nonfinite": 0,
            "skipped_stale": 0,
            "skipped_malformed": 0,
            "committed_batches": 0,
            "commit_errors": 0,
            "commit_row_errors": 0,
            "unexpected_errors": 0,
            "lost_beliefs": 0,
        }
        skip_counter = {
            "retained": "skipped_retained",
            "forbidden": "skipped_forbidden",
            "malformed": "skipped_malformed",
            "nonnumeric": "skipped_nonnumeric",
            "nonfinite": "skipped_nonfinite",
            "stale": "skipped_stale",
        }
        batch = []

        def commit_batch():
            """Commit the batch. Return 'ok', or 'transient' if the DB is at fault (batch retained)."""
            if not batch:
                return "ok"
            try:
                db.session.add_all(batch)
                db.session.commit()
                counters["committed"] += len(batch)
                counters["committed_batches"] += 1
                batch.clear()
                return "ok"
            except SQLAlchemyError as exc:
                db.session.rollback()
                counters["commit_errors"] += 1
                if t2t_core.classify_db_error(exc) == "transient":
                    print(
                        f"[t2t] transient DB error, retaining {len(batch)} beliefs: {exc.__class__.__name__}",
                        file=sys.stderr,
                        flush=True,
                    )
                    return "transient"
                # A deterministic row error: salvage the good rows one by one, but stop and retain the
                # remainder if the database itself drops out mid-salvage (so a transient outage during
                # salvage cannot delete the rows we have not tried yet).
                print(
                    f"[t2t] row error, salvaging batch of {len(batch)} row by row: {exc.__class__.__name__}",
                    file=sys.stderr,
                    flush=True,
                )
                for index, belief in enumerate(batch):
                    try:
                        db.session.add(belief)
                        db.session.commit()
                        counters["committed"] += 1
                    except SQLAlchemyError as row_exc:
                        db.session.rollback()
                        if t2t_core.classify_db_error(row_exc) == "transient":
                            del batch[
                                :index
                            ]  # drop the rows already committed/skipped, keep the rest
                            print(
                                f"[t2t] DB dropped out during salvage; retaining {len(batch)} beliefs",
                                file=sys.stderr,
                                flush=True,
                            )
                            return "transient"
                        counters["commit_row_errors"] += 1
                batch.clear()
                return "ok"

        def heartbeat():
            up = int(time.monotonic() - start_mono)
            fields = " ".join(f"{k}={v}" for k, v in counters.items())
            print(
                f"[t2t] hb {fields} pending={len(batch)} uptime_s={up}",
                file=sys.stderr,
                flush=True,
            )

        def handle_line(raw):
            frame = t2t_core.parse_frame(raw)
            if frame is None:
                counters["skipped_malformed"] += 1
                return
            status, result = t2t_core.decide_reading(frame, gate, time.monotonic())
            if status == "skip":
                counters[skip_counter.get(result, "skipped_malformed")] += 1
                return
            sensor = topic_to_sensor.get(frame["topic"])
            if sensor is None:
                counters["skipped_unmapped"] += 1
                return
            now = t2t_core.next_monotonic_stamp(
                last_ts, sensor.id, datetime.now(tz=timezone.utc)
            )
            batch.append(
                TimedBelief(
                    sensor=sensor,
                    source=source,
                    event_start=now,
                    belief_time=now,
                    event_value=result,
                )
            )
            counters["ingested"] += 1

        # SIGTERM sets a flag rather than raising, so the stop goes through the same final-flush path
        # as EOF, and the exit code can tell the truth about a failed final commit.
        stop_requested = {"v": False}

        def _on_sigterm(signum, frame):
            stop_requested["v"] = True

        signal.signal(signal.SIGTERM, _on_sigterm)

        start_mono = time.monotonic()
        last_commit = start_mono
        last_heartbeat = start_mono
        prev_lines = 0
        prev_committed = 0
        zero_commit_heartbeats = 0
        transient_failures = 0
        exit_code = 0
        stdin_fd = sys.stdin.fileno()
        buf = b""
        discarding = False
        print(
            f"[t2t] started sha256={self_sha[:12]} source_id={source.id} mapped_topics={len(topic_to_sensor)}",
            file=sys.stderr,
            flush=True,
        )
        try:
            while True:
                if stop_requested["v"]:
                    break
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
                    while not discarding:
                        newline = buf.find(b"\n")
                        if newline == -1:
                            if len(buf) > MAX_LINE_BYTES:
                                # An unterminated oversized frame: discard until the next delimiter.
                                counters["skipped_malformed"] += 1
                                discarding = True
                                buf = b""
                            break
                        rawline = buf[:newline]
                        buf = buf[newline + 1 :]
                        if len(rawline) > MAX_LINE_BYTES:
                            # A complete but oversized line: drop it rather than parse it.
                            counters["skipped_malformed"] += 1
                            continue
                        counters["lines"] += 1
                        try:
                            handle_line(rawline.decode("utf-8", "replace"))
                        except (
                            Exception
                        ) as exc:  # contain one bad line, but trip the breaker on a storm
                            counters["unexpected_errors"] += 1
                            print(
                                f"[t2t] unexpected line error: {exc.__class__.__name__}: {exc}",
                                file=sys.stderr,
                                flush=True,
                            )
                            if (
                                counters["unexpected_errors"]
                                >= MAX_UNEXPECTED_LINE_ERRORS
                            ):
                                print(
                                    "[t2t] too many unexpected errors; exiting for a restart",
                                    file=sys.stderr,
                                    flush=True,
                                )
                                exit_code = 1
                                raise SystemExit(exit_code)
                    if discarding and b"\n" in buf:
                        # The delimiter that ends the discarded oversized frame has arrived.
                        buf = buf[buf.find(b"\n") + 1 :]
                        discarding = False
                now_mono = time.monotonic()
                if batch and (
                    len(batch) >= BATCH_MAX_LINES
                    or now_mono - last_commit >= BATCH_MAX_SECONDS
                ):
                    if commit_batch() == "transient":
                        transient_failures += 1
                        if transient_failures >= MAX_TRANSIENT_COMMIT_FAILURES:
                            print(
                                "[t2t] database unavailable; exiting for a restart",
                                file=sys.stderr,
                                flush=True,
                            )
                            exit_code = 1
                            break
                        time.sleep(TRANSIENT_BACKOFF_SECONDS)
                    else:
                        transient_failures = 0
                        last_commit = now_mono
                if now_mono - last_heartbeat >= HEARTBEAT_SECONDS:
                    if (
                        counters["lines"] - prev_lines >= MIN_LINES_FOR_HEALTH
                        and counters["committed"] - prev_committed == 0
                    ):
                        zero_commit_heartbeats += 1
                    else:
                        zero_commit_heartbeats = 0
                    prev_lines = counters["lines"]
                    prev_committed = counters["committed"]
                    heartbeat()
                    last_heartbeat = now_mono
                    if zero_commit_heartbeats >= MAX_ZERO_COMMIT_HEARTBEATS:
                        print(
                            "[t2t] lines flowing but nothing committed; exiting for a restart",
                            file=sys.stderr,
                            flush=True,
                        )
                        exit_code = 1
                        break
        finally:
            # Best-effort final flush; a DB outage here loses the in-flight batch (MQTT QoS 0 does not
            # replay it), so report it honestly and make the exit non-zero.
            if commit_batch() == "transient" or batch:
                counters["lost_beliefs"] += len(batch)
                print(
                    f"[t2t] exiting with {len(batch)} uncommitted beliefs lost (DB unavailable)",
                    file=sys.stderr,
                    flush=True,
                )
                batch.clear()
                exit_code = 1
            heartbeat()
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
