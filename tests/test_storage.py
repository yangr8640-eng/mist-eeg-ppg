"""Recording integrity tests, including receive-time boundaries and disk failure."""
from __future__ import annotations

import csv
import json
import queue
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Barrier

import pytest

from mist_app.clock import SessionClock
from mist_app.models import DeviceMessage, Stage
from mist_app.storage import SessionRecorder, safe_id


def read_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def make_recorder(path, **kwargs):
    return SessionRecorder(path, {"id": "P001", "age": 25, "sex": "其他"},
                           [Stage("eyes_open", "睁眼静息", 1)], True, SessionClock(), **kwargs)


def ppg(at, value=42):
    return DeviceMessage("ppg", "packet", at, f"{value}\n".encode(), ((value,),))


def temperature(at, value=36.25):
    return DeviceMessage("temperature", "packet", at, f"{value}\r\n".encode(), ((value,),))


@pytest.fixture
def frozen_session_datetime(monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 21, 23, 59, 59, 123456, tzinfo=tz)

    monkeypatch.setattr("mist_app.storage.datetime", FrozenDateTime)


@pytest.mark.parametrize("simulate", [False, True])
@pytest.mark.parametrize("participant_sex, expected_sex", [
    ({"sex": "male"}, "男"),
    ({"sex": "female"}, "女"),
    ({"sex": "unspecified"}, "其他"),
    ({"sex": "other"}, "其他"),
    ({"sex": "男"}, "男"),
    ({"sex": "女"}, "女"),
    ({"sex": "其他"}, "其他"),
    ({"sex": "其他 / 不愿透露"}, "其他"),
    ({"gender": "female"}, "女"),
])
def test_session_directory_uses_participant_sex_and_local_datetime(
        tmp_path, frozen_session_datetime, participant_sex, expected_sex, simulate):
    participant = {"id": "P001", "age": 25, **participant_sex}
    recorder = SessionRecorder(tmp_path, participant, [], simulate, SessionClock())
    try:
        parent = tmp_path / "SIMULATED" if simulate else tmp_path
        assert recorder.path == parent / f"P001_{expected_sex}_20260921_235959_123456"
        manifest = read_json(recorder.path / "session.json")
        assert manifest["participant"] == participant
        assert manifest["mode"] == ("simulate" if simulate else "hardware")
    finally:
        recorder.shutdown()


def test_session_directory_pads_time_and_preserves_six_microsecond_digits(tmp_path, monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 21, 1, 2, 3, 4, tzinfo=tz)

    monkeypatch.setattr("mist_app.storage.datetime", FrozenDateTime)
    recorder = make_recorder(tmp_path)
    try:
        assert recorder.path.name == "P001_其他_20260921_010203_000004"
    finally:
        recorder.shutdown()


def test_same_timestamp_repeated_sessions_preserve_all_previous_files(tmp_path, frozen_session_datetime):
    sessions = []
    snapshots = []
    for number in range(1, 4):
        recorder = make_recorder(tmp_path)
        try:
            stage = recorder.prepare(Stage("eyes_open", "睁眼静息", 1), 1)
            stage.start_ns, stage.end_ns, stage.status = 100, 200, "completed"
            assert recorder.packet(ppg(150, number), 1)
            assert recorder.finish_stage(stage).wait(3)
            assert recorder.finish("completed").wait(3)
            assert not recorder.error
        finally:
            recorder.shutdown()
        suffix = "" if number == 1 else f"_{number:02d}"
        assert recorder.path.name == f"P001_其他_20260921_235959_123456{suffix}"
        assert read_csv(stage.path / "eyes_open_ppg.csv")[0]["red"] == str(number)
        sessions.append(recorder)
        snapshots.append({p.relative_to(recorder.path): p.read_bytes()
                          for p in recorder.path.rglob("*") if p.is_file()})
        for previous, snapshot in zip(sessions, snapshots):
            assert {p.relative_to(previous.path): p.read_bytes()
                    for p in previous.path.rglob("*") if p.is_file()} == snapshot
    assert len({recorder.session_id for recorder in sessions}) == 3
    assert len({read_json(recorder.path / "session.json")["session_id"]
                for recorder in sessions}) == 3


