"""Pure, FlexMeasures-free logic for the labems-t2t continuous ingester.

Kept import-light (only the standard library), so it can be unit-tested without the FlexMeasures app or a database.
continuous_ingest.py imports this module for every decision that does not touch the ORM.
"""

import json
import logging
import math
import queue
from datetime import timedelta
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler

# Sources that must never be ingested, enforced independently of what the DB happens to bind:
# the inverter SOC estimate is wrong (reads full at ~1/3 charge), and the deye/bms/* group is all zeros.
FORBIDDEN_TOPIC_PREFIXES = ("deye/battery/soc", "deye/bms/")
# The two per-string JK-BMS topic namespaces, used for the data_age staleness gate.
STRING_PREFIXES = ("jkbms/string_a", "jkbms/string_b")
STALE_AGE_SECONDS = 60.0
# If no data_age has been received within this horizon, the string is stale even without a new value,
# so a BLE dropout that stops data_age updates still closes the gate.
STALE_OBSERVATION_HORIZON_SECONDS = 90.0
# A single record longer than this is dropped, so a broken stream cannot exhaust memory on the board.
MAX_LINE_BYTES = 1_000_000
# A small rotating file inside the container mirrors the stderr heartbeat.
# podman exec buffers the relayed stderr and leaves journalctl empty for the running instance.
HEARTBEAT_LOG_PATH = "/tmp/t2t-heartbeat.log"
HEARTBEAT_LOG_MAX_BYTES = 256 * 1024
HEARTBEAT_LOG_BACKUPS = 2

# SQLAlchemy error class names that mean the database or connection is at fault, so the whole batch
# should be retried rather than salvaged row by row.
# Deliberately specific: DBAPIError/DatabaseError are NOT listed, because IntegrityError, DataError,
# and ProgrammingError inherit from them and must classify as deterministic row errors, not transient.
TRANSIENT_DB_ERROR_NAMES = (
    "OperationalError",
    "InterfaceError",
    "InternalError",
    "TimeoutError",
    "DisconnectionError",
    "ResourceClosedError",
)


def is_forbidden(topic):
    """Return True for a topic in the forbidden deny-list (deye/battery/soc, deye/bms/*)."""
    return any(topic == p or topic.startswith(p) for p in FORBIDDEN_TOPIC_PREFIXES)


def string_prefix(topic):
    """Return the jkbms string namespace a topic belongs to, or None."""
    for prefix in STRING_PREFIXES:
        if topic.startswith(prefix + "/"):
            return prefix
    return None


def data_age_topic_for(prefix):
    """Return the data_age state topic for a string prefix."""
    return f"{prefix}/sensor/data_age/state"


DATA_AGE_TOPICS = {data_age_topic_for(p): p for p in STRING_PREFIXES}


def parse_frame(line):
    """Parse one `mosquitto_sub -F '%j'` JSON object into a frame dict, or None if unusable.

    Each message is a single JSON object, so a newline inside the payload is escaped within the JSON string and can never be read as a second record.
    That is what stops a payload from forging a topic and slipping past the deny-list.
    Returns {topic, payload, retain, tst}; payload is None when the message carried no string payload.
    """
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    topic = obj.get("topic")
    if not isinstance(topic, str) or not topic:
        return None
    payload = obj.get("payload")
    payload = payload if isinstance(payload, str) else None
    raw_retain = obj.get("retain")
    if isinstance(raw_retain, bool):
        retain = raw_retain
    elif isinstance(raw_retain, int) and raw_retain in (0, 1):
        retain = bool(raw_retain)
    else:
        # A missing, null, or malformed retain flag fails closed: the frame is treated as retained,
        # so it is skipped and can never open the staleness gate.
        retain = True
    return {
        "topic": topic,
        "payload": payload,
        "retain": retain,
        "tst": obj.get("tst"),
    }


class LineAssembler:
    """Reassemble newline-delimited records from arbitrary byte chunks, with a bounded buffer.

    A record with no delimiter within max_line_bytes, or a complete line longer than it, is dropped and counted, and the assembler resynchronizes at the next delimiter.
    The buffer never grows without bound: while discarding an oversized record it keeps at most one chunk, so a broken or hostile stream cannot exhaust memory.
    """

    def __init__(self, max_line_bytes=MAX_LINE_BYTES):
        self.max_line_bytes = max_line_bytes
        self._buf = b""
        self._discarding = False
        self.dropped = 0

    def feed(self, chunk):
        """Yield each complete record in chunk as a decoded str (UTF-8, invalid bytes replaced)."""
        self._buf += chunk
        if self._discarding:
            newline = self._buf.find(b"\n")
            if newline == -1:
                self._buf = b""  # still no delimiter: drop, stay bounded
                return
            self._buf = self._buf[newline + 1 :]  # resynchronize past the delimiter
            self._discarding = False
        while True:
            newline = self._buf.find(b"\n")
            if newline == -1:
                if len(self._buf) > self.max_line_bytes:
                    # An unterminated oversized record: drop it and discard until the next delimiter.
                    self.dropped += 1
                    self._discarding = True
                    self._buf = b""
                return
            rawline = self._buf[:newline]
            self._buf = self._buf[newline + 1 :]
            if len(rawline) > self.max_line_bytes:
                self.dropped += 1  # a complete but oversized line: drop it, keep going
                continue
            yield rawline.decode("utf-8", "replace")


