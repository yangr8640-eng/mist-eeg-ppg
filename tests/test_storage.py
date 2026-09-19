"""Recording integrity tests, including receive-time boundaries and disk failure."""
from __future__ import annotations

import csv
import json
import queue
import time
from pathlib import Path

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