def test_concurrent_sessions_claim_distinct_directories_atomically(
        tmp_path, frozen_session_datetime, monkeypatch):
    base = tmp_path / "SIMULATED" / "P001_其他_20260921_235959_123456"
    contenders = Barrier(4)
    original_mkdir = Path.mkdir

    def synchronized_mkdir(path, *args, **kwargs):
        if path == base:
            contenders.wait(timeout=5)
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", synchronized_mkdir)

    def record(value):
        recorder = make_recorder(tmp_path)
        try:
            stage = recorder.prepare(Stage("eyes_open", "睁眼静息", 1), 1)
            stage.start_ns, stage.end_ns, stage.status = 100, 200, "completed"
            assert recorder.packet(ppg(150, value), 1)
            assert recorder.finish_stage(stage).wait(3)
            assert recorder.finish("completed").wait(3)
            assert not recorder.error
        finally:
            recorder.shutdown()
        return recorder, stage, value

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(record, range(1, 5)))
    assert {recorder.path.name for recorder, _, _ in results} == {
        "P001_其他_20260921_235959_123456", "P001_其他_20260921_235959_123456_02",
        "P001_其他_20260921_235959_123456_03", "P001_其他_20260921_235959_123456_04",
    }
    assert len({recorder.session_id for recorder, _, _ in results}) == 4
    for recorder, stage, value in results:
        assert read_json(recorder.path / "session.json")["session_id"] == recorder.session_id
        assert [row["red"] for row in read_csv(stage.path / "eyes_open_ppg.csv")] == [str(value)]


def test_session_directory_skips_existing_directories_and_files(tmp_path, frozen_session_datetime):
    base = tmp_path / "SIMULATED" / "P001_其他_20260921_235959_123456"
    base.mkdir(parents=True)
    existing_record = base / "session.json"
    existing_record.write_bytes(b"previous participant data")
    existing_file = base.with_name(base.name + "_02")
    existing_file.write_bytes(b"existing file must also survive")
    recorder = make_recorder(tmp_path)
    try:
        assert recorder.path == base.with_name(base.name + "_03")
        assert existing_record.read_bytes() == b"previous participant data"
        assert list(base.iterdir()) == [existing_record]
        assert existing_file.read_bytes() == b"existing file must also survive"
    finally:
        recorder.shutdown()


def test_session_directory_permission_failure_is_not_treated_as_collision(
        tmp_path, frozen_session_datetime, monkeypatch):
    base = tmp_path / "SIMULATED" / "P001_其他_20260921_235959_123456"
    original_mkdir = Path.mkdir
    attempts = []

    def deny_session_mkdir(path, *args, **kwargs):
        if path.parent == base.parent and path.name.startswith(base.name):
            attempts.append(path)
            raise PermissionError("injected session directory permission failure")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", deny_session_mkdir)
    with pytest.raises(PermissionError, match="injected session directory permission failure"):
        make_recorder(tmp_path)
    assert attempts == [base]


def test_receive_window_excludes_before_and_exact_end_and_keeps_delayed_queue(tmp_path):
    recorder = make_recorder(tmp_path)
    try:
        window = recorder.prepare(Stage("eyes_open", "睁眼静息", 1), 1)
        window.start_ns = recorder.clock.anchor_ns + 100_000_000
        window.end_ns = window.start_ns + 1_000_000_000
        window.status = "recording"
        # Queue delayed payloads after their timestamps; ownership uses reception.
        times = [window.start_ns - 1, window.start_ns, window.end_ns - 1, window.end_ns]
        for sequence, at in enumerate(times, 1):
            assert recorder.packet(ppg(at, sequence), sequence)
        window.status = "completed"
        assert recorder.finish_stage(window).wait(3)
        assert not recorder.error
        rows = read_csv(window.path / "eyes_open_ppg.csv")
        assert [int(row["received_ns"]) for row in rows] == times[1:3]
        assert [int(row["red"]) for row in rows] == [2, 3]
        raw = [json.loads(line) for line in (window.path / "eyes_open_ppg_raw.jsonl").read_text().splitlines()]
        assert [line["received_ns"] for line in raw] == times[1:3]
        assert recorder.finish("completed").wait(3)
        quality = read_json(recorder.path / "quality_report.json")
        assert quality["hardware_alignment_error_ms"] is None
        assert quality["attempts"][0]["quality"]["ppg"]["samples"] == 2
        assert recorder.path.parent.name == "SIMULATED"
    finally:
        recorder.shutdown()