class StalenessGate:
    """Per-string BLE-staleness gate that expires with elapsed time.

    A string is stale — its readings must be skipped — when any of these hold:
    - no live (non-retained) data_age has been seen yet, or
    - the effective age (last reported age + seconds elapsed since that report) exceeds the limit, or
    - the last data_age observation is itself older than the observation horizon.
    Retained data_age messages are ignored, because their original time is unknown.
    So the gate opens only on a live reading, and closes again on its own during a dropout.
    """

    def __init__(
        self,
        stale_age=STALE_AGE_SECONDS,
        horizon=STALE_OBSERVATION_HORIZON_SECONDS,
    ):
        self.stale_age = stale_age
        self.horizon = horizon
        self._state = {}  # prefix -> (reported_age, receipt_monotonic)

    def note_data_age(self, prefix, payload, now_mono, retained):
        """Record a live data_age observation for a string; retained frames are ignored."""
        if retained:
            return
        try:
            age = float(payload)
        except (TypeError, ValueError):
            age = None
        if age is None or not math.isfinite(age) or age < 0:
            # Unparsable, nan, or negative: the string is not reporting usably, so mark it stale now.
            self._state[prefix] = (self.stale_age + 1.0, now_mono)
        else:
            self._state[prefix] = (age, now_mono)

    def is_stale(self, prefix, now_mono):
        """Return True if the string's readings should be skipped at now_mono."""
        entry = self._state.get(prefix)
        if entry is None:
            return True
        reported_age, receipt_mono = entry
        elapsed = now_mono - receipt_mono
        if elapsed > self.horizon:
            return True
        return (reported_age + elapsed) > self.stale_age


def decide_reading(frame, gate, now_mono):
    """Decide what to do with a parsed frame, mutating the gate for live data_age frames.

    Returns ("accept", float_value) or ("skip", reason).
    Retained frames are skipped entirely, because their original time is unknown, and forbidden topics are dropped before anything else.
    """
    topic = frame["topic"]
    if frame["retain"]:
        return ("skip", "retained")
    if is_forbidden(topic):
        return ("skip", "forbidden")
    payload = frame["payload"]
    da_prefix = DATA_AGE_TOPICS.get(topic)
    if da_prefix is not None:
        gate.note_data_age(da_prefix, payload, now_mono, retained=False)
    if payload is None:
        return ("skip", "malformed")
    try:
        value = float(payload)
    except ValueError:
        return ("skip", "nonnumeric")
    if not math.isfinite(value):
        return ("skip", "nonfinite")
    prefix = string_prefix(topic)
    if prefix is not None and gate.is_stale(prefix, now_mono):
        return ("skip", "stale")
    return ("accept", value)


def classify_db_error(exc):
    """Classify a database error as 'transient' (retry the batch) or 'row' (salvage row by row)."""
    # A DBAPIError that invalidated the connection is transient even when its class name is not listed,
    # e.g. a ProgrammingError raised because the connection dropped mid-statement.
    if getattr(exc, "connection_invalidated", False):
        return "transient"
    for cls in type(exc).__mro__:
        if cls.__name__ in TRANSIENT_DB_ERROR_NAMES:
            return "transient"
    return "row"


def salvage_batch(rows, commit_one, rollback):
    """Commit rows one at a time after a batch commit failed, classifying each row's outcome.

    commit_one(row) commits a single row or raises; rollback() undoes a failed row.
    Returns (committed, lost, remaining, status).
    status is 'transient' when a row fails with a transient error: salvage stops, and remaining holds that row and the rest for a retry.
    status is 'systematic' when nothing committed and there were rows: a systematic fault (e.g. a missing table) to surface, not to swallow.
    status is 'ok' otherwise; a row that fails non-transiently is counted as lost.
    """
    committed = 0
    lost = 0
    for index, row in enumerate(rows):
        try:
            commit_one(row)
            committed += 1
        except Exception as exc:
            rollback()
            if classify_db_error(exc) == "transient":
                return committed, lost, list(rows[index:]), "transient"
            lost += 1
    if committed == 0 and rows:
        return committed, lost, [], "systematic"
    return committed, lost, [], "ok"


def next_monotonic_stamp(last_ts, sensor_id, now):
    """Return an arrival stamp strictly greater than the last one used for this sensor.

    This keeps event_start monotonic per sensor even across a restart or a backward clock step, so a belief never collides with a stored one on the TimedBelief primary key.
    Mutates last_ts.
    """
    earliest = last_ts.get(sensor_id)
    if earliest is not None and now <= earliest:
        now = earliest + timedelta(microseconds=1)
    last_ts[sensor_id] = now
    return now


def make_heartbeat_logger(
    path=HEARTBEAT_LOG_PATH,
    max_bytes=HEARTBEAT_LOG_MAX_BYTES,
    backups=HEARTBEAT_LOG_BACKUPS,
):
    """Return (logger, listener) for a non-blocking rotating-file heartbeat sink.

    Records go through a QueueHandler onto an in-memory queue, and a background QueueListener thread does the file I/O,
    so a slow or blocked disk write can never stall the caller.
    This is a secondary sink alongside the stderr prints, so the heartbeat and notices are readable live inside the container,
    even though `podman exec` buffers the relayed stderr.
    It changes nothing about ingestion; it only mirrors the messages.
    """
    # Never let logging raise or print its own diagnostics into the ingester's stderr.
    logging.raiseExceptions = False
    file_handler = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    log_queue = queue.Queue(-1)
    listener = QueueListener(log_queue, file_handler, respect_handler_level=False)
    listener.start()  # QueueListener runs its monitor thread as a daemon.
    logger = logging.getLogger("t2t-heartbeat")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for existing in list(logger.handlers):
        logger.removeHandler(existing)
    logger.addHandler(QueueHandler(log_queue))
    return logger, listener
