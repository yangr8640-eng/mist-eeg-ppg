"""User-visible gates and interactions without requiring physical devices."""
from __future__ import annotations

import copy
import threading

import pytest
from PySide6 import QtCore

from mist_app.ui import MainWindow


class FakeController:
    def __init__(self):
        device = dict(connected=False, ready=False, streaming=False, rate=0,
                      samples=0, saved_samples=0, last_age=None, error="",
                      identifier="", electrode_off=False, battery_raw=None, preview=[])
        self.data = dict(mode="simulate", state="setup", ready=False,
                         devices={"eeg": dict(device), "ppg": dict(device)},
                         session_path="", stage_index=0, stage_key="eyes_open",
                         stage_name="睁眼静息", duration=180, remaining=180,
                         attempt=1, scene=dict(id=1, kind="instruction", text="开始实验", detail=""),
                         last_error="", saving=False)
        self.created = None
        self.answers = []
        self.ratings = []
        self.presented = []
        self.closed = False
        self.scan_threads = []

    def snapshot(self):
        return copy.deepcopy(self.data)

    def list_ppg(self):
        self.scan_threads.append(threading.get_ident())
        return [{"device": "COM7", "description": "测试指夹"}]

    def scan_eeg(self):
        self.scan_threads.append(threading.get_ident())
        return [{"name": "测试脑环", "address": "00:11:22:33:44:55"}]

    def set_ready(self, ready=True):
        self.data["ready"] = ready
        for device in self.data["devices"].values():
            device.update(connected=ready, ready=ready, streaming=ready, rate=500)

    def connect_eeg(self, address):
        self.data["devices"]["eeg"]["identifier"] = address

    def connect_ppg(self, port):
        self.data["devices"]["ppg"]["identifier"] = port

    def disconnect_device(self, kind):
        self.data["devices"][kind]["connected"] = False

    def create_session(self, participant, durations, output_root):
        self.created = (participant, durations, output_root)
        self.data["session_path"] = str(output_root / participant["id"])
        self.data["state"] = "instruction"

    def start_stage(self):
        self.data["state"] = "running"
        self.data["scene"] = dict(id=2, kind="rest", text="请保持放松", detail="")

    def submit_answer(self, text):
        self.answers.append(text)

    def submit_rating(self, value):
        self.ratings.append(value)
        self.data["state"] = "instruction"
        self.data["stage_index"] += 1

    def abort_stage(self, reason="operator"):
        self.data["state"] = "interrupted"

    def abort_session(self):
        self.data["state"] = "aborted"

    def mark_presented(self, scene_id, at_ns=None):
        self.presented.append((scene_id, at_ns))

    def close(self):
        self.closed = True


@pytest.fixture
def window(qtbot, tmp_path):
    controller = FakeController()
    widget = MainWindow(simulate=True, output_root=tmp_path, controller=controller)
    widget._confirm = lambda *args: True
    qtbot.addWidget(widget)
    widget.show()
    qtbot.waitUntil(lambda: not widget._busy)
    yield widget, controller
    widget._confirm = lambda *args: True
    widget.close()


def test_information_and_start_require_both_devices_ready(window, qtbot):
    widget, controller = window
    assert not widget.subject_id.isEnabled()
    assert not widget.create_button.isEnabled()
    controller.data["devices"]["eeg"].update(connected=True, ready=True)
    widget.refresh()
    assert not widget.subject_id.isEnabled()
    controller.set_ready()
    widget.refresh()
    assert widget.subject_id.isEnabled()
    assert widget.create_button.isEnabled()
    widget.subject_id.setText("P001")
    widget.age.setValue(30)
    widget.sex.setCurrentIndex(1)
    qtbot.mouseClick(widget.create_button, QtCore.Qt.MouseButton.LeftButton)
    assert controller.created[0] == {"id": "P001", "age": 30, "sex": "female", "note": ""}
    assert set(controller.created[1].values()) == {180.0}
    assert widget.pages.currentIndex() == 1
    assert not widget.simulation.isEnabled()
    controller.set_ready(False)
    widget.refresh()
    assert not widget.stage_action.isEnabled()
    controller.set_ready()
    widget.refresh()
    qtbot.mouseClick(widget.stage_action, QtCore.Qt.MouseButton.LeftButton)
    assert controller.data["state"] == "running"


def test_negative_numeric_answer_and_rating_needs_explicit_touch(window, qtbot):
    widget, controller = window
    controller.set_ready()
    controller.data.update(state="running", session_path="C:/test/P001")
    controller.data["scene"] = dict(id=10, kind="question", question="2 − 5 = ?", deadline_seconds=8)
    widget.refresh()
    qtbot.keyClicks(widget.answer, "-3")
    qtbot.keyClick(widget.answer, QtCore.Qt.Key.Key_Return)
    assert controller.answers == ["-3"]
    controller.data["state"] = "rating"
    controller.data["scene"] = dict(id=11, kind="rating", text="主观压力评分")
    widget.refresh()
    assert not widget.rating_button.isEnabled()
    widget._submit_rating()
    assert not controller.ratings
    widget.rating_slider.setFocus()
    qtbot.keyClick(widget.rating_slider, QtCore.Qt.Key.Key_Right)
    assert widget.rating_button.isEnabled()
    qtbot.mouseClick(widget.rating_button, QtCore.Qt.MouseButton.LeftButton)
    assert controller.ratings == [51]


def test_scanning_runs_off_gui_thread_and_selects_real_identifier(window, qtbot):
    widget, controller = window
    widget._scan("eeg")
    qtbot.waitUntil(lambda: "scan_eeg" not in widget._busy)
    assert controller.scan_threads
    assert all(ident != threading.get_ident() for ident in controller.scan_threads)
    assert widget.cards["eeg"].device_picker.currentData() == "00:11:22:33:44:55"
    widget._connect("eeg")
    qtbot.waitUntil(lambda: "connect_eeg" not in widget._busy)
    assert controller.data["devices"]["eeg"]["identifier"] == "00:11:22:33:44:55"


def test_presentation_is_reported_once_per_scene(window, qtbot):
    widget, controller = window
    controller.data.update(state="running", session_path="C:/test/P001")
    controller.data["scene"] = dict(id=50, kind="fixation", text="+")
    widget.refresh()
    qtbot.waitUntil(lambda: any(ident == 50 for ident, _ in controller.presented))
    widget.canvas.repaint()
    qtbot.wait(40)
    assert len([mark for mark in controller.presented if mark[0] == 50]) == 1
    assert all(isinstance(at_ns, int) and at_ns > 0 for _, at_ns in controller.presented)


def test_interrupted_stage_retry_and_close_confirmation(window, qtbot):
    widget, controller = window
    controller.data.update(state="interrupted", session_path="C:/test/P001")
    controller.set_ready(False)
    widget.refresh()
    assert widget.stage_action.text() == "重新开始本阶段"
    assert not widget.stage_action.isEnabled()
    controller.set_ready()
    widget.refresh()
    qtbot.mouseClick(widget.stage_action, QtCore.Qt.MouseButton.LeftButton)
    assert controller.data["state"] == "running"
    widget._confirm = lambda *args: False
    widget.close()
    assert not controller.closed
    assert widget.isVisible()
    widget._confirm = lambda *args: True
    widget.close()
    assert controller.closed
