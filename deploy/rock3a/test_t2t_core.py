"""Self-contained regression tests for the labems-t2t pure logic (t2t_core).

These import only t2t_core (standard-library only), so they run without the FlexMeasures app or a database.
Run them with `pytest deploy/rock3a/test_t2t_core.py`.
They cover the safety-critical behaviours a review flagged: JSON framing (embedded-newline payloads cannot forge a topic), the deny-list, elapsed-time staleness,
retained-skip, monotonic-stamp seeding, and database-error classification.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import t2t_core  # noqa: E402


def _frame(topic, payload, retain=0):
    return t2t_core.parse_frame(
        json.dumps({"topic": topic, "payload": payload, "retain": retain})
    )


# --- JSON framing: a newline inside a payload can never become a second record / forge a topic ---


def test_embedded_newline_payload_stays_one_frame():
    line = json.dumps(
        {"topic": "deye/ac/frequency", "payload": "50.0\nfake/topic 999", "retain": 0}
    )
    frame = t2t_core.parse_frame(line)
    assert frame["topic"] == "deye/ac/frequency"
    assert "\n" in frame["payload"]  # the newline is data, not a record boundary
    status, reason = t2t_core.decide_reading(frame, t2t_core.StalenessGate(), 0.0)
    assert (status, reason) == (
        "skip",
        "nonnumeric",
    )  # the injected "fake/topic" never becomes a topic


def test_payload_cannot_forge_a_forbidden_topic():
    frame = _frame("deye/ac/frequency", "deye/battery/soc 100")
    status, reason = t2t_core.decide_reading(frame, t2t_core.StalenessGate(), 0.0)
    assert (status, reason) == ("skip", "nonnumeric")  # never turns into a soc write


def test_parse_frame_rejects_junk():
    assert t2t_core.parse_frame("") is None
    assert t2t_core.parse_frame("not json") is None
    assert t2t_core.parse_frame("[1,2,3]") is None
    assert t2t_core.parse_frame(json.dumps({"payload": "5"})) is None  # no topic


# --- deny-list ---


def test_deny_list():
    assert t2t_core.is_forbidden("deye/battery/soc")
    assert t2t_core.is_forbidden("deye/bms/1/voltage")
    assert not t2t_core.is_forbidden("deye/battery/power")
    status, reason = t2t_core.decide_reading(
        _frame("deye/bms/1/soc", "0"), t2t_core.StalenessGate(), 0.0
    )
    assert (status, reason) == ("skip", "forbidden")


# --- elapsed-time staleness: a string goes stale from elapsed time even with no new data_age ---


def test_stale_from_elapsed_time_without_new_data_age():
    gate = t2t_core.StalenessGate()
    gate.note_data_age("jkbms/string_a", "5", now_mono=1000.0, retained=False)
    assert gate.is_stale("jkbms/string_a", 1000.0) is False  # fresh: 5 s
    assert gate.is_stale("jkbms/string_a", 1050.0) is False  # 5 + 50 = 55 < 60
    assert (
        gate.is_stale("jkbms/string_a", 1058.0) is True
    )  # 5 + 58 = 63 > 60, no new data_age needed


def test_reading_skipped_when_string_stale():
    gate = t2t_core.StalenessGate()
    # No data_age seen yet -> fail closed.
    status, reason = t2t_core.decide_reading(
        _frame("jkbms/string_b/sensor/total_voltage/state", "48.9"), gate, 0.0
    )
    assert (status, reason) == ("skip", "stale")
    # Fresh data_age opens the gate.
    gate.note_data_age("jkbms/string_b", "3", now_mono=0.0, retained=False)
    status, value = t2t_core.decide_reading(
        _frame("jkbms/string_b/sensor/total_voltage/state", "48.9"), gate, 0.0
    )
    assert status == "accept" and value == 48.9


def test_nan_and_negative_data_age_mark_stale():
    gate = t2t_core.StalenessGate()
    gate.note_data_age("jkbms/string_a", "5", 0.0, retained=False)
    gate.note_data_age("jkbms/string_a", "nan", 1.0, retained=False)
    assert gate.is_stale("jkbms/string_a", 1.0) is True
    gate.note_data_age("jkbms/string_a", "3", 2.0, retained=False)
    assert gate.is_stale("jkbms/string_a", 2.0) is False
    gate.note_data_age("jkbms/string_a", "-1", 3.0, retained=False)
    assert gate.is_stale("jkbms/string_a", 3.0) is True


# --- retained-skip: retained frames are dropped and never open the gate ---


def test_retained_frames_skipped_and_do_not_open_gate():
    gate = t2t_core.StalenessGate()
    # A retained data_age must NOT open the gate.
    status, reason = t2t_core.decide_reading(
        _frame("jkbms/string_a/sensor/data_age/state", "1", retain=1), gate, 0.0
    )
    assert (status, reason) == ("skip", "retained")
    assert gate.is_stale("jkbms/string_a", 0.0) is True  # still closed
    # A retained sensor reading is dropped outright.
    status, reason = t2t_core.decide_reading(
        _frame("deye/ac/frequency", "50.0", retain=1), gate, 0.0
    )
    assert (status, reason) == ("skip", "retained")


# --- monotonic-stamp seeding: survives a restart / backward clock step ---


def test_monotonic_stamp_bumps_past_seed():
    seed = datetime(2026, 9, 3, 8, 0, 0, tzinfo=timezone.utc)
    last_ts = {7: seed}  # as if seeded from the DB
    now = seed - timedelta(seconds=5)  # a backward clock step
    stamped = t2t_core.next_monotonic_stamp(last_ts, 7, now)
    assert stamped == seed + timedelta(microseconds=1)
    again = t2t_core.next_monotonic_stamp(last_ts, 7, now)
    assert again == seed + timedelta(microseconds=2)  # strictly increasing


def test_monotonic_stamp_passes_through_when_ahead():
    last_ts = {}
    now = datetime(2026, 9, 3, 8, 0, 0, tzinfo=timezone.utc)
    assert t2t_core.next_monotonic_stamp(last_ts, 7, now) == now


# --- database-error classification ---


def test_classify_db_error_by_mro_names():
    # Mimic the SQLAlchemy hierarchy so this runs without the library and still catches the regression:
    # if DBAPIError were treated as transient, IntegrityError (which inherits it) would be misclassified.
    class DBAPIError(Exception):
        pass

    class DatabaseError(DBAPIError):
        pass

    class OperationalError(DatabaseError):
        pass

    class IntegrityError(DatabaseError):
        pass

    assert t2t_core.classify_db_error(OperationalError()) == "transient"
    assert (
        t2t_core.classify_db_error(IntegrityError()) == "row"
    )  # MRO includes DBAPIError, still row
    assert t2t_core.classify_db_error(DBAPIError()) == "row"


def test_classify_db_error_with_real_sqlalchemy():
    exc = pytest.importorskip("sqlalchemy.exc")

    def make(cls):
        return cls("stmt", {}, Exception("orig"))

    assert t2t_core.classify_db_error(make(exc.OperationalError)) == "transient"
    assert t2t_core.classify_db_error(make(exc.InterfaceError)) == "transient"
    # These inherit from DBAPIError but are deterministic row faults, not transient.
    assert t2t_core.classify_db_error(make(exc.IntegrityError)) == "row"
    assert t2t_core.classify_db_error(make(exc.DataError)) == "row"
    assert t2t_core.classify_db_error(make(exc.ProgrammingError)) == "row"


# --- byte framing: bounded reassembly, oversized drop, discard-resync ---


def test_line_assembler_reassembles_across_chunks():
    a = t2t_core.LineAssembler()
    assert list(a.feed(b"deye/ac/fre")) == []
    assert list(a.feed(b"q 50\ndeye/ac/x 1\n")) == ["deye/ac/freq 50", "deye/ac/x 1"]


def test_line_assembler_drops_oversized_complete_line_but_keeps_others():
    a = t2t_core.LineAssembler(max_line_bytes=8)
    out = list(a.feed(b"waytoolongline 1\nok 2\n"))
    assert out == ["ok 2"]
    assert a.dropped == 1


def test_line_assembler_discards_unterminated_oversized_bounded_then_resyncs():
    a = t2t_core.LineAssembler(max_line_bytes=8)
    assert list(a.feed(b"0123456789")) == []  # >8 bytes, no newline
    assert a.dropped == 1
    assert a._buf == b""  # buffer stays bounded while discarding
    assert list(a.feed(b"still-no-newline-here")) == []
    assert a._buf == b""  # still bounded
    assert list(a.feed(b"tail\ngood 1\n")) == ["good 1"]  # resync past the delimiter


# --- classify: a connection-invalidating error is transient even with a non-transient class name ---


def test_classify_connection_invalidated_is_transient():
    class ProgrammingError(Exception):  # a name that classifies as a row fault
        pass

    exc = ProgrammingError("x")
    assert t2t_core.classify_db_error(exc) == "row"
    exc.connection_invalidated = True
    assert t2t_core.classify_db_error(exc) == "transient"


# --- salvage_batch: systematic failure is surfaced and lost rows are counted ---


def test_salvage_batch_all_ok():
    committed, lost, remaining, status = t2t_core.salvage_batch(
        [1, 2, 3], lambda r: None, lambda: None
    )
    assert (committed, lost, remaining, status) == (3, 0, [], "ok")


def test_salvage_batch_systematic_when_all_fail_nontransiently():
    class ProgrammingError(Exception):
        pass

    def commit_one(row):
        raise ProgrammingError("missing table")

    committed, lost, remaining, status = t2t_core.salvage_batch(
        [1, 2, 3], commit_one, lambda: None
    )
    assert committed == 0 and lost == 3 and remaining == [] and status == "systematic"


def test_salvage_batch_transient_stops_and_retains_remainder():
    class OperationalError(Exception):
        pass

    def commit_one(row):
        if row == 2:
            raise OperationalError("connection reset")

    committed, lost, remaining, status = t2t_core.salvage_batch(
        [1, 2, 3], commit_one, lambda: None
    )
    assert (
        committed == 1 and lost == 0 and remaining == [2, 3] and status == "transient"
    )


def test_salvage_batch_partial_counts_lost_rows():
    class ProgrammingError(Exception):
        pass

    def commit_one(row):
        if row == 2:
            raise ProgrammingError("bad row")

    committed, lost, remaining, status = t2t_core.salvage_batch(
        [1, 2, 3], commit_one, lambda: None
    )
    assert committed == 2 and lost == 1 and remaining == [] and status == "ok"


# --- retain fails closed on a malformed/missing flag ---


def test_parse_frame_retain_fail_closed():
    def retain_of(value):
        return t2t_core.parse_frame(
            json.dumps({"topic": "t", "payload": "1", "retain": value})
        )["retain"]

    assert retain_of(0) is False
    assert retain_of(1) is True
    assert retain_of(True) is True
    assert retain_of(False) is False
    assert retain_of(None) is True  # fail closed
    assert retain_of(2) is True  # fail closed
    assert retain_of("1") is True  # fail closed
    assert retain_of(1.0) is True  # a float fails closed
    # A missing retain key also fails closed.
    assert (
        t2t_core.parse_frame(json.dumps({"topic": "t", "payload": "1"}))["retain"]
        is True
    )


def test_retain_null_data_age_does_not_open_gate():
    gate = t2t_core.StalenessGate()
    da = json.dumps(
        {
            "topic": "jkbms/string_a/sensor/data_age/state",
            "payload": "1",
            "retain": None,
        }
    )
    status, reason = t2t_core.decide_reading(t2t_core.parse_frame(da), gate, 0.0)
    assert (status, reason) == ("skip", "retained")
    assert (
        gate.is_stale("jkbms/string_a", 0.0) is True
    )  # the null-retain frame did NOT open the gate
    gate.note_data_age("jkbms/string_a", "1", 0.0, retained=False)
    assert (
        gate.is_stale("jkbms/string_a", 0.0) is False
    )  # a valid live data_age then opens it


# --- heartbeat logger: writes a readable file and rotates, without duplicate handlers ---


def test_heartbeat_logger_writes_and_rotates(tmp_path):
    path = str(tmp_path / "hb.log")
    logger, listener = t2t_core.make_heartbeat_logger(
        path=path, max_bytes=200, backups=2
    )
    try:
        for i in range(50):
            logger.info(f"[t2t] hb line {i} " + "x" * 40)
    finally:
        listener.stop()  # drain the queue and flush before asserting.
    assert os.path.exists(path)  # the primary file is present.
    assert "[t2t] hb line" in open(path).read()
    assert os.path.exists(path + ".1")  # the small max_bytes forced a rotation.
    logger2, listener2 = t2t_core.make_heartbeat_logger(
        path=path, max_bytes=200, backups=2
    )
    listener2.stop()
    assert len(logger2.handlers) == 1  # re-creating must not stack duplicate handlers.