def test_eeg_sample_time_estimate_and_raw_units_are_explicit(tmp_path):
    recorder = make_recorder(tmp_path)
    try:
        window = recorder.prepare(Stage("eyes_open", "睁眼静息", 1), 1)
        window.start_ns = recorder.clock.anchor_ns
        window.end_ns = window.start_ns + 1_000_000_000
        at = window.start_ns + 5_000_000
        samples = tuple((index, -index, -8388608, 8388607) for index in range(8))
        packet = DeviceMessage("eeg", "packet", at, b"raw-wire", samples,
                               meta={"electrode_raw": 1, "electrode_off": True, "battery_raw": 170})
        assert recorder.packet(packet, 1)
        window.status = "completed"
        assert recorder.finish_stage(window).wait(3)
        rows = read_csv(window.path / "eyes_open_eeg.csv")
        assert len(rows) == 8
        assert [int(row["estimated_sample_ns"]) for row in rows] == [at - (7-i)*2_000_000 for i in range(8)]
        assert all(int(row["received_ns"]) == at for row in rows)
        assert float(rows[0]["ch3_uv"]) == -25000
        assert int(rows[0]["ch3_raw"]) == -8388608
        assert float(rows[0]["estimated_stage_seconds"]) < 0  # Estimated acquisition can predate receive window.
        assert all(row["timing_basis"] == "estimate_from_packet_receive_500Hz" for row in rows)
        assert all(row["electrode_off"] == "True" for row in rows)
    finally:
        recorder.shutdown()


def test_retries_preserve_closed_files_and_never_overwrite_attempt(tmp_path):
    recorder = make_recorder(tmp_path)
    try:
        stage = Stage("eyes_open", "睁眼静息", 1)
        first = recorder.prepare(stage, 1)
        first.start_ns, first.end_ns, first.status = 100, 200, "incomplete"
        recorder.packet(ppg(150, 1), 1)
        assert recorder.finish_stage(first).wait(3)
        before = (first.path / "eyes_open_ppg.csv").read_bytes()
        second = recorder.prepare(stage, 2)
        second.start_ns, second.end_ns, second.status = 200, 300, "completed"
        recorder.packet(ppg(250, 2), 2)
        assert recorder.finish_stage(second).wait(3)
        assert first.path != second.path
        assert (first.path / "eyes_open_ppg.csv").read_bytes() == before
        assert read_csv(second.path / "eyes_open_ppg.csv")[0]["red"] == "2"
        with pytest.raises(OSError):
            recorder.prepare(stage, 1)
        assert (first.path / "eyes_open_ppg.csv").read_bytes() == before
    finally:
        recorder.shutdown()


