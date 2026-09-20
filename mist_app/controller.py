"""Experiment state machine, independent of Qt and driven by one monotonic clock."""
from __future__ import annotations

import math
import re
import threading
import time
from collections import deque
from pathlib import Path

from .clock import SessionClock
from .devices import (EEGDevice, PPGDevice, TemperatureDevice, SimulatedDevice,
                      list_ppg, list_temperature, scan_eeg)
from .models import DEFAULT_STAGES, DeviceMessage, EEG_UV_PER_COUNT, Stage
from .paradigm import FEEDBACK_SECONDS, FIXATION_SECONDS, MistTask
from .storage import SessionRecorder, safe_id

INSTRUCTIONS = {
    "eyes_open": ("睁眼静息", "请保持安静，睁眼注视中央十字，放松身体，尽量减少眨眼和动作。"),
    "eyes_closed": ("闭眼静息", "点击开始后请闭眼，保持清醒和放松。听到结束提示音后再睁眼。"),
    "practice": ("算术练习", "请输入算式的整数答案，按 Enter 提交。答案可能为负数。每题最多 8 秒。"),
    "control": ("对照任务", "尽量准确地完成算术题。每题最多 10 秒，输入答案后按 Enter。"),
    "stress": ("压力任务", "请尽可能又快又准确地回答。倒计时会随表现调整，请努力达到画面中的目标。"),
    "recovery": ("恢复阶段", "任务已结束。请睁眼安静休息，注视中央十字，让呼吸自然恢复。"),
}


class DeviceHealth:
    def __init__(self, device="ppg"):
        self.device = device
        # The temperature stream is low frequency. Do not apply the EEG/PPG
        # half-second continuity requirement to ~1 Hz temperature reports.
        self.continuity_ns = 1_500_000_000 if device == "temperature" else 500_000_000
        self.stale_ns = 3_000_000_000
        self.connected = False
        self.first_valid = None
        self.last_valid = None
        self.last_message = 0
        self.samples = 0
        self.sequence = 0
        self.error = ""
        self.identifier = ""
        self.meta = {}
        self.rates = deque()
        self.preview = deque(maxlen=1500)
        self.recent = deque(maxlen=1000)

    def update(self, msg):
        self.last_message = max(self.last_message, msg.received_ns)
        if msg.kind == "connected":
            self.connected = True
            self.first_valid = self.last_valid = None
            self.rates.clear()
            self.preview.clear()
            self.recent.clear()
            self.meta.clear()
            self.meta.update(msg.meta)
            self.error = ""
        elif msg.kind in ("disconnected", "error"):
            self.connected = False
            self.first_valid = None
            self.error = msg.error or "设备已断开"
        if msg.kind != "packet":
            return
        self.sequence += 1
        self.recent.append((msg, self.sequence))
        self.meta.update(msg.meta)
        if msg.error:
            self.error = msg.error
        if msg.samples:
            if self.last_valid is None or msg.received_ns - self.last_valid >= self.continuity_ns:
                self.first_valid = msg.received_ns
            self.last_valid = msg.received_ns
            self.samples += len(msg.samples)
            self.rates.append((msg.received_ns, len(msg.samples)))
            self.error = msg.error
            while self.rates and msg.received_ns - self.rates[0][0] > 5_000_000_000:
                self.rates.popleft()
            for i, values in enumerate(msg.samples):
                if msg.device == "eeg":
                    at = msg.received_ns - (7-i) * 2_000_000
                    values = tuple(v * EEG_UV_PER_COUNT for v in values)
                else:
                    at = msg.received_ns
                self.preview.append((at / 1e9, values))

    def ready(self, now):
        return bool(self.connected and self.first_valid is not None and self.last_valid is not None
            and now - self.last_valid < self.continuity_ns and self.last_valid - self.first_valid >= 3_000_000_000)

    def snapshot(self, now, saved):
        age = (now - self.last_valid) / 1e9 if self.last_valid is not None else None
        rate = 0.0
        if len(self.rates) >= 2:
            span = (self.rates[-1][0] - self.rates[0][0]) / 1e9
            if span > 0:
                rate = sum(count for _, count in list(self.rates)[1:]) / span
        return {"connected": self.connected, "ready": self.ready(now),
            "streaming": bool(self.connected and age is not None and age < 3),
            "rate": rate if age is not None and age < 3 else 0,
            "samples": self.samples, "saved_samples": saved, "last_age": age,
            "error": self.error, "identifier": self.identifier,
            "temperature_c": self.preview[-1][1][0] if self.device == "temperature" and self.preview else None,
            "electrode_off": self.meta.get("electrode_off", self.meta.get("lead_off")),
            "battery_raw": self.meta.get("battery_raw"), "preview": list(self.preview)}


