"""Explicit, bounded simulation acceptance check for source and packaged builds.

Developer invocation::

    python -m mist_app --self-test C:\\Temp\\mist-check --self-test-hidden
    MIST-EEG-PPG.exe --self-test C:\\Temp\\mist-exe-check --self-test-hidden

The flag always creates a simulation controller. It never scans or connects to
physical devices. ``self_test_report.json`` and three screenshots are written
to the selected directory alongside clearly marked synthetic session data.
"""
from __future__ import annotations

import csv
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
              "hidden": hidden, "checks": {}, "screenshots": [], "session_path": ""}
    timer = QtCore.QTimer()
    answered: set[int] = set()
    question_captured = False
    finished = False

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

    def verify(session: Path) -> None:
        manifest = json.loads((session / "session.json").read_text(encoding="utf-8"))
        if manifest.get("status") != "completed":
            raise AssertionError(f"Session not finalized: {manifest.get('status')}")
        stages = sorted(session.rglob("stage.json"))
        if len(stages) != 6:
            raise AssertionError(f"Expected 6 saved stage attempts, found {len(stages)}")
        counts = {}
        for stage in DEFAULT_STAGES:
            counts[stage.key] = {}
            for device in ("eeg", "ppg"):
                matches = list(session.rglob(f"{stage.key}_{device}.csv"))
                if len(matches) != 1:
                    raise AssertionError(f"Expected one {stage.key} {device} sample file")
                with matches[0].open(encoding="utf-8-sig", newline="") as handle:
                    count = sum(1 for _ in csv.DictReader(handle))
                if count < 1:
                    raise AssertionError(f"No saved samples for {stage.key} {device}")
                counts[stage.key][device] = count
        with (session / "ratings.csv").open(encoding="utf-8-sig", newline="") as handle:
            ratings = list(csv.DictReader(handle))
        if len(ratings) != 6:
            raise AssertionError(f"Expected 6 ratings, found {len(ratings)}")
        with (session / "events.jsonl").open(encoding="utf-8") as handle:
            events = [json.loads(row) for row in handle if row.strip()]
        report["checks"].update(all_six_stages_saved=True, six_ratings_saved=True,
                                 sample_rows=counts, event_rows=len(events),
                                 software_presentation_only=True, completed=True)

    def step() -> None:
        nonlocal question_captured
        if finished:
            return
        try:
            if time.monotonic() - started > 45:
                raise TimeoutError("Simulation GUI acceptance check exceeded 45 seconds")
            snapshot = window.controller.snapshot()
            state = snapshot["state"]
            if state in ("error", "interrupted", "aborted"):
                raise RuntimeError(f"Unexpected state {state}: {snapshot.get('last_error', '')}")
            if state == "setup" and snapshot["ready"]:
                screenshot("01_setup.png")
                window.subject_id.setText("SELFTEST")
                window.age.setValue(20)
                window.sex.setCurrentIndex(window.sex.findData("unspecified"))
                window.note.setText("Automated simulation acceptance check; no physical devices")
                for spin in window.durations.values():
                    spin.setValue(1)
                window._create_session()
                report["checks"]["readiness_gate_passed"] = True
            elif state == "instruction":
                window._start_stage()
            elif state == "running" and snapshot["scene"].get("kind") == "question":
                scene = snapshot["scene"]
                if not question_captured:
                    screenshot("02_question.png")
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
                window.rating_slider.setValue(50)
                window._touch_rating()
                window._submit_rating()
            elif state == "completed":
                screenshot("03_completed.png")
                if not question_captured or len(answered) < 3:
                    raise AssertionError("All three arithmetic stages must display a question and accept an answer")
                report["checks"]["answered_questions"] = len(answered)
                session_path = Path(snapshot["session_path"])
                window.controller.close()
                verify(session_path)
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
        window.controller.connect_eeg("SIM-EEG")
        window.controller.connect_ppg("SIM-PPG")
        timer.setInterval(40)
        timer.timeout.connect(step)
        timer.start()
    except Exception:
        finish(traceback.format_exc())
        return 1
    return app.exec()