def test_temperature_values_boundaries_raw_errors_and_closed_manifest(tmp_path):
    config = {"port": "COM12", "baudrate": 9600, "measurement_site": "左手食指"}
    devices = ("eeg", "ppg", "temperature")
    recorder = make_recorder(tmp_path, enabled_devices=devices, temperature_config=config)
    try:
        window = recorder.prepare(Stage("eyes_open", "睁眼静息", 4), 1)
        window.start_ns = recorder.clock.anchor_ns + 100_000_000
        window.end_ns = window.start_ns + 4_000_000_000
        window.status = "recording"
        times = [window.start_ns - 1, window.start_ns, window.start_ns + 1_000_000_000,
                 window.start_ns + 2_000_000_000, window.end_ns - 1, window.end_ns]
        values = [99.0, 35.21, 35.22, 35.24, 35.26, 99.0]
        for sequence, (at, value) in enumerate(zip(times, values), 1):
            assert recorder.packet(temperature(at, value), sequence)
        invalid = DeviceMessage("temperature", "packet", window.start_ns + 2_500_000_000,
                                b"invalid-wire\x00\xff", error="invalid temperature packet",
                                meta={"parse_errors": 1})
        assert recorder.packet(invalid, 7)
        window.status = "completed"
        assert recorder.finish_stage(window).wait(3)
        assert recorder.finish("completed").wait(3)
        assert not recorder.error
    finally:
        recorder.shutdown()

    rows = read_csv(window.path / "eyes_open_temperature.csv")
    assert [float(row["temperature_c"]) for row in rows] == values[1:-1]
    assert [int(row["received_ns"]) for row in rows] == times[1:-1]
    assert [int(row["host_sequence"]) for row in rows] == [2, 3, 4, 5]
    assert float(rows[0]["session_seconds"]) == pytest.approx(.1)
    assert float(rows[0]["stage_seconds"]) == 0
    assert all(row["timing_basis"] == "host_receive" for row in rows)
    assert all(row["sample_in_packet"] == "0" for row in rows)
    assert all(row["stage"] == "eyes_open" and row["attempt"] == "1" for row in rows)
    raw = [json.loads(line) for line in
           (window.path / "eyes_open_temperature_raw.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(raw) == 5
    assert raw[-1]["raw_hex"] == invalid.raw.hex()
    assert raw[-1]["error"] == invalid.error
    assert raw[-1]["sample_count"] == 0
    assert raw[-1]["meta"] == {"parse_errors": 1}
    assert recorder.saved_counts == {"eeg": 0, "ppg": 0, "temperature": 4}

    manifest = read_json(recorder.path / "session.json")
    assert manifest["enabled_devices"] == list(devices)
    assert manifest["status"] == "completed"
    assert manifest["temperature"]["config"] == config
    assert manifest["temperature"]["measurement"] == "contact_surface_temperature"
    assert "不代表核心体温" in manifest["temperature"]["measurement_note"]
    assert manifest["temperature"]["timing_basis"] == "host_receive"
    quality = read_json(recorder.path / "quality_report.json")
    assert quality["enabled_devices"] == list(devices)
    temperature_quality = quality["attempts"][0]["quality"]["temperature"]
    assert temperature_quality["samples"] == 4
    assert temperature_quality["packets_or_reads"] == 5
    assert temperature_quality["invalid_or_non_sample_reads"] == 1
    assert temperature_quality["parse_errors"] == 1
    assert temperature_quality["receive_gap_threshold_seconds"] == 3
    assert temperature_quality["receive_gaps_at_least_threshold"] == 0
    assert "receive_gaps_at_least_100ms" not in temperature_quality
    assert temperature_quality["exact_device_loss_count"] is None


def test_temperature_one_hz_quality_distinguishes_long_receive_gaps(tmp_path):
    recorder = make_recorder(tmp_path, enabled_devices=("eeg", "ppg", "temperature"))
    try:
        window = recorder.prepare(Stage("eyes_open", "睁眼静息", 10), 1)
        window.start_ns = recorder.clock.anchor_ns
        window.end_ns = window.start_ns + 10_000_000_000
        for sequence, offset in enumerate((0, 1, 2, 5), 1):
            assert recorder.packet(temperature(window.start_ns + offset * 1_000_000_000), sequence)
        assert recorder.finish_stage(window).wait(3)
        quality = read_json(window.path / "stage.json")["quality"]
        assert quality["temperature"]["maximum_receive_gap_seconds"] == 3
        assert quality["temperature"]["receive_gaps_at_least_threshold"] == 1
        assert quality["temperature"]["observed_sample_rate_hz"] == pytest.approx(.6)
        for device in ("eeg", "ppg"):
            assert quality[device]["receive_gap_threshold_seconds"] == .1
            assert quality[device]["receive_gaps_at_least_100ms"] == 0
    finally:
        recorder.shutdown()


def test_temperature_retries_preserve_first_attempt_and_reset_counts(tmp_path):
    recorder = make_recorder(tmp_path, enabled_devices=("eeg", "ppg", "temperature"))
    try:
        stage = Stage("eyes_open", "睁眼静息", 1)
        first = recorder.prepare(stage, 1)
        first.start_ns, first.end_ns, first.status = 100, 200, "incomplete"
        first.reason = "温度传感器掉线"
        assert recorder.packet(temperature(150, 34.1), 1)
        assert recorder.packet(temperature(160, 34.2), 2)
        assert recorder.finish_stage(first).wait(3)
        assert recorder.saved_counts["temperature"] == 2
        before = (first.path / "eyes_open_temperature.csv").read_bytes()
        second = recorder.prepare(stage, 2)
        assert recorder.saved_counts["temperature"] == 0
        second.start_ns, second.end_ns, second.status = 200, 300, "completed"
        assert recorder.packet(temperature(199, 99.0), 3)  # Old attempt's late queue entry.
        assert recorder.packet(temperature(250, 34.3), 4)
        assert recorder.finish_stage(second).wait(3)
        assert recorder.finish("completed").wait(3)
        assert recorder.saved_counts["temperature"] == 1
        assert (first.path / "eyes_open_temperature.csv").read_bytes() == before
        assert read_csv(second.path / "eyes_open_temperature.csv")[0]["temperature_c"] == "34.3"
        attempts = read_json(recorder.path / "session.json")["attempts"]
        assert [item["status"] for item in attempts] == ["incomplete", "completed"]
        assert [item["quality"]["temperature"]["samples"] for item in attempts] == [2, 1]
        assert attempts[0]["reason"] == "温度传感器掉线"
        assert not recorder.error
    finally:
        recorder.shutdown()


def test_default_two_devices_ignore_temperature_and_create_no_temperature_files(tmp_path):
    recorder = make_recorder(tmp_path)
    try:
        window = recorder.prepare(Stage("eyes_open", "睁眼静息", 1), 1)
        window.start_ns, window.end_ns, window.status = 100, 200, "completed"
        assert recorder.packet(temperature(150), 1)
        assert recorder.packet(ppg(160), 2)
        assert recorder.finish_stage(window).wait(3)
        assert recorder.finish("completed").wait(3)
        assert not recorder.error
        assert recorder.saved_counts == {"eeg": 0, "ppg": 1}
        assert not list(window.path.glob("*temperature*"))
        manifest = read_json(recorder.path / "session.json")
        assert manifest["enabled_devices"] == ["eeg", "ppg"]
        assert "temperature" not in manifest
        assert set(manifest["attempts"][0]["quality"]) == {"eeg", "ppg"}
    finally:
        recorder.shutdown()


def test_write_failure_is_latched_and_existing_stage_is_incomplete(tmp_path, monkeypatch):
    recorder = make_recorder(tmp_path)
    window = recorder.prepare(Stage("eyes_open", "睁眼静息", 1), 1)
    window.start_ns, window.end_ns, window.status = 100, 200, "recording"

    def disk_full(*_args, **_kwargs):
        raise OSError("injected disk full")

    monkeypatch.setattr(recorder, "_write_packet", disk_full)
    try:
        recorder.packet(ppg(150), 1)
        recorder._thread.join(timeout=3)
        assert not recorder._thread.is_alive()
        assert "injected disk full" in recorder.error
        assert recorder.packet(ppg(160), 2) is False
        assert recorder.finish_stage(window).is_set()
        assert read_json(window.path / "stage.json")["status"] == "incomplete"
        assert read_json(recorder.path / "session.json")["status"] == "error"
    finally:
        recorder.shutdown()


def test_queue_full_latches_failure_and_preserves_incomplete_attempt(tmp_path, monkeypatch):
    recorder = make_recorder(tmp_path)
    window = recorder.prepare(Stage("eyes_open", "睁眼静息", 1), 1)
    window.start_ns, window.end_ns, window.status = 100, 200, "recording"

    def full(_item):
        raise queue.Full

    monkeypatch.setattr(recorder._queue, "put_nowait", full)
    assert recorder.packet(ppg(150), 1) is False
    assert "队列已满" in recorder.error
    assert recorder.finish_stage(window).is_set()
    recorder._thread.join(timeout=3)
    assert not recorder._thread.is_alive()  # Error metadata must persist without closing the GUI.
    assert read_json(window.path / "stage.json")["status"] == "incomplete"
    assert read_json(recorder.path / "session.json")["status"] == "error"
    recorder.shutdown()


def test_prepare_on_closed_recorder_fails_instead_of_returning_missing_directory(tmp_path):
    recorder = make_recorder(tmp_path)
    recorder.shutdown()
    with pytest.raises(OSError, match="关闭"):
        recorder.prepare(Stage("eyes_open", "睁眼静息", 1), 1)


def test_failed_stage_metadata_uses_failure_time_not_future_deadline(tmp_path, monkeypatch):
    recorder = make_recorder(tmp_path)
    window = recorder.prepare(Stage("eyes_open", "睁眼静息", 180), 1)
    window.start_ns = time.perf_counter_ns()
    planned_end = window.start_ns + 180_000_000_000
    window.end_ns, window.status = planned_end, "recording"
    def disk_full(*args):
        raise OSError("injected write failure")
    monkeypatch.setattr(recorder, "_write_packet", disk_full)
    try:
        recorder.packet(ppg(time.perf_counter_ns()), 1)
        recorder._thread.join(timeout=3)
        metadata = read_json(window.path / "stage.json")
        assert metadata["status"] == "incomplete"
        assert window.start_ns <= metadata["end_ns"] <= time.perf_counter_ns()
        assert metadata["end_ns"] < planned_end
        assert metadata["duration_seconds"] < 3
    finally:
        recorder.shutdown()


def test_session_clock_ignores_wall_clock_jump(monkeypatch):
    monkeypatch.setattr(time, "time_ns", lambda: 123_000_000_000)
    clock = SessionClock()
    metadata = clock.metadata()
    assert clock.relative(clock.anchor_ns + 1_250_000_000) == 1.25
    monkeypatch.setattr(time, "time_ns", lambda: 987_000_000_000)
    assert clock.metadata() == metadata
    assert clock.relative(clock.anchor_ns + 1_250_000_000) == 1.25


@pytest.mark.parametrize("identifier", ["../P001", "P/001", "P\\001", "", "P:001", "P001.", "\x00P001"])
def test_unsafe_participant_names_rejected(identifier):
    with pytest.raises(ValueError):
        safe_id(identifier)
