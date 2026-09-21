"""Explicit, bounded simulation acceptance check for source and packaged builds.

Developer invocation::

    python -m mist_app --self-test C:\\Temp\\mist-check --self-test-hidden
    MIST-EEG-PPG.exe --self-test C:\\Temp\\mist-exe-check --self-test-hidden

The flag always creates a simulation controller. It never scans or connects to
physical devices. Two successive experiments run in the same window using the
restart button. ``self_test_report.json`` and screenshots are written
to the selected directory alongside clearly marked synthetic session data.
"""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import re
import time
import traceback

from PySide6 import QtCore, QtWidgets

from .models import DEFAULT_STAGES
from .ui import MainWindow


def run_self_test(app: QtWidgets.QApplication, output: Path, hidden: bool = False) -> int:
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    window: MainWindow | None = None
    report = {"status": "running", "mode": "simulate", "hardware_tested": False,
              "hidden": hidden, "checks": {}, "screenshots": [], "session_path": "",
              "sessions": []}
    timer = QtCore.QTimer()
    answered: set[int] = set()
    question_captured = False
    finished = False
    session_number = 1
    connected_at = 0.0
    readiness_seconds: list[float] = []
    previous_controller = None
    previous_session: Path | None = None
    payload_hashes: dict[str, str] = {}
    saved_hashes: dict[str, str] = {}

    def screenshot(name: str) -> None:
        window.refresh()
        filename = output / name
        if not window.grab().save(str(filename)):
            raise RuntimeError(f"Failed to save screenshot: {filename}")
        report["screenshots"].append(name)

    def finish(error: str = "") -> None:
        nonlocal finished
        if finished:
            return
        finished = True
        timer.stop()
        if window is not None:
            try:
                report["session_path"] = window.controller.snapshot().get("session_path", "")
                window.close()
            except Exception:
                error = error or traceback.format_exc()
        report["status"] = "failed" if error else "passed"
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        if error:
            report["error"] = error
        (output / "self_test_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        app.exit(1 if error else 0)

    def fingerprints(session: Path, include_events: bool = True) -> dict[str, str]:
        return {str(path.relative_to(session)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(session.rglob("*"))
                if path.is_file() and (include_events or path.name != "events.jsonl")}

    def connect_devices() -> None:
        nonlocal connected_at
        connected_at = time.monotonic()
        window.controller.connect_eeg("SIM-EEG")
        window.controller.connect_ppg("SIM-PPG")
        window.controller.connect_temperature("SIM-TEMPERATURE")

    def verify(session: Path, participant_id: str) -> dict:
        manifest = json.loads((session / "session.json").read_text(encoding="utf-8"))
        if manifest.get("status") != "completed":
            raise AssertionError(f"Session not finalized: {manifest.get('status')}")
        if manifest["participant"]["id"] != participant_id:
            raise AssertionError("Saved participant does not match the current experiment")
        if not re.fullmatch(rf"{re.escape(participant_id)}_其他_\d{{8}}_\d{{6}}_\d{{6}}(?:_\d{{2,}})?", session.name):
            raise AssertionError("Session folder must use participant ID, sex and local time including microseconds")
        stages = sorted(session.rglob("stage.json"))
        if len(stages) != 6:
            raise AssertionError(f"Expected 6 saved stage attempts, found {len(stages)}")
        counts = {}
        for stage in DEFAULT_STAGES:
            counts[stage.key] = {}
            for device in ("eeg", "ppg", "temperature"):
                matches = list(session.rglob(f"{stage.key}_{device}.csv"))
                if len(matches) != 1:
                    raise AssertionError(f"Expected one {stage.key} {device} sample file")
                with matches[0].open(encoding="utf-8-sig", newline="") as handle:
                    records = list(csv.DictReader(handle))
                    count = len(records)
                if any(row["session_id"] != manifest["session_id"] for row in records):
                    raise AssertionError("Sample rows contain another experiment's session ID")
                if device == "temperature":
                    if not all(30 <= float(row["temperature_c"]) <= 40 for row in records):
                        raise AssertionError("Unexpected simulated temperature value")
                if count < 1:
                    raise AssertionError(f"No saved samples for {stage.key} {device}")
                counts[stage.key][device] = count
        with (session / "ratings.csv").open(encoding="utf-8-sig", newline="") as handle:
            ratings = list(csv.DictReader(handle))
        if len(ratings) != 6:
            raise AssertionError(f"Expected 6 ratings, found {len(ratings)}")
        with (session / "events.jsonl").open(encoding="utf-8") as handle:
            events = [json.loads(row) for row in handle if row.strip()]
        checks = dict(all_six_stages_saved=True, six_ratings_saved=True,
                      sample_rows=counts, event_rows=len(events),
                      software_presentation_only=True, completed=True,
                      answered_questions=len(answered))
        # Keep the legacy single-session fields for existing build consumers.
        report["checks"].update(checks)
        return {"session_path": str(session), "folder_name": session.name, "session_id": manifest["session_id"],
                "participant_id": participant_id, "checks": checks}

    def verify_restart(snapshot: dict) -> None:
        if snapshot["state"] != "setup" or snapshot["session_path"]:
            raise AssertionError("Restart did not return to a fresh setup page")
        if snapshot["stage_index"] != 0 or snapshot["attempt"] != 0:
            raise AssertionError("The previous experiment's stage state was retained")
        if snapshot["ready"] or any(data["connected"] or data["ready"] or data["samples"]
                                    or data["preview"] for data in snapshot["devices"].values()):
            raise AssertionError("Restart retained old device connections, samples, or readiness")
        if (window.pages.currentIndex() != 0 or window.participant_group.isEnabled()
                or window.create_button.isEnabled() or window.restart_button.isVisible()):
            raise AssertionError("Restart must show setup with participant entry gated by device readiness")
        if (window.subject_id.text() or window.age.value() != 25 or window.sex.currentData()
                or window.note.text() or window.temperature_site.text() or window.answer.text()
                or window._rating_touched or window.rating_slider.value() != 50
                or window.rating_button.isEnabled()):
            raise AssertionError("Restart retained participant information, an answer, or a rating")
        if (not window.simulation.isChecked() or not window.temperature_enabled.isChecked()
                or Path(window.output_root.text()).resolve() != output
                or any(spin.value() != 2 for spin in window.durations.values())):
            raise AssertionError("Restart changed the acquisition mode, output folder, or stage durations")
        if previous_controller._thread.is_alive() or previous_controller.recorder._thread.is_alive():
            raise AssertionError("The previous controller or session writer is still running after restart")
        report["checks"].update(restart_returns_to_setup=True, participant_fields_reset=True,
                                 device_readiness_reset=True, previous_controller_closed=True,
                                 acquisition_settings_preserved=True)

    def step() -> None:
        nonlocal question_captured, session_number, previous_controller, previous_session
        nonlocal payload_hashes, saved_hashes
        if finished:
            return
        try:
            if time.monotonic() - started > 55:
                raise TimeoutError("Two-experiment GUI acceptance check exceeded 55 seconds")
            window.refresh()
            snapshot = window.controller.snapshot()
            state = snapshot["state"]
            if previous_controller is not None and session_number == 1:
                if window.controller is previous_controller:
                    return  # The restart button closes the old devices and writer asynchronously.
                verify_restart(snapshot)
                if fingerprints(previous_session, include_events=False) != payload_hashes:
                    raise AssertionError("Restart modified the previous experiment's saved data")
                report["sessions"].append(verify(previous_session, "SELFTEST"))
                # Shutdown can append device-disconnect events. Once it has
                # finished, every file must remain byte-for-byte unchanged.
                saved_hashes = fingerprints(previous_session)
                screenshot("04_restart_setup.png")
                session_number = 2
                answered.clear()
                question_captured = False
                connect_devices()
                return
            if state in ("error", "interrupted", "aborted"):
                raise RuntimeError(f"Unexpected state {state}: {snapshot.get('last_error', '')}")
            if state == "setup":
                if not snapshot["ready"]:
                    if window.participant_group.isEnabled() or window.create_button.isEnabled():
                        raise AssertionError("Participant entry was unlocked before devices became ready")
                    return
                elapsed = time.monotonic() - connected_at
                if elapsed < 3:
                    raise AssertionError("Devices became ready before receiving 3 seconds of fresh data")
                if window._busy:
                    return
                readiness_seconds.append(round(elapsed, 3))
                screenshot("01_setup.png" if session_number == 1 else "05_repeat_ready.png")
                window.subject_id.setText("SELFTEST")
                window.age.setValue(20 if session_number == 1 else 30)
                window.sex.setCurrentIndex(window.sex.findData("unspecified"))
                window.note.setText("Automated simulation acceptance check; no physical devices")
                window.temperature_site.setText("左前臂皮肤（模拟）")
                for spin in window.durations.values():
                    # A low-frequency temperature stream needs more than one
                    # period to guarantee a sample despite scheduler jitter.
                    spin.setValue(2)
                window._create_session()
                report["checks"]["readiness_gate_passed"] = True
            elif state == "instruction":
                window._start_stage()
            elif state == "running" and snapshot["scene"].get("kind") == "question":
                scene = snapshot["scene"]
                if not question_captured:
                    screenshot("02_question.png" if session_number == 1 else "06_repeat_question.png")
                    question_captured = True
                scene_id = scene["id"]
                if scene_id not in answered and scene.get("deadline_seconds", 0) > 0:
                    match = re.search(r"(-?\d+)\s*([+\-×*])\s*(-?\d+)", str(scene.get("question", "")))
                    if not match:
                        raise AssertionError(f"Unexpected arithmetic prompt: {scene.get('question')}")
                    left, op, right = int(match[1]), match[2], int(match[3])
                    value = left + right if op == "+" else left - right if op == "-" else left * right
                    window.answer.setText(str(value))
                    window._submit_answer()
                    answered.add(scene_id)
            elif state == "rating":
                window.rating_slider.setValue(65 if session_number == 1 else 35)
                window._touch_rating()
                window._submit_rating()
            elif state == "completed":
                screenshot("03_completed.png" if session_number == 1 else "07_repeat_completed.png")
                if not question_captured or len(answered) < 3:
                    raise AssertionError("All three arithmetic stages must display a question and accept an answer")
                if not window.restart_button.isVisible() or not window.restart_button.isEnabled():
                    raise AssertionError("A completed experiment must offer an enabled restart button")
                session_path = Path(snapshot["session_path"])
                if session_number == 1:
                    previous_session = session_path
                    payload_hashes = fingerprints(session_path, include_events=False)
                    previous_controller = window.controller
                    # Leave a stale answer as well as the last participant's
                    # rating so that the reset cannot pass accidentally.
                    window.answer.setText("12345")
                    window.restart_button.click()
                    if window.restart_button.isEnabled():
                        raise AssertionError("Restart must be disabled while the old experiment is closing")
                    return
                window.controller.close()
                report["sessions"].append(verify(session_path, "SELFTEST"))
                if (session_path == previous_session
                        or report["sessions"][0]["session_id"] == report["sessions"][1]["session_id"]):
                    raise AssertionError("Successive experiments must have separate session paths and IDs")
                if fingerprints(previous_session) != saved_hashes:
                    raise AssertionError("The second experiment modified the first experiment's saved files")
                report["checks"].update(two_experiments_completed=True, same_window_reused=True,
                                         distinct_session_paths_and_ids=True, previous_files_unchanged=True,
                                         participant_sex_date_folder_names=True, repeated_participant_id=True,
                                         microsecond_timestamp_folder_names=True,
                                         readiness_seconds=readiness_seconds,
                                         completed_session_count=len(report["sessions"]))
                finish()
        except Exception:
            finish(traceback.format_exc())

    try:
        window = MainWindow(simulate=True, output_root=output)
        window._confirm = lambda *args: True

        def raise_error(error) -> None:
            raise RuntimeError(str(error))

        window._error = raise_error
        if hidden:
            window.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        window.show()
        connect_devices()
        timer.setInterval(40)
        timer.timeout.connect(step)
        timer.start()
    except Exception:
        finish(traceback.format_exc())
        return 1
    return app.exec()
