"""User-visible gates and interactions without requiring physical devices."""
from __future__ import annotations

import copy
from pathlib import Path
import threading

import pytest
from PySide6 import QtCore

from mist_app.ui import MainWindow


class FakeController:
    def __init__(self):
        device = dict(enabled=True, connected=False, ready=False, streaming=False, rate=0,
                      samples=0, saved_samples=0, last_age=None, error="",
                      identifier="", electrode_off=False, battery_raw=None, preview=[])
        self.data = dict(mode="simulate", state="setup", ready=False, temperature_enabled=True,
                         devices={"eeg": dict(device), "ppg": dict(device),
                                  "temperature": dict(device, temperature_c=None)},
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

    def list_temperature(self):
        self.scan_threads.append(threading.get_ident())
        return [{"device": "COM8", "description": "温度蓝牙接收器"}]

    def set_temperature_enabled(self, enabled):
        if self.data["state"] != "setup":
            raise RuntimeError("会话建立后不可修改温度采集设置")
        self.data["temperature_enabled"] = enabled
        self.data["devices"]["temperature"]["enabled"] = enabled
        self.data["ready"] = all(device["ready"] for device in self.data["devices"].values()
                                 if device["enabled"])

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

    def connect_temperature(self, port):
        self.data["devices"]["temperature"]["identifier"] = port

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


@pytest.fixture
def replacement_controllers(monkeypatch):
    replacements = []

    def create_controller(*, simulate, output_root, temperature_enabled):
        replacement = FakeController()
        replacement.data["mode"] = "simulate" if simulate else "hardware"
        replacement.set_temperature_enabled(temperature_enabled)
        replacements.append((dict(simulate=simulate, output_root=output_root,
                                  temperature_enabled=temperature_enabled), replacement))
        return replacement

    monkeypatch.setattr("mist_app.ui.ExperimentController", create_controller)
    return replacements


def test_information_and_start_require_all_enabled_devices_ready(window, qtbot):
    widget, controller = window
    assert not widget.subject_id.isEnabled()
    assert not widget.create_button.isEnabled()
    controller.data["devices"]["eeg"].update(connected=True, ready=True)
    controller.data["devices"]["ppg"].update(connected=True, ready=True)
    widget.refresh()
    assert not widget.subject_id.isEnabled()
    controller.set_ready()
    widget.refresh()
    assert widget.subject_id.isEnabled()
    assert widget.create_button.isEnabled()
    widget.subject_id.setText("P001")
    widget.age.setValue(30)
    widget.sex.setCurrentIndex(1)
    widget.temperature_site.setText("左侧前臂内侧")
    qtbot.mouseClick(widget.create_button, QtCore.Qt.MouseButton.LeftButton)
    assert controller.created[0] == {"id": "P001", "age": 30, "sex": "female", "note": "",
                                     "temperature_site": "左侧前臂内侧"}
    assert set(controller.created[1].values()) == {180.0}
    assert widget.pages.currentIndex() == 1
    assert not widget.simulation.isEnabled()
    assert not widget.temperature_enabled.isEnabled()
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
    widget._scan("temperature")
    qtbot.waitUntil(lambda: "scan_temperature" not in widget._busy)
    assert widget.cards["temperature"].device_picker.currentData() == "COM8"
    widget._connect("temperature")
    qtbot.waitUntil(lambda: "connect_temperature" not in widget._busy)
    assert controller.data["devices"]["temperature"]["identifier"] == "COM8"
    assert all(ident != threading.get_ident() for ident in controller.scan_threads)


def test_temperature_can_be_disabled_before_session_and_skips_site(window, qtbot):
    widget, controller = window
    for kind in ("eeg", "ppg"):
        controller.data["devices"][kind].update(connected=True, ready=True)
    qtbot.mouseClick(widget.temperature_enabled, QtCore.Qt.MouseButton.LeftButton)
    assert not controller.data["temperature_enabled"]
    assert not widget.temperature_site.isVisible()
    assert widget.cards["temperature"].status.text() == "未启用"
    assert widget.create_button.isEnabled()
    widget.subject_id.setText("NO_TEMP")
    widget.sex.setCurrentIndex(1)
    qtbot.mouseClick(widget.create_button, QtCore.Qt.MouseButton.LeftButton)
    assert "temperature_site" not in controller.created[0]
    assert not widget.temperature_enabled.isEnabled()


def test_temperature_site_is_required_and_old_values_are_marked_stale(window, qtbot):
    widget, controller = window
    controller.set_ready()
    widget.refresh()
    widget.subject_id.setText("P_TEMP")
    widget.sex.setCurrentIndex(1)
    errors = []
    widget._error = errors.append
    qtbot.mouseClick(widget.create_button, QtCore.Qt.MouseButton.LeftButton)
    assert controller.created is None
    assert "温度测量部位" in errors[0]
    device = controller.data["devices"]["temperature"]
    device.update(temperature_c=33.125, last_age=.2, saved_samples=6, rate=1)
    widget.refresh()
    card = widget.cards["temperature"]
    assert card.temperature_value.text() == "33.1 °C"
    assert "最近更新 0.2 秒前" in card.details.text()
    device.update(streaming=False, ready=False, last_age=4.2)
    widget.refresh()
    assert "上次 33.1 °C · 已过期" == card.temperature_value.text()
    assert card.status.text() == "等待更新"
    device.update(connected=False, error="串口断开")
    widget.refresh()
    assert "已过期" in card.temperature_value.text()
    assert card.status.text() == "连接异常"


def test_temperature_plot_uses_absolute_celsius(window):
    widget, controller = window
    card = widget.cards["temperature"]
    card.update_plot({"preview": [(10.0, [32.75]), (11.0, [32.8]), (12.0, [32.85])]})
    x, y = card.curves[0].getData()
    assert x.tolist() == [-2, -1, 0]
    assert y.tolist() == [32.75, 32.8, 32.85]
    low, high = card.plot.viewRange()[1]
    assert low < 32.75 < 32.85 < high


def test_simulation_switch_preserves_temperature_setting(window, qtbot, monkeypatch):
    widget, controller = window
    qtbot.mouseClick(widget.temperature_enabled, QtCore.Qt.MouseButton.LeftButton)
    replacements = []

    def replace_controller(*, simulate, output_root, temperature_enabled):
        replacement = FakeController()
        replacement.data["mode"] = "simulate" if simulate else "hardware"
        replacement.set_temperature_enabled(temperature_enabled)
        replacements.append(replacement)
        return replacement

    monkeypatch.setattr("mist_app.ui.ExperimentController", replace_controller)
    qtbot.mouseClick(widget.simulation, QtCore.Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not widget._busy)
    assert controller.closed
    assert widget.controller is replacements[0]
    assert not widget.controller.data["temperature_enabled"]
    assert not widget.temperature_enabled.isChecked()
    assert not widget.cards["temperature"].device_picker.isVisible()
    qtbot.mouseClick(widget.temperature_enabled, QtCore.Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not widget._busy)
    assert widget.cards["temperature"].device_picker.isVisible()
    assert widget.cards["temperature"].device_picker.currentData() == "COM8"


@pytest.mark.parametrize("size", [(1360, 900), (1080, 750)])
def test_three_cards_fit_sidebar_during_recording(window, qtbot, size):
    widget, controller = window
    controller.set_ready()
    controller.data.update(state="running", session_path="C:/test/P001")
    controller.data["devices"]["temperature"].update(temperature_c=32.5, last_age=.2)
    widget.resize(*size)
    widget.refresh()
    qtbot.wait(50)
    viewport = widget.cards["temperature"].parentWidget().parentWidget()
    for card in widget.cards.values():
        assert not card.device_picker.isVisible()
        assert card.plot.isVisible()
        top_left = card.mapTo(viewport, QtCore.QPoint(0, 0))
        assert top_left.y() >= 0
        assert top_left.y() + card.height() <= viewport.height()


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


@pytest.mark.parametrize("final_state,temperature_enabled,simulate", [
    ("completed", True, True), ("completed", False, False),
    ("aborted", True, False), ("aborted", False, True),
])
def test_repeat_experiment_resets_subject_and_requires_fresh_devices(
        window, qtbot, replacement_controllers, final_state, temperature_enabled, simulate):
    widget, controller = window
    controller.set_temperature_enabled(temperature_enabled)
    controller.set_ready()
    controller.data.update(state=final_state, mode="simulate" if simulate else "hardware",
                           session_path="C:/test/PREVIOUS", stage_index=5, stage_key="recovery")
    blocker = QtCore.QSignalBlocker(widget.simulation)
    widget.simulation.setChecked(simulate)
    del blocker
    widget.refresh()
    qtbot.waitUntil(lambda: any(scene_id == 1 for scene_id, _ in controller.presented))
    widget.subject_id.setText("PREVIOUS")
    widget.age.setValue(62)
    widget.sex.setCurrentIndex(2)
    widget.note.setText("上一次的备注")
    widget.temperature_site.setText("右手背")
    widget.answer.setText("-31")
    widget._touch_rating()
    widget.rating_slider.setValue(91)
    for index, spin in enumerate(widget.durations.values()):
        spin.setValue(11 + index)
    durations = {key: float(spin.value()) for key, spin in widget.durations.items()}
    output_root = Path(widget.output_root.text()) / "collection"
    widget.output_root.setText(str(output_root))
    previous_data = controller.snapshot()

    assert widget.restart_button.isVisible()
    assert widget.restart_button.isEnabled()
    assert widget.restart_button.text() == "再次实验"
    assert widget.open_folder_button.isVisible()
    qtbot.mouseClick(widget.restart_button, QtCore.Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: bool(replacement_controllers) and not widget._busy)

    options, replacement = replacement_controllers[0]
    assert len(replacement_controllers) == 1
    assert controller.closed
    assert controller.snapshot() == previous_data
    assert options == dict(simulate=simulate, output_root=output_root,
                           temperature_enabled=temperature_enabled)
    assert widget.controller is replacement
    assert widget.pages.currentIndex() == 0
    assert widget.subject_id.text() == ""
    assert widget.age.value() == 25
    assert widget.sex.currentData() == ""
    assert widget.note.text() == ""
    assert widget.temperature_site.text() == ""
    assert widget.temperature_site.isVisible() == temperature_enabled
    assert widget.temperature_enabled.isChecked() == temperature_enabled
    assert widget.simulation.isChecked() == simulate
    assert widget.simulation.isEnabled()
    assert widget.temperature_enabled.isEnabled()
    assert widget.output_root.text() == str(output_root)
    assert {key: float(spin.value()) for key, spin in widget.durations.items()} == durations
    assert widget.answer.text() == ""
    assert widget.rating_slider.value() == 50
    assert not widget._rating_touched
    assert not widget.rating_button.isEnabled()
    assert widget.session_path_label.text() == ""
    assert not widget.restart_button.isVisible()
    assert not widget.open_folder_button.isVisible()
    assert not widget.subject_id.isEnabled()
    assert not widget.create_button.isEnabled()
    assert not replacement.data["ready"]
    assert all(not device["connected"] and not device["ready"]
               for device in replacement.data["devices"].values())
    assert widget.cards["ppg"].device_picker.currentData() == "COM7"
    if temperature_enabled:
        assert widget.cards["temperature"].device_picker.currentData() == "COM8"
    if simulate:
        assert widget.cards["eeg"].device_picker.currentData() == "00:11:22:33:44:55"

    replacement.set_ready()
    widget.refresh()
    assert widget.subject_id.isEnabled()
    errors = []
    widget._error = errors.append
    qtbot.mouseClick(widget.create_button, QtCore.Qt.MouseButton.LeftButton)
    assert replacement.created is None
    assert "被试编号" in errors[-1]
    widget.subject_id.setText("NEXT")
    qtbot.mouseClick(widget.create_button, QtCore.Qt.MouseButton.LeftButton)
    assert replacement.created is None
    assert "性别" in errors[-1]
    widget.sex.setCurrentIndex(1)
    if temperature_enabled:
        qtbot.mouseClick(widget.create_button, QtCore.Qt.MouseButton.LeftButton)
        assert replacement.created is None
        assert "温度测量部位" in errors[-1]
        widget.temperature_site.setText("左手背")
    qtbot.mouseClick(widget.create_button, QtCore.Qt.MouseButton.LeftButton)
    participant = dict(id="NEXT", age=25, sex="female", note="")
    if temperature_enabled:
        participant["temperature_site"] = "左手背"
    assert replacement.created == (participant, durations, output_root)
    assert widget.pages.currentIndex() == 1
    # Controllers reuse scene IDs: the new session must still record its first presentation.
    qtbot.waitUntil(lambda: any(scene_id == 1 for scene_id, _ in replacement.presented))
    qtbot.mouseClick(widget.stage_action, QtCore.Qt.MouseButton.LeftButton)
    assert replacement.data["state"] == "running"


def test_repeat_waits_for_close_without_blocking_gui_or_starting_twice(
        window, qtbot, monkeypatch, replacement_controllers):
    widget, controller = window
    controller.data.update(state="completed", session_path="C:/test/PREVIOUS")
    widget.refresh()
    started, release = threading.Event(), threading.Event()
    close_threads = []
    original_close = controller.close

    def slow_close():
        close_threads.append(threading.get_ident())
        started.set()
        if not release.wait(3):
            raise RuntimeError("测试关闭超时")
        original_close()

    monkeypatch.setattr(controller, "close", slow_close)
    try:
        qtbot.mouseClick(widget.restart_button, QtCore.Qt.MouseButton.LeftButton)
        qtbot.waitUntil(started.is_set)
        heartbeat = []
        QtCore.QTimer.singleShot(0, lambda: heartbeat.append(True))
        qtbot.waitUntil(lambda: bool(heartbeat))
        assert not widget.restart_button.isEnabled()
        assert widget.controller is controller
        assert widget.pages.currentIndex() == 1
        assert not replacement_controllers
        widget._restart_experiment()
        qtbot.mouseClick(widget.restart_button, QtCore.Qt.MouseButton.LeftButton)
        assert len(close_threads) == 1
        assert close_threads[0] != threading.get_ident()
    finally:
        release.set()
    qtbot.waitUntil(lambda: bool(replacement_controllers) and not widget._busy)
    assert len(replacement_controllers) == 1
    assert controller.closed
    assert widget.pages.currentIndex() == 0


def test_repeat_close_failure_keeps_previous_result_and_allows_retry(
        window, qtbot, monkeypatch, replacement_controllers):
    widget, controller = window
    controller.data.update(state="completed", session_path="C:/test/PREVIOUS")
    widget.subject_id.setText("PREVIOUS")
    widget.refresh()
    original_close = controller.close
    previous_data = controller.snapshot()
    errors = []
    widget._error = errors.append

    def failed_close():
        raise RuntimeError("设备关闭失败")

    monkeypatch.setattr(controller, "close", failed_close)
    qtbot.mouseClick(widget.restart_button, QtCore.Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: bool(errors) and not widget._busy)
    assert "设备关闭失败" in str(errors[0])
    assert widget.controller is controller
    assert not replacement_controllers
    assert controller.snapshot() == previous_data
    assert widget.subject_id.text() == "PREVIOUS"
    assert widget.pages.currentIndex() == 1
    assert "C:/test/PREVIOUS" in widget.session_path_label.text()
    assert widget.open_folder_button.isVisible()
    assert widget.restart_button.isEnabled()
    assert widget.timer.isActive()

    monkeypatch.setattr(controller, "close", original_close)
    qtbot.mouseClick(widget.restart_button, QtCore.Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: bool(replacement_controllers) and not widget._busy)
    assert widget.pages.currentIndex() == 0


@pytest.mark.parametrize("state,saving", [
    ("setup", False), ("instruction", False), ("running", False), ("rating", False),
    ("interrupted", False), ("saving", True), ("error", False),
    ("completed", True), ("aborted", True),
])
def test_repeat_is_unavailable_until_session_is_finished_and_saved(
        window, replacement_controllers, state, saving):
    widget, controller = window
    controller.data.update(state=state, saving=saving,
                           session_path="" if state == "setup" else "C:/test/PREVIOUS")
    widget.refresh()
    if state in ("completed", "aborted"):
        assert not widget.restart_button.isEnabled()
    else:
        assert not widget.restart_button.isVisible()
    widget._restart_experiment()
    assert not controller.closed
    assert widget.controller is controller
    assert not replacement_controllers


def test_repeat_waits_for_outstanding_device_operation(window, qtbot, replacement_controllers):
    widget, controller = window
    controller.data.update(state="aborted", session_path="C:/test/PREVIOUS")
    release = threading.Event()
    widget._run_async("scan_eeg", lambda: release.wait(3))
    try:
        widget.refresh()
        assert widget.restart_button.isVisible()
        assert not widget.restart_button.isEnabled()
        widget._restart_experiment()
        assert not controller.closed
        assert not replacement_controllers
    finally:
        release.set()
    qtbot.waitUntil(lambda: not widget._busy)
    assert widget.restart_button.isEnabled()
