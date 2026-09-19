"""End-to-end simulation and experiment state transitions without a Qt event loop."""
from __future__ import annotations

import csv
import json
import time
from pathlib import Path

import pytest

from mist_app.controller import DeviceHealth, ExperimentController
from mist_app.models import DEFAULT_STAGES, DeviceMessage


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for experiment condition")


def present(controller):
    scene = controller.snapshot()["scene"]
    controller.mark_presented(scene["id"])
    return scene


def rows(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def initialize_simulation(controller):
    controller.connect_eeg("SIM-EEG")
    controller.connect_ppg("SIM-PPG")
    wait_until(lambda: controller.snapshot()["ready"], timeout=5)
    controller.create_session({"id": "测试编号", "age": 25, "sex": "其他"},
                              {stage.key: 1 for stage in DEFAULT_STAGES})


def run_stage_to_rating(controller, answer=True):
    controller.start_stage()
    deadline = time.monotonic() + 6
    answered = set()
    while time.monotonic() < deadline:
        scene = present(controller)
        snapshot = controller.snapshot()
        if snapshot["state"] == "rating":
            return
        assert snapshot["state"] not in ("error", "interrupted"), snapshot["last_error"]
        if answer and scene["kind"] == "question" and scene["id"] not in answered:
            with controller._lock:
                if controller._question:
                    value = controller._question["correct_answer"]
                    controller.submit_answer(str(value))
                    answered.add(scene["id"])
        time.sleep(0.005)
    raise AssertionError(f"Stage did not finish: {controller.snapshot()}")


def test_real_simulation_all_six_stages_gates_timestamps_and_no_duplicates(tmp_path):
    controller = ExperimentController(simulate=True, output_root=tmp_path)
    try:
        with pytest.raises(RuntimeError, match="3 秒"):
            controller.create_session({"id": "TEST", "age": 20, "sex": "其他"}, {})
        started = time.perf_counter()
        initialize_simulation(controller)
        assert time.perf_counter() - started >= 3
        path = controller.recorder.path
        assert path.parent.name == "SIMULATED"
        for index, stage in enumerate(DEFAULT_STAGES):
            assert controller.snapshot()["stage_key"] == stage.key
            run_stage_to_rating(controller, answer=stage.key != "stress")
            with pytest.raises(ValueError):
                controller.submit_rating(101)
            controller.submit_rating(index * 10)
        wait_until(lambda: controller.state == "completed")
        wait_until(lambda: not any(device.connected for device in controller._devices.values()))
        assert not controller.recorder.error
        controller.close()
        manifest = json.loads((path / "session.json").read_text(encoding="utf-8"))
        assert manifest["status"] == "completed"
        assert len(manifest["attempts"]) == 6
        all_keys = {"eeg": set(), "ppg": set()}
        for attempt in manifest["attempts"]:
            assert attempt["status"] == "completed"
            assert attempt["end_ns"] - attempt["start_ns"] == 1_000_000_000
            for device in ("eeg", "ppg"):
                records = rows(path / attempt["directory"] / f"{attempt['stage']}_{device}.csv")
                assert records
                assert all(attempt["start_ns"] <= int(row["received_ns"]) < attempt["end_ns"] for row in records)
                keys = [(int(row["host_sequence"]), int(row["sample_in_packet"])) for row in records]
                assert len(keys) == len(set(keys))
                assert not all_keys[device].intersection(keys)
                all_keys[device].update(keys)
                assert attempt["quality"][device]["samples"] == len(records)
        trials = rows(path / "trials.csv")
        assert {trial["stage"] for trial in trials} == {"practice", "control", "stress"}
        assert next(trial for trial in trials if trial["stage"] == "stress")["outcome"] == "stage_truncated"
        assert len(rows(path / "ratings.csv")) == 6
        events = [json.loads(line) for line in (path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        presented = [event for event in events if event["kind"] == "scene_presented"]
        assert presented
        assert all(event["software_draw_ns"] >= event["scheduled_ns"] for event in presented)
    finally:
        controller.close()


def test_disconnect_failed_control_retry_does_not_calibrate_and_preserves_files(tmp_path):
    controller = ExperimentController(simulate=True, output_root=tmp_path)
    try:
        initialize_simulation(controller)
        # This test targets control retry; preceding stages are covered end-to-end above.
        with controller._lock:
            controller.stage_index = 3
            controller._instruction()
        controller.start_stage()
        present(controller)
        wait_until(lambda: (present(controller)["kind"] == "question"))
        with controller._lock:
            controller.submit_answer(str(controller._question["correct_answer"]))
        assert len(controller._attempt_trials) == 1
        controller.disconnect_device("ppg")
        wait_until(lambda: controller.state == "interrupted")
        failed = controller.window.path
        failed_rows = (failed / "control_ppg.csv").read_bytes()
        assert json.loads((failed / "stage.json").read_text(encoding="utf-8"))["status"] == "incomplete"
        assert controller._control_trials == []
        with pytest.raises(RuntimeError):
            controller.start_stage()
        controller.connect_ppg("SIM-PPG")
        wait_until(lambda: controller.snapshot()["ready"])
        run_stage_to_rating(controller)
        assert controller.attempt == 2
        assert controller.window.path != failed
        controller.submit_rating(50)
        assert controller.snapshot()["stage_key"] == "stress"
        assert len(controller._control_trials) == 1
        assert all(trial["attempt"] == 2 for trial in controller._control_trials)
        assert (failed / "control_ppg.csv").read_bytes() == failed_rows
    finally:
        controller.close()


def test_health_gate_resets_after_gap_and_stale_stream_is_not_ready():
    health = DeviceHealth()
    health.update(DeviceMessage("ppg", "connected", 0))
    for at in range(0, 3_100_000_000, 100_000_000):
        health.update(DeviceMessage("ppg", "packet", at, samples=((1,),)))
    assert health.ready(3_000_000_000)
    assert not health.ready(3_500_000_000)
    health.update(DeviceMessage("ppg", "packet", 3_600_000_000, samples=((1,),)))
    assert not health.ready(3_600_000_000)
    health.update(DeviceMessage("ppg", "disconnected", 3_600_000_001))
    assert not health.ready(3_600_000_001)


def test_close_during_setup_releases_devices_and_is_idempotent(tmp_path):
    controller = ExperimentController(simulate=True, output_root=tmp_path)
    controller.connect_eeg("SIM-EEG")
    controller.connect_ppg("SIM-PPG")
    wait_until(lambda: all(device.connected for device in controller._devices.values()))
    controller.close()
    controller.close()
    assert controller.state == "aborted"
    assert not controller._thread.is_alive()
    assert not any(device.connected for device in controller._devices.values())


def test_three_seconds_without_valid_data_interrupts_stage(tmp_path):
    controller = ExperimentController(simulate=True, output_root=tmp_path)
    try:
        # Inject an established connection without running either reader. This
        # controls the no-data interval exactly while retaining real storage.
        base = time.perf_counter_ns() - 3_100_000_000
        for device, samples in (("eeg", ((1, 2, 3, 4),) * 8), ("ppg", ((42,),))):
            controller._on_message(DeviceMessage(device, "connected", base))
            for index in range(32):
                controller._on_message(DeviceMessage(device, "packet", base + index * 100_000_000,
                                                       samples=samples))
        controller.create_session({"id": "STALE", "age": 20, "sex": "其他"},
                                  {stage.key: 10 for stage in DEFAULT_STAGES})
        controller.start_stage()
        present(controller)
        with controller._lock:
            now = time.perf_counter_ns()
            controller._health["ppg"].last_valid = now - 3_000_000_000
            controller._health["ppg"].last_message = now
            controller._health["eeg"].last_message = now
            controller._tick(now)
        wait_until(lambda: controller.state == "interrupted")
        assert "连续 3 秒" in controller.last_error
        assert controller.window.status == "incomplete"
        stage = json.loads((controller.window.path / "stage.json").read_text(encoding="utf-8"))
        assert stage["status"] == "incomplete"
    finally:
        controller.close()


def test_close_running_stage_finalizes_incomplete_attempt_and_aborted_session(tmp_path):
    controller = ExperimentController(simulate=True, output_root=tmp_path)
    initialize_simulation(controller)
    controller.start_stage()
    present(controller)
    wait_until(lambda: controller.recorder.saved_counts["eeg"] > 0 and controller.recorder.saved_counts["ppg"] > 0)
    path = controller.recorder.path
    stage_path = controller.window.path
    controller.close()
    assert controller.state == "aborted"
    assert not controller._thread.is_alive()
    assert not controller.recorder._thread.is_alive()
    manifest = json.loads((path / "session.json").read_text(encoding="utf-8"))
    stage = json.loads((stage_path / "stage.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "aborted"
    assert stage["status"] == "incomplete"
    for device in ("eeg", "ppg"):
        records = rows(stage_path / f"eyes_open_{device}.csv")
        assert records
        assert all(stage["start_ns"] <= int(row["received_ns"]) < stage["end_ns"] for row in records)


def test_delayed_disconnect_callback_never_moves_cutoff_behind_saved_data(tmp_path):
    controller = ExperimentController(simulate=True, output_root=tmp_path)
    try:
        base = time.perf_counter_ns() - 3_100_000_000
        for device, samples in (("eeg", ((1, 2, 3, 4),) * 8), ("ppg", ((42,),))):
            controller._on_message(DeviceMessage(device, "connected", base))
            for index in range(32):
                controller._on_message(DeviceMessage(device, "packet", base + index * 100_000_000,
                                                       samples=samples))
        controller.create_session({"id": "RACE", "age": 20, "sex": "其他"},
                                  {stage.key: 10 for stage in DEFAULT_STAGES})
        controller.start_stage()
        present(controller)
        # The PPG worker captured its disconnection timestamp, then lost the
        # controller lock to EEG. EEG's later packet was already persisted.
        original_fault_ns = controller.window.start_ns + 1
        eeg_received_ns = time.perf_counter_ns()
        controller._on_message(DeviceMessage("eeg", "packet", eeg_received_ns,
                                               samples=((1, 2, 3, 4),) * 8))
        wait_until(lambda: controller.recorder.saved_counts["eeg"] == 8)
        controller._on_message(DeviceMessage("ppg", "disconnected", original_fault_ns))
        assert controller.window.end_ns > eeg_received_ns
        # Post-cutoff watermark drains EEG, without recording this packet.
        controller._on_message(DeviceMessage("eeg", "packet", time.perf_counter_ns(),
                                               samples=((5, 6, 7, 8),) * 8))
        wait_until(lambda: controller.state == "interrupted")
        records = rows(controller.window.path / "eyes_open_eeg.csv")
        assert len(records) == 8
        assert all(int(row["received_ns"]) < controller.window.end_ns for row in records)
        controller.close()
        events = [json.loads(line) for line in (controller.recorder.path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        fault = next(event for event in events if event["kind"] == "device_disconnected" and event["device"] == "ppg")
        assert fault["at_ns"] == original_fault_ns
    finally:
        controller.close()