class ExperimentController:
    def __init__(self, simulate=False, output_root=None, temperature_enabled=True):
        self.simulate = simulate
        self.temperature_enabled = bool(temperature_enabled)
        self.output_root = Path(output_root or Path.home() / "Desktop" / "MIST_data")
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._health = {d: DeviceHealth(d) for d in ("eeg", "ppg", "temperature")}
        self._connecting = set()
        self._devices = {
            "eeg": SimulatedDevice("eeg", self._on_message) if simulate else EEGDevice(self._on_message),
            "ppg": SimulatedDevice("ppg", self._on_message) if simulate else PPGDevice(self._on_message),
            "temperature": SimulatedDevice("temperature", self._on_message) if simulate else TemperatureDevice(self._on_message),
        }
        self.state = "setup"
        self.stages = list(DEFAULT_STAGES)
        self.stage_index = 0
        self.attempt = 0
        self._attempt_numbers = {}
        self.recorder = None
        self.clock = None
        self.window = None
        self.last_error = ""
        self._scene_id = 0
        self._scene = {}
        self._scene_pending = False
        self._scene_at = 0
        self._scene_requested = 0
        self._phase = ""
        self._phase_deadline = 0
        self._pending_start = False
        self._task = None
        self._question = None
        self._trial_number = 0
        self._attempt_trials = []
        self._control_trials = []
        self._close_done = None
        self._finish_done = None
        self._finish_status = None
        self._after_save = "rating"
        self._release_devices = False
        self._closed = False
        self._set_scene("instruction", "连接设备", "连接脑环、指夹及已启用的温度传感器，连续收到有效数据 3 秒后填写被试信息。")
        self._thread = threading.Thread(target=self._run, name="experiment-clock", daemon=True)
        self._thread.start()

    def scan_eeg(self):
        return [{"name": "模拟 QX-EEG-4", "address": "SIM-EEG"}] if self.simulate else scan_eeg()

    def list_ppg(self):
        return [{"device": "SIM-PPG", "description": "模拟 RED PPG"}] if self.simulate else list_ppg()

    def list_temperature(self):
        return [{"device": "SIM-TEMPERATURE", "description": "模拟 GT-M601 温度"}] if self.simulate else list_temperature()

    @property
    def enabled_devices(self):
        return ("eeg", "ppg", "temperature") if self.temperature_enabled else ("eeg", "ppg")

    def set_temperature_enabled(self, enabled):
        with self._lock:
            if self.state != "setup" or self.recorder:
                raise RuntimeError("建立会话后不能更改温度采集配置。")
            enabled = bool(enabled)
            if enabled == self.temperature_enabled:
                return
            self.temperature_enabled = enabled
        if not enabled:
            self._devices["temperature"].disconnect()
            with self._lock:
                self._connecting.discard("temperature")
                self._health["temperature"] = DeviceHealth("temperature")

    def connect_eeg(self, address):
        self._connect("eeg", address)

    def connect_ppg(self, port):
        self._connect("ppg", port)

    def connect_temperature(self, port):
        self._connect("temperature", port)

    def _connect(self, device, identifier):
        identifier = str(identifier).strip()
        if not str(identifier).strip():
            raise ValueError("请先选择设备或填写连接地址。")
        with self._lock:
            if self.state in ("running", "saving", "completed", "aborted", "error"):
                raise RuntimeError("当前状态不能重新连接；请等待阶段结束或重新打开程序。")
            if device not in self.enabled_devices:
                raise RuntimeError("请先启用温度采集。")
            if device in self._connecting or self._health[device].connected:
                raise RuntimeError("设备已经连接或正在连接，请先断开。")
            if device in ("ppg", "temperature"):
                other = "temperature" if device == "ppg" else "ppg"
                canonical = lambda port: str(port).strip().upper().removeprefix("\\\\.\\")
                if (self._health[other].connected or other in self._connecting) and canonical(self._health[other].identifier) == canonical(identifier):
                    raise ValueError("指夹与温度传感器不能使用同一个串口，请选择各自的 USB 设备。")
            self._health[device].identifier = str(identifier)
            self._health[device].error = ""
            self._connecting.add(device)
            try:
                self._devices[device].connect(str(identifier))
            except Exception:
                self._connecting.discard(device)
                raise

    def disconnect_device(self, device):
        if device not in self._devices:
            raise ValueError("未知设备。")
        with self._lock:
            self._health[device].connected = False
            if self.state == "running":
                self._end_stage(time.perf_counter_ns(), False, f"{device} 主动断开")
        self._devices[device].disconnect()

    def _ready(self, now):
        return all(self._health[d].ready(now) for d in self.enabled_devices)

    def create_session(self, participant, durations, output_root=None):
        with self._lock:
            if self.state != "setup" or self.recorder:
                raise RuntimeError("已经建立会话；请完成或退出后重新开始。")
            if not self._ready(time.perf_counter_ns()):
                raise RuntimeError("所有已启用设备均需连续收到有效数据至少 3 秒。")
            participant = dict(participant)
            if self.temperature_enabled:
                site = str(participant.get("temperature_site", "")).strip()
                if not site:
                    raise ValueError("请填写温度测量部位，例如左前臂皮肤。")
                participant["temperature_site"] = site
            participant["id"] = safe_id(str(participant.get("id", participant.get("participant_id", ""))))
            try:
                age = int(participant.get("age", 0))
            except (ValueError, TypeError):
                raise ValueError("请填写有效年龄。") from None
            if not 1 <= age <= 120 or not str(participant.get("sex", participant.get("gender", ""))).strip():
                raise ValueError("年龄应为 1–120 岁，并请选择性别。")
            participant["age"] = age
            stages = []
            for stage in DEFAULT_STAGES:
                duration = float(durations.get(stage.key, stage.duration))
                if not math.isfinite(duration) or not 1 <= duration <= 3600:
                    raise ValueError("阶段时长应为 1–3600 秒。")
                stages.append(Stage(stage.key, stage.name, duration))
            self.clock = SessionClock()
            self.recorder = SessionRecorder(Path(output_root or self.output_root), participant, stages, self.simulate, self.clock,
                enabled_devices=self.enabled_devices,
                temperature_config={"model": "GT-M601", "transport": "serial_usb_or_bluetooth_receiver",
                    "port": self._health["temperature"].identifier, "baud_rate": 115200,
                    "protocol": "gt-m601-ascii", "measurement_site": participant.get("temperature_site", ""),
                    "continuity_seconds": 1.5, "stale_seconds": 3.0,
                    "timing_basis": "host_receive"} if self.temperature_enabled else None)
            self.stages = stages
            self.state = "instruction"
            self.recorder.event("session_created", time.perf_counter_ns(),
                devices={d: self._health[d].identifier for d in self.enabled_devices})
            self._instruction()

    def _instruction(self):
        stage = self.stages[self.stage_index]
        title, detail = INSTRUCTIONS[stage.key]
        self._set_scene("instruction", title, detail)

    def _set_scene(self, kind, text="", detail="", **extra):
        self._scene_id += 1
        self._scene_requested = time.perf_counter_ns()
        self._scene = {"id": self._scene_id, "kind": kind, "text": text, "detail": detail,
            "question": "", "feedback": "", "deadline_seconds": 0.0, "comparison": "", **extra}
        self._scene_pending = True
        if self.recorder:
            self.recorder.event("scene_scheduled", self._scene_requested,
                scene_id=self._scene_id, scene_kind=kind, stage=self.stages[self.stage_index].key,
                attempt=self.attempt, planned_ns=self._scene_requested)

    def start_stage(self):
        with self._lock:
            if self.state not in ("instruction", "interrupted"):
                raise RuntimeError("当前阶段尚不能开始。")
            if not self._ready(time.perf_counter_ns()):
                raise RuntimeError("请先恢复所有已启用设备并等待连续 3 秒有效数据。")
            stage = self.stages[self.stage_index]
            self.attempt = self._attempt_numbers.get(stage.key, 0) + 1
            self._attempt_numbers[stage.key] = self.attempt
            try:
                self.window = self.recorder.prepare(stage, self.attempt)
            except Exception as exc:
                self.state = "error"
                self.last_error = str(exc)
                raise
            self._attempt_trials = []
            self._trial_number = 0
            self._question = None
            self._close_done = None
            self._after_save = "rating"
            self._pending_start = True
            self.state = "running"
            self.last_error = ""
            self._task = MistTask(stage.key, self._control_trials) if stage.key in ("practice", "control", "stress") else None
            self._phase = "fixation" if self._task else "rest"
            self._phase_deadline = 0
            self._set_scene(self._phase, "+", "闭眼休息，等待结束提示音" if stage.key == "eyes_closed" else "")

    def mark_presented(self, scene_id, at_ns=None):
        with self._lock:
            if scene_id != self._scene_id or not self._scene_pending:
                return
            now = time.perf_counter_ns()
            at = now if at_ns is None else max(self._scene_requested, min(int(at_ns), now))
            self._scene_at = at
            self._scene_pending = False
            if self.recorder:
                self.recorder.event("scene_presented", at, scene_id=scene_id, scene_kind=self._scene["kind"],
                    scheduled_ns=self._scene_requested, software_draw_ns=at,
                    presentation_proxy="Qt paint completion; not physical screen onset")
            if self.state != "running":
                return
            if self._pending_start:
                self._pending_start = False
                self.window.start_ns = at
                self.window.end_ns = at + int(self.window.stage.duration * 1e9)
                self.window.status = "recording"
                self.recorder.event("stage_start", at, stage=self.window.stage.key, attempt=self.attempt,
                    planned_end_ns=self.window.end_ns)
                # The paint callback may be queued behind device callbacks: recover its tiny pre-roll.
                for device in self.enabled_devices:
                    health = self._health[device]
                    for msg, seq in health.recent:
                        if at <= msg.received_ns < self.window.end_ns:
                            self.recorder.packet(msg, seq)
            if self._phase == "fixation":
                self._phase_deadline = at + int(FIXATION_SECONDS * 1e9)
            elif self._phase == "question" and self._question:
                self._question["question_presented_ns"] = at
                self._phase_deadline = at + int(self._question["limit_seconds"] * 1e9)
            elif self._phase == "feedback":
                self._phase_deadline = at + int(FEEDBACK_SECONDS * 1e9)
            elif self._phase == "iti":
                self._phase_deadline = at + int(self._task.iti_seconds() * 1e9)

    def _on_message(self, msg: DeviceMessage):
        with self._lock:
            if msg.kind in ("connected", "disconnected", "error"):
                self._connecting.discard(msg.device)
            if msg.device not in self.enabled_devices:
                return
            health = self._health[msg.device]
            health.update(msg)
            if self.recorder and msg.kind != "packet":
                self.recorder.event("device_" + msg.kind, msg.received_ns,
                    device=msg.device, error=msg.error, meta=msg.meta)
            if msg.kind == "packet" and self.window and self.state in ("running", "saving") and not self._pending_start and self._close_done is None:
                self.recorder.packet(msg, health.sequence)
            if msg.kind in ("disconnected", "error") and self.state == "running":
                # Other-source packets may already have been written after the callback's
                # receive timestamp. Stop at observation time, retaining fault time above.
                self._end_stage(time.perf_counter_ns(), False, msg.error or f"{msg.device} 已断开")

    def _new_question(self):
        question = self._task.new_question()
        self._trial_number += 1
        self._question = {**question, "trial": self._trial_number, "question_presented_ns": None}
        self._phase = "question"
        self._set_scene("question", "请计算", question=question["prompt"], comparison=question["comparison"],
            deadline_total=question["limit_seconds"])
        self._question["question_scheduled_ns"] = self._scene_requested

    def submit_answer(self, text):
        text = str(text).strip()
        if not re.fullmatch(r"-?\d{1,9}", text):
            raise ValueError("请输入整数答案，可使用负号。")
        with self._lock:
            now = time.perf_counter_ns()
            if self.state != "running" or self._phase != "question" or self._scene_pending:
                return
            if now >= self.window.end_ns:
                self._end_stage(self.window.end_ns, True)
            elif now >= self._phase_deadline:
                self._answer(None, self._phase_deadline, "timeout")
            else:
                self._answer(int(text), now, "answer")

    def mark_audio_requested(self, reason="eyes_closed_end"):
        with self._lock:
            if self.recorder:
                self.recorder.event("audio_requested", time.perf_counter_ns(), reason=reason,
                    stage=self.stages[self.stage_index].key, attempt=self.attempt,
                    timing_basis="system beep requested; not measured acoustic onset")

    def _answer(self, value, at, outcome):
        question = self._question
        if not question:
            return
        onset = question.get("question_presented_ns")
        correct = outcome == "answer" and value == question["correct_answer"]
        rt = (at - onset) / 1e9 if onset is not None and outcome == "answer" else None
        row = {"stage": self.window.stage.key, "attempt": self.attempt,
            "trial": question["trial"], "prompt": question["prompt"],
            "correct_answer": question["correct_answer"], "answer": value,
            "correct": correct, "outcome": outcome, "rt": rt,
            "limit_seconds": question["limit_seconds"],
            "question_scheduled_ns": question["question_scheduled_ns"],
            "question_presented_ns": onset, "response_ns": at}
        self.recorder.trial(row)
        self.recorder.event("trial_result", at, **row)
        self._question = None
        if outcome not in ("answer", "timeout"):
            return
        self._attempt_trials.append(row)
        self._task.record_result(correct, rt, outcome == "timeout")
        self._phase = "feedback"
        feedback = self._task.feedback(correct, outcome == "timeout")
        self._set_scene("feedback", feedback, feedback=feedback)

    def _end_stage(self, at, complete, reason=""):
        if self.state != "running":
            return
        if self._question:
            self._answer(None, at, "stage_truncated" if complete else "interrupted")
        if self._pending_start:
            self.window.start_ns = at
            self._pending_start = False
        self.window.end_ns = max(self.window.start_ns, min(at, self.window.end_ns or at))
        self.window.status = "completed" if complete else "incomplete"
        self.window.reason = reason
        self._after_save = "rating" if complete else "interrupted"
        self.last_error = reason
        self.state = "saving"
        self.recorder.event("stage_end", self.window.end_ns, stage=self.window.stage.key,
            attempt=self.attempt, status=self.window.status, reason=reason)
        self._set_scene("instruction", "正在保存本阶段", "正在收齐接收队列并关闭文件。")

    def abort_stage(self, reason="operator"):
        with self._lock:
            if self.state == "running":
                self._end_stage(time.perf_counter_ns(), False, reason)

    def submit_rating(self, value):
        with self._lock:
            if self.state != "rating":
                raise RuntimeError("当前不在评分环节。")
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
                raise ValueError("评分必须是 0–100 的整数。")
            at = time.perf_counter_ns()
            self.recorder.rating({"stage": self.window.stage.key, "attempt": self.attempt,
                "rating": value, "at_ns": at, "session_seconds": self.clock.relative(at)})
            self.recorder.event("rating", at, stage=self.window.stage.key, attempt=self.attempt, value=value)
            if self.window.stage.key == "control":
                self._control_trials = list(self._attempt_trials)
            if self.stage_index == len(self.stages) - 1:
                self._finish_status = "completed"
                self._finish_done = self.recorder.finish("completed")
                self.state = "saving"
                self._set_scene("instruction", "正在保存实验汇总")
            else:
                self.stage_index += 1
                self.state = "instruction"
                self.window = None
                self._instruction()

    def abort_session(self):
        with self._lock:
            if self.state in ("completed", "aborted", "error"):
                return
            if not self.recorder:
                self.state = "aborted"
                self._release_devices = True
                return
            if self.state == "running":
                self._end_stage(time.perf_counter_ns(), False, "操作员结束整场实验")
            if self.state == "saving" and self._finish_done is None:
                self._after_save = "aborted"
            else:
                self._finish_status = "aborted"
                self._finish_done = self.recorder.finish("aborted")
                self.state = "saving"

    def _tick(self, now):
        if self.recorder and self.recorder.error and self.state != "error":
            self.last_error = self.recorder.error
            if self.window and self.state == "running":
                if self.window.status != "incomplete":
                    self.window.end_ns = min(self.window.end_ns or now, now)
                    self.window.status = "incomplete"
                self.window.reason = self.last_error
            self.state = "error"
            self._release_devices = True
            self._set_scene("instruction", "记录发生错误，实验已停止", self.last_error)
        if self.state == "running":
            for device in self.enabled_devices:
                health = self._health[device]
                if not health.connected or health.last_valid is None or now - health.last_valid >= health.stale_ns:
                    self._end_stage(now, False, f"{device} 断开或连续 3 秒无有效数据")
                    break
        if self.state == "running":
            if not self._pending_start and now >= self.window.end_ns:
                self._end_stage(self.window.end_ns, True)
            elif self._scene_pending and now - self._scene_requested > 2_000_000_000:
                self._end_stage(now, False, "界面未及时呈现刺激，请恢复窗口后重做阶段")
            elif not self._scene_pending and self._phase_deadline and now >= self._phase_deadline:
                if self._phase == "fixation":
                    self._new_question()
                elif self._phase == "question":
                    self._answer(None, self._phase_deadline, "timeout")
                elif self._phase == "feedback":
                    self._phase = "iti"
                    self._set_scene("fixation", "")
                elif self._phase == "iti":
                    self._phase = "fixation"
                    self._set_scene("fixation", "+")
        if self.state == "saving":
            if self._finish_done is not None:
                if self._finish_done.is_set():
                    self.state = self._finish_status
                    self._release_devices = True
                    self._set_scene("complete", "实验已完成" if self.state == "completed" else "实验已结束",
                        "感谢参与。同伴平均和目标成绩用于构造实验压力情境，不代表对您个人能力的评价。所有已采数据均已保存在本机会话文件夹。")
                return
            if self.window is not None:
                # Each enabled source delivers in order. Wait for receive watermarks to pass the boundary.
                drained = all(h.last_message >= self.window.end_ns or not h.connected or
                    now - (h.last_valid or self.window.end_ns) >= h.stale_ns
                    for h in (self._health[d] for d in self.enabled_devices))
                if self._close_done is None and drained:
                    self._close_done = self.recorder.finish_stage(self.window)
                if self._close_done is not None and self._close_done.is_set():
                    if self._after_save == "aborted":
                        self._finish_status = "aborted"
                        self._finish_done = self.recorder.finish("aborted")
                    else:
                        self.state = self._after_save
                        if self.state == "rating":
                            self._set_scene("rating", "此刻的压力有多大？", "0 = 完全没有压力　　100 = 极大的压力")
                        else:
                            self._set_scene("instruction", "本阶段未完成", self.last_error + "。重连并就绪后重新开始，旧文件会保留。")

    def _run(self):
        while not self._stop.wait(.01):
            try:
                with self._lock:
                    self._tick(time.perf_counter_ns())
                    release = self._release_devices
                    self._release_devices = False
                if release:
                    for adapter in self._devices.values():
                        adapter.disconnect()
            except Exception as exc:
                with self._lock:
                    self.last_error = f"实验控制错误：{exc}"
                    if self.state == "running":
                        self._end_stage(time.perf_counter_ns(), False, self.last_error)
                    else:
                        self.state = "error"

    def snapshot(self):
        with self._lock:
            now = time.perf_counter_ns()
            stage = self.stages[self.stage_index]
            remaining = max(0, (self.window.end_ns - now) / 1e9) if self.window and not self._pending_start and self.state == "running" else stage.duration
            scene = dict(self._scene)
            if self.state == "running" and self._phase == "question" and not self._scene_pending:
                scene["deadline_seconds"] = max(0, (self._phase_deadline - now) / 1e9)
            return {"mode": "simulate" if self.simulate else "hardware", "state": self.state,
                "ready": self._ready(now),
                "temperature_enabled": self.temperature_enabled,
                "devices": {d: {**h.snapshot(now, self.recorder.saved_counts.get(d, 0) if self.recorder else 0),
                    "enabled": d in self.enabled_devices} for d, h in self._health.items()},
                "session_path": str(self.recorder.path) if self.recorder else "",
                "stage_index": self.stage_index, "stage_key": stage.key, "stage_name": stage.name,
                "duration": stage.duration, "remaining": remaining, "attempt": self.attempt,
                "scene": scene, "last_error": self.last_error, "saving": self.state == "saving"}

    def close(self):
        if self._closed:
            return
        self.abort_session()
        for adapter in self._devices.values():
            adapter.disconnect()
        deadline = time.monotonic() + 10
        while self.state == "saving" and time.monotonic() < deadline:
            with self._lock:
                self._tick(time.perf_counter_ns())
            time.sleep(.01)
        self._stop.set()
        self._thread.join(timeout=2)
        if self.recorder:
            self.recorder.shutdown()
        self._closed = True
