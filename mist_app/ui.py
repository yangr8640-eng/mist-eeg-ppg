"""Chinese desktop interface for the combined MIST acquisition controller."""
from __future__ import annotations

import argparse
import math
from pathlib import Path
import sys
import threading
import time
from typing import Any, Callable

from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

from .controller import ExperimentController
from .models import DEFAULT_STAGES


STYLE = """
QWidget { font-family: 'Microsoft YaHei UI', 'Microsoft YaHei', sans-serif;
          font-size: 13px; color: #193449; }
QMainWindow, QWidget#root, QWidget#setupContent { background: #edf5fa; }
QFrame#panel, QGroupBox { background: #ffffff; border: 1px solid #dce8f0;
                        border-radius: 14px; }
QGroupBox { margin-top: 14px; padding: 18px 16px 12px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 16px; padding: 0 5px; }
QLabel#title { font-size: 25px; font-weight: 700; color: #163b55; }
QLabel#subtitle, QLabel#muted { color: #698193; }
QLabel#eyebrow { color: #148fa9; font-size: 11px; font-weight: 700; }
QLabel#sectionTitle { font-size: 19px; font-weight: 650; }
QLabel#state { background: #e7f7fa; color: #08778b; padding: 6px 10px;
               border-radius: 10px; font-weight: 600; }
QLabel#warning { background: #fff1dc; color: #99621c; padding: 9px;
                 border-radius: 8px; }
QPushButton { background: #ffffff; border: 1px solid #cbdfe9; border-radius: 8px;
              padding: 8px 12px; min-height: 20px; }
QPushButton:hover { background: #edf8fc; border-color: #88c6da; }
QPushButton:pressed { background: #dcf0f7; }
QPushButton#primary { background: #127da4; color: #ffffff; border: none;
                      font-weight: 600; padding: 11px 20px; }
QPushButton#primary:hover { background: #08688e; }
QPushButton#danger { color: #ab4c50; border-color: #e5c4c5; }
QPushButton:disabled { background: #ecf1f5; color: #9aadb9; border-color: #e0e8ed; }
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit { background: #f7fafc;
    border: 1px solid #cfdee7; border-radius: 7px; padding: 7px; min-height: 18px; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus {
    border: 1px solid #279cba; background: #ffffff; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QPlainTextEdit:disabled {
    color: #99aab6; background: #f1f5f8; }
QProgressBar { background: #dfecf3; border: none; border-radius: 3px; height: 6px; }
QProgressBar::chunk { background: #28a6bd; border-radius: 3px; }
QSlider::groove:horizontal { height: 7px; background: #dfebf1; border-radius: 3px; }
QSlider::sub-page:horizontal { background: #2aa9be; border-radius: 3px; }
QSlider::handle:horizontal { background: #1383a4; border: 3px solid #e5f7fa;
                           width: 18px; height: 18px; margin: -8px 0; border-radius: 11px; }
QScrollArea { border: none; background: transparent; }
QToolTip { background: #193e53; color: white; padding: 7px; border: none; }
"""


def label(text: str, name: str = "", wrap: bool = False) -> QtWidgets.QLabel:
    widget = QtWidgets.QLabel(text)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    return widget


def button(text: str, callback: Callable, name: str = "") -> QtWidgets.QPushButton:
    widget = QtWidgets.QPushButton(text)
    widget.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
    widget.clicked.connect(callback)
    if name:
        widget.setObjectName(name)
    return widget


class TaskCanvas(QtWidgets.QWidget):
    """Draw the task and report a software presentation proxy once per scene.

    This timestamp is after application painting, not a display scanout or
    photodiode measurement. Delivery is queued to avoid controller mutation
    from inside the paint event.
    """

    presented = QtCore.Signal(int, object)

    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.scene: dict[str, Any] = {}
        self._presented_id: int | None = None
        self.setMinimumHeight(285)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)

    def set_scene(self, scene: dict[str, Any]) -> None:
        if scene != self.scene:
            self.scene = dict(scene)
            self.update()

    def reset(self) -> None:
        self._presented_id = None
        self.scene = {}
        self.update()

    def _text(self, painter: QtGui.QPainter, rect: QtCore.QRectF, text: str,
              size: int, color: str = "#193449", bold: bool = False) -> None:
        font = QtGui.QFont("Microsoft YaHei UI", size)
        font.setBold(bold)
        painter.setFont(font)
        painter.setPen(QtGui.QColor(color))
        painter.drawText(rect, int(QtCore.Qt.AlignmentFlag.AlignCenter | QtCore.Qt.TextFlag.TextWordWrap), str(text))

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        super().paintEvent(event)
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        area = self.rect().adjusted(1, 1, -1, -1)
        p.setPen(QtGui.QPen(QtGui.QColor("#dce8f0"), 1))
        p.setBrush(QtGui.QColor("#ffffff"))
        p.drawRoundedRect(area, 18, 18)
        width, height = self.width(), self.height()
        kind = self.scene.get("kind", "instruction")
        text = self.scene.get("text", "准备开始")
        detail = self.scene.get("detail", "")
        if kind == "question":
            comparison = self.scene.get("comparison")
            if isinstance(comparison, dict):
                values = [comparison.get(k) for k in ("current", "peer", "target")]
                if all(v is None for v in values):
                    values = list(comparison.values())[:3]
                parts = []
                for caption, value in zip(("当前表现", "同伴平均", "目标表现"), values):
                    if value is not None:
                        parts.append(f"{caption}  {value:.0f}%" if isinstance(value, (int, float)) else f"{caption}  {value}")
                comparison = "      ·      ".join(parts)
            if comparison:
                self._text(p, QtCore.QRectF(25, 28, width - 50, 55), str(comparison), 13, "#b36339")
            question = self.scene.get("question") or text
            if isinstance(question, dict):
                question = question.get("expression", question.get("text", ""))
            self._text(p, QtCore.QRectF(30, height * .27, width - 60, height * .34), str(question), 36, bold=True)
            self._text(p, QtCore.QRectF(30, height * .63, width - 60, 45), detail or "输入答案，按 Enter 提交", 13, "#728898")
            seconds = max(0.0, float(self.scene.get("deadline_seconds") or 0.0))
            total = max(seconds, float(self.scene.get("deadline_total") or 10.0))
            rect = QtCore.QRectF(width * .18, height - 47, width * .64, 7)
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.setBrush(QtGui.QColor("#e3edf3"))
            p.drawRoundedRect(rect, 3, 3)
            rect.setWidth(rect.width() * min(1., seconds / max(total, .001)))
            p.setBrush(QtGui.QColor("#d7805b" if seconds < 1 else "#26a5bc"))
            p.drawRoundedRect(rect, 3, 3)
        elif kind == "fixation":
            self._text(p, QtCore.QRectF(20, 20, width - 40, height - 40), text, 54, "#214c66")
        elif kind == "rest":
            symbol = "+" if self.scene.get("fixation") or "睁眼" in str(text) or "注视" in str(text) else "○"
            self._text(p, QtCore.QRectF(25, height * .15, width - 50, height * .34), symbol, 60, "#1784a4")
            self._text(p, QtCore.QRectF(35, height * .52, width - 70, height * .2), text, 22, bold=True)
            self._text(p, QtCore.QRectF(35, height * .75, width - 70, height * .17), detail, 12, "#718796")
        else:
            if kind == "feedback":
                text = self.scene.get("feedback") or text
                if isinstance(text, dict):
                    text = text.get("text", str(text))
            color = "#168391" if kind != "feedback" or "正确" in str(text) or "记录" in str(text) else "#bd6348"
            self._text(p, QtCore.QRectF(35, height * .19, width - 70, height * .29), text, 25, color, True)
            self._text(p, QtCore.QRectF(40, height * .5, width - 80, height * .36), detail, 14, "#6c8292")
        p.end()
        scene_id = self.scene.get("id")
        if isinstance(scene_id, int) and scene_id != self._presented_id:
            self._presented_id = scene_id
            self.presented.emit(scene_id, time.perf_counter_ns())


class DeviceCard(QtWidgets.QFrame):
    def __init__(self, kind: str, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.kind = kind
        self.setObjectName("panel")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)
        row = QtWidgets.QHBoxLayout()
        title = label({"eeg": "脑环 · EEG", "ppg": "指夹 · PPG",
                       "temperature": "温度 · GT-M601"}[kind], "sectionTitle")
        title.setStyleSheet("font-size: 16px; font-weight: 600;")
        row.addWidget(title)
        row.addStretch()
        self.status = label("未连接", "state")
        row.addWidget(self.status)
        layout.addLayout(row)
        self.device_picker = QtWidgets.QComboBox()
        self.device_picker.setEditable(kind == "eeg")
        self.device_picker.setMinimumContentsLength(16)
        self.device_picker.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        if kind == "eeg":
            self.device_picker.setPlaceholderText("扫描选择，或输入蓝牙地址")
            self.device_picker.lineEdit().setPlaceholderText("扫描选择，或输入蓝牙地址")
        elif kind == "ppg":
            self.device_picker.setPlaceholderText("选择指夹串口")
        else:
            self.device_picker.setPlaceholderText("选择蓝牙接收器 / USB 串口")
        layout.addWidget(self.device_picker)
        actions = QtWidgets.QHBoxLayout()
        self.scan_button = QtWidgets.QPushButton("扫描脑环" if kind == "eeg" else "刷新串口")
        self.connect_button = QtWidgets.QPushButton("连接")
        actions.addWidget(self.scan_button)
        actions.addWidget(self.connect_button)
        layout.addLayout(actions)
        self.temperature_value = label("-- °C · 等待数据")
        self.temperature_value.setStyleSheet("font-size: 19px; font-weight: 600; color: #ae632d;")
        self.temperature_value.setVisible(kind == "temperature")
        if kind == "temperature":
            layout.addWidget(self.temperature_value)
        self.details = label("等待连接设备", "muted", True)
        self.details.setMinimumHeight(30)
        self.details.setStyleSheet("font-size: 11px;")
        layout.addWidget(self.details)
        self.plot = pg.PlotWidget(background="#f6fafc")
        self.plot.setFixedHeight(76)
        self.plot.setMouseEnabled(x=False, y=False)
        if kind == "temperature":
            self.plot.setLabel("left", "°C")
            self.plot.getAxis("left").setWidth(44)
            self.plot.setYRange(25, 40, padding=0)
        else:
            self.plot.hideAxis("left")
        self.plot.hideAxis("bottom")
        self.plot.hideButtons()
        self.plot.setMenuEnabled(False)
        self.plot.setContentsMargins(0, 0, 0, 0)
        colors = (("#158cb0", "#42ab9d", "#8e9bce", "#d2a067") if kind == "eeg"
                  else ("#d98670",) if kind == "ppg" else ("#c38030",))
        self.curves = [self.plot.plot(pen=pg.mkPen(c, width=1)) for c in colors]
        layout.addWidget(self.plot)
        self.hint = label({"eeg": "4 通道 · 500 Hz", "ppg": "串口数据 · 自动识别采样率",
                           "temperature": "蓝牙接收器 / USB · 115200 · 接触部位温度"}[kind], "muted")
        self.hint.setStyleSheet("font-size: 11px;")
        layout.addWidget(self.hint)

    def update_status(self, data: dict, recording: bool, busy: set[str], compact: bool = False) -> None:
        connected = bool(data.get("connected"))
        enabled = bool(data.get("enabled", True))
        show_controls = enabled and not (connected and compact)
        self.device_picker.setVisible(show_controls)
        self.scan_button.setVisible(show_controls)
        self.connect_button.setVisible(show_controls)
        margins = (10, 7, 10, 7) if compact else (12, 10, 12, 10)
        self.layout().setContentsMargins(*margins)
        self.layout().setSpacing(3 if compact else 6)
        self.plot.setFixedHeight((50 if self.kind == "temperature" else 42) if compact else 76)
        self.plot.setVisible(enabled)
        self.hint.setVisible(not compact)
        self.setMinimumHeight(0)
        if not enabled:
            status, color, bg = "未启用", "#7b8f9e", "#edf2f5"
        elif data.get("error"):
            status, color, bg = "连接异常", "#a44d39", "#fff0e6"
        elif self.kind == "temperature" and connected and not data.get("streaming"):
            status, color, bg = "等待更新", "#9b6e20", "#fff3db"
        elif recording and data.get("ready"):
            status, color, bg = "● 记录中", "#087e78", "#e1f5ef"
        elif data.get("ready"):
            status, color, bg = "已就绪", "#087e78", "#e1f5ef"
        elif connected:
            status, color, bg = "检查数据", "#9b6e20", "#fff3db"
        else:
            status, color, bg = "未连接", "#7b8f9e", "#edf2f5"
        self.status.setText(status)
        self.status.setStyleSheet(f"color: {color}; background: {bg}; font-size: 11px; padding: 4px 7px;")
        self.connect_button.setText("断开" if connected else "连接")
        self.connect_button.setEnabled(enabled and f"connect_{self.kind}" not in busy)
        self.scan_button.setEnabled(enabled and f"scan_{self.kind}" not in busy and not connected)
        self.device_picker.setEnabled(enabled and not connected and f"connect_{self.kind}" not in busy)
        if not enabled:
            text = "本次实验不采集温度"
        elif self.kind == "temperature":
            count = int(data.get("saved_samples") or 0)
            age = data.get("last_age")
            updated = f"{age:.1f} 秒前" if age is not None else "尚无数据"
            text = f"本阶段已存 {count:,} 点 · 最近更新 {updated}"
            if data.get("error"):
                text += f"\n{data['error']}"
            elif connected:
                text += f"\n接触部位温度 · {float(data.get('rate') or 0):.2f} Hz"
            else:
                text += "\n请选择配套蓝牙接收器 / USB 串口"
        elif data.get("error"):
            text = str(data["error"])
        elif connected:
            rate = float(data.get("rate") or 0)
            count = int(data.get("saved_samples") or 0)
            text = f"实测速率 {rate:.1f} Hz  ·  本阶段已存 {count:,} 点"
            age = data.get("last_age")
            if age is not None and age > 1:
                text += f"\n距上一包 {age:.1f} 秒"
            elif data.get("electrode_off"):
                text += "\n请检查电极佩戴与接触"
            elif not recording:
                text += "\n实时预览 · 当前未记录阶段数据"
        else:
            text = "请连接设备，连续接收有效数据后进入实验"
        self.details.setText(text)
        self.details.setToolTip(str(data.get("identifier") or ""))
        if self.kind == "temperature":
            value = data.get("temperature_c")
            valid = isinstance(value, (int, float)) and math.isfinite(value)
            current = enabled and connected and bool(data.get("streaming")) and not data.get("error") and valid
            self.temperature_value.setVisible(enabled)
            self.temperature_value.setText(f"{value:.1f} °C" if current else
                                           f"上次 {value:.1f} °C · 已过期" if valid else "-- °C · 等待有效数据")
            self.temperature_value.setToolTip("皮肤 / 接触部位温度，模块输出分辨率 0.1 °C。")
            self.temperature_value.setStyleSheet("font-size: 18px; font-weight: 600; color: "
                                                + ("#956020;" if current else "#b04c36;"))
            self.plot.setToolTip("曲线为已接收的历史温度，纵轴单位 °C，横轴为距最近样本的秒数。")

    def update_plot(self, data: dict) -> None:
        preview = data.get("preview") or []
        if len(preview) < 2:
            for curve in self.curves:
                curve.setData([], [])
            return
        # EEG/PPG normalization is display-only; temperature keeps absolute °C.
        import numpy as np
        try:
            timestamps = np.asarray([row[0] for row in preview], dtype=float)
            values = np.asarray([row[1] for row in preview], dtype=float)
            if values.ndim == 1:
                values = values[:, None]
            timestamps -= timestamps[-1]
            if self.kind == "temperature":
                series = values[:, 0]
                finite = np.isfinite(series) & np.isfinite(timestamps)
                self.curves[0].setData(timestamps[finite], series[finite])
                if finite.any():
                    low, high = float(np.min(series[finite])), float(np.max(series[finite]))
                    margin = max(.5, (high - low) * .15)
                    self.plot.setYRange(low - margin, high + margin, padding=0)
                return
            for index, curve in enumerate(self.curves):
                if index >= values.shape[1]:
                    curve.setData([], [])
                    continue
                series = values[:, index]
                centered = series - np.median(series)
                scale = max(float(np.percentile(np.abs(centered), 90)), 1.0)
                curve.setData(timestamps, centered / scale + (len(self.curves) - index - 1) * 3)
        except (TypeError, ValueError, IndexError):
            for curve in self.curves:
                curve.setData([], [])


class MainWindow(QtWidgets.QMainWindow):
    async_result = QtCore.Signal(object)

    def __init__(self, simulate: bool = False, output_root: Path | None = None,
                 controller: Any | None = None):
        super().__init__()
        self.controller = controller or ExperimentController(simulate=simulate, output_root=output_root)
        self._snapshot: dict = {}
        self._busy: set[str] = set()
        self._generation = 0
        self._closed = False
        self._last_plot = 0.0
        self._rating_touched = False
        self._last_state = ""
        self._last_scene_id = None
        self._last_stage_key = None
        self.setWindowTitle("MIST · 脑环、指夹与温度同步实验")
        self.resize(1360, 900)
        self.setMinimumSize(1080, 750)
        self.setStyleSheet(STYLE)
        root = QtWidgets.QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        layout = QtWidgets.QVBoxLayout(root)
        layout.setContentsMargins(22, 16, 22, 12)
        layout.setSpacing(12)
        header = QtWidgets.QHBoxLayout()
        titles = QtWidgets.QVBoxLayout()
        titles.setSpacing(4)
        titles.addWidget(label("MIST  /  PHYSIOLOGY LAB", "eyebrow"))
        titles.addWidget(label("脑环、指夹与温度同步实验", "title"))
        titles.addWidget(label("设备连接 → 被试信息 → 六阶段任务 → 自动保存", "subtitle"))
        header.addLayout(titles)
        header.addStretch()
        self.temperature_enabled = QtWidgets.QCheckBox("采集温度")
        self.temperature_enabled.setChecked(True)
        self.temperature_enabled.setToolTip("采集 GT-M601 皮肤 / 接触部位温度；建立会话后不可更改。")
        self.temperature_enabled.toggled.connect(self._toggle_temperature)
        header.addWidget(self.temperature_enabled)
        self.simulation = QtWidgets.QCheckBox("模拟演示")
        self.simulation.setChecked(simulate)
        self.simulation.setToolTip("使用模拟信号演练流程，所有输出会明确标记为模拟数据")
        self.simulation.toggled.connect(self._toggle_simulation)
        header.addWidget(self.simulation)
        layout.addLayout(header)
        self.simulation_banner = label("模拟演示模式 · 当前使用合成生理信号，输出不可作为真实被试数据。", "warning", True)
        layout.addWidget(self.simulation_banner)
        content = QtWidgets.QHBoxLayout()
        content.setSpacing(18)
        self.pages = QtWidgets.QStackedWidget()
        self.pages.addWidget(self._build_setup(output_root))
        self.pages.addWidget(self._build_experiment())
        content.addWidget(self.pages, 1)
        content.addWidget(self._build_sidebar())
        layout.addLayout(content, 1)
        self.footer = label("数据保存在本机 · 请保持电脑唤醒，并在实验期间维持稳定佩戴", "muted")
        self.footer.setStyleSheet("font-size: 11px;")
        layout.addWidget(self.footer)
        self.async_result.connect(self._receive_async)
        self.canvas.presented.connect(self._mark_presented, QtCore.Qt.ConnectionType.QueuedConnection)
        self.abort_shortcut = QtGui.QShortcut(QtGui.QKeySequence("Esc"), self)
        self.abort_shortcut.activated.connect(self._abort_session)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(25)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()
        self._scan("ppg")
        if self.temperature_enabled.isChecked():
            self._scan("temperature")

    def _build_setup(self, output_root: Path | None) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.gate_message = label("01  先连接右侧已启用设备", "sectionTitle")
        layout.addWidget(self.gate_message)
        layout.addWidget(label("已启用设备连续提供有效信号后，解锁被试信息。无需温度时可取消顶部勾选。", "muted", True))
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        self.setup_scroll = scroll
        inner = QtWidgets.QWidget()
        inner.setObjectName("setupContent")
        inner_layout = QtWidgets.QVBoxLayout(inner)
        inner_layout.setContentsMargins(0, 3, 4, 0)
        self.participant_group = QtWidgets.QGroupBox("02  被试信息")
        form = QtWidgets.QFormLayout(self.participant_group)
        form.setSpacing(12)
        self.subject_id = QtWidgets.QLineEdit()
        self.subject_id.setObjectName("subject_id")
        self.subject_id.setPlaceholderText("例如 P001")
        self.subject_id.setMaxLength(64)
        self.age = QtWidgets.QSpinBox()
        self.age.setRange(1, 120)
        self.age.setValue(25)
        self.sex = QtWidgets.QComboBox()
        for caption, value in (("请选择", ""), ("女", "female"), ("男", "male"), ("其他 / 不愿透露", "unspecified")):
            self.sex.addItem(caption, value)
        self.note = QtWidgets.QLineEdit()
        self.note.setPlaceholderText("选填：操作员或本次实验备注")
        form.addRow("被试编号 *", self.subject_id)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.age)
        row.addSpacing(18)
        row.addWidget(label("性别 *"))
        row.addWidget(self.sex, 1)
        form.addRow("年龄 *", row)
        self.temperature_site = QtWidgets.QLineEdit()
        self.temperature_site.setPlaceholderText("例如：左侧前臂内侧；每次保持测量位置一致")
        self.temperature_site.setMaxLength(120)
        self.temperature_site_label = label("温度测量部位 *")
        form.addRow(self.temperature_site_label, self.temperature_site)
        form.addRow("备注", self.note)
        inner_layout.addWidget(self.participant_group)
        self.config_group = QtWidgets.QGroupBox("03  实验设置")
        settings = QtWidgets.QVBoxLayout(self.config_group)
        settings.addWidget(label("每阶段默认 3 分钟，阶段结束后进行 0–100 压力评分。", "muted", True))
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(8)
        self.durations: dict[str, QtWidgets.QSpinBox] = {}
        for index, stage in enumerate(DEFAULT_STAGES):
            spin = QtWidgets.QSpinBox()
            spin.setRange(1, 3600)
            spin.setValue(int(stage.duration))
            spin.setSuffix(" 秒")
            self.durations[stage.key] = spin
            cell = QtWidgets.QVBoxLayout()
            cell.addWidget(label(f"{index + 1:02d}  {stage.name}", "muted"))
            cell.addWidget(spin)
            grid.addLayout(cell, index // 3, index % 3)
        settings.addLayout(grid)
        settings.addSpacing(8)
        settings.addWidget(label("信息与采集数据保存位置", "muted"))
        folder_row = QtWidgets.QHBoxLayout()
        self.output_root = QtWidgets.QLineEdit(str(output_root or Path.home() / "Desktop" / "MIST_data"))
        folder_row.addWidget(self.output_root, 1)
        folder_row.addWidget(button("选择文件夹", self._choose_folder))
        settings.addLayout(folder_row)
        settings.addWidget(label("将自动创建独立被试目录；每阶段单独保存 EEG、PPG、已启用的温度数据与事件信息。", "muted", True))
        inner_layout.addWidget(self.config_group)
        inner_layout.addStretch()
        scroll.setWidget(inner)
        layout.addWidget(scroll, 1)
        self.create_button = button("保存信息，进入实验", self._create_session, "primary")
        self.create_button.setObjectName("primary")
        layout.addWidget(self.create_button)
        return page

    def _build_experiment(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(13)
        heading = QtWidgets.QHBoxLayout()
        self.stage_label = label("实验准备", "sectionTitle")
        heading.addWidget(self.stage_label)
        heading.addStretch()
        self.clock_label = label("03:00", "sectionTitle")
        self.clock_label.setStyleSheet("color: #157e9c; font-size: 25px; font-weight: 600;")
        heading.addWidget(self.clock_label)
        layout.addLayout(heading)
        self.stage_progress = QtWidgets.QProgressBar()
        self.stage_progress.setRange(0, 1000)
        self.stage_progress.setTextVisible(False)
        self.stage_progress.setFixedHeight(6)
        layout.addWidget(self.stage_progress)
        self.canvas = TaskCanvas()
        layout.addWidget(self.canvas, 1)
        self.answer_panel = QtWidgets.QWidget()
        answer_layout = QtWidgets.QHBoxLayout(self.answer_panel)
        answer_layout.setContentsMargins(30, 0, 30, 0)
        self.answer = QtWidgets.QLineEdit()
        self.answer.setPlaceholderText("输入答案")
        self.answer.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.answer.setValidator(QtGui.QRegularExpressionValidator(QtCore.QRegularExpression(r"-?\d{0,9}")))
        self.answer.setStyleSheet("font-size: 25px; padding: 10px;")
        self.answer.returnPressed.connect(self._submit_answer)
        answer_layout.addWidget(self.answer, 1)
        self.answer_button = button("提交  ↵", self._submit_answer, "primary")
        answer_layout.addWidget(self.answer_button)
        layout.addWidget(self.answer_panel)
        self.rating_panel = QtWidgets.QFrame()
        self.rating_panel.setObjectName("panel")
        rating_layout = QtWidgets.QVBoxLayout(self.rating_panel)
        rating_layout.setContentsMargins(25, 16, 25, 18)
        self.rating_label = label("请拖动滑块，选择你此刻的压力程度", "sectionTitle", True)
        rating_layout.addWidget(self.rating_label)
        self.rating_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.rating_slider.setRange(0, 100)
        self.rating_slider.setValue(50)
        self.rating_slider.setAccessibleName("压力评分，0到100")
        self.rating_slider.sliderPressed.connect(self._touch_rating)
        self.rating_slider.actionTriggered.connect(self._touch_rating)
        self.rating_slider.valueChanged.connect(self._rating_changed)
        rating_layout.addWidget(self.rating_slider)
        anchors = QtWidgets.QHBoxLayout()
        anchors.addWidget(label("0  完全没有", "muted"))
        anchors.addStretch()
        anchors.addWidget(label("100  非常强烈", "muted"))
        rating_layout.addLayout(anchors)
        self.rating_button = button("确认评分，继续", self._submit_rating, "primary")
        self.rating_button.setEnabled(False)
        rating_layout.addWidget(self.rating_button)
        layout.addWidget(self.rating_panel)
        self.stage_action = button("开始本阶段", self._start_stage, "primary")
        layout.addWidget(self.stage_action)
        self.session_path_label = label("", "muted", True)
        self.session_path_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.session_path_label.setStyleSheet("font-size: 11px;")
        layout.addWidget(self.session_path_label)
        self.open_folder_button = button("打开数据文件夹", self._open_folder)
        layout.addWidget(self.open_folder_button)
        self.restart_button = button("再次实验", self._restart_experiment, "primary")
        self.restart_button.setToolTip("返回设备连接与被试信息页，开始一次新的实验。已保存的数据会保留。")
        layout.addWidget(self.restart_button)
        return page

    def _build_sidebar(self) -> QtWidgets.QWidget:
        sidebar = QtWidgets.QWidget()
        sidebar.setFixedWidth(320)
        layout = QtWidgets.QVBoxLayout(sidebar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(label("实时设备状态", "sectionTitle"))
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_content = QtWidgets.QWidget()
        scroll_content.setObjectName("setupContent")
        scroll_layout = QtWidgets.QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(6)
        self.cards: dict[str, DeviceCard] = {}
        for kind in ("eeg", "ppg", "temperature"):
            card = DeviceCard(kind)
            card.scan_button.clicked.connect(lambda checked=False, k=kind: self._scan(k))
            card.connect_button.clicked.connect(lambda checked=False, k=kind: self._connect(k))
            self.cards[kind] = card
            scroll_layout.addWidget(card)
        self.connection_note = label("设备连接后先预览信号；开始阶段时自动记录，结束时自动保存。", "muted", True)
        scroll_layout.addWidget(self.connection_note)
        self.error_label = label("", "warning", True)
        self.error_label.setVisible(False)
        scroll_layout.addWidget(self.error_label)
        scroll_layout.addStretch()
        scroll.setWidget(scroll_content)
        layout.addWidget(scroll, 1)
        self.abort_stage_button = button("中止当前阶段", self._abort_stage, "danger")
        self.abort_session_button = button("结束本次实验  ·  Esc", self._abort_session, "danger")
        self.abort_stage_button.setStyleSheet("font-size: 11px; padding: 7px 6px;")
        self.abort_session_button.setStyleSheet("font-size: 11px; padding: 7px 6px;")
        abort_actions = QtWidgets.QHBoxLayout()
        abort_actions.setSpacing(6)
        abort_actions.addWidget(self.abort_stage_button)
        abort_actions.addWidget(self.abort_session_button)
        layout.addLayout(abort_actions)
        return sidebar

    def _error(self, error: Exception | str) -> None:
        QtWidgets.QMessageBox.warning(self, "操作未完成", str(error))

    def _call(self, action: Callable) -> bool:
        try:
            action()
        except Exception as exc:
            self._error(exc)
            return False
        self.refresh()
        return True

    def _run_async(self, name: str, work: Callable, done: Callable | None = None) -> None:
        if name in self._busy:
            return
        self._busy.add(name)
        generation = self._generation

        def run() -> None:
            try:
                result, error = work(), None
            except Exception as exc:
                result, error = None, str(exc)
            if not self._closed:
                try:
                    self.async_result.emit((generation, name, result, error, done))
                except RuntimeError:
                    pass

        threading.Thread(target=run, name=f"mist-ui-{name}", daemon=True).start()

    @QtCore.Slot(object)
    def _receive_async(self, payload: tuple) -> None:
        generation, name, result, error, done = payload
        if generation != self._generation or self._closed:
            return
        self._busy.discard(name)
        if error:
            self._error(error)
        elif done:
            done(result)
        self.refresh()

    def _scan(self, kind: str) -> None:
        if "restart" in self._busy:
            return
        if kind == "temperature" and not self.temperature_enabled.isChecked():
            return
        card = self.cards[kind]
        card.scan_button.setText("扫描中…" if kind == "eeg" else "刷新中…")
        controller = self.controller

        def done(devices: list[dict]) -> None:
            picker = card.device_picker
            previous = picker.currentData() or picker.currentText()
            picker.clear()
            for entry in devices:
                value = entry.get("address", "") if kind == "eeg" else entry.get("device", "")
                name = entry.get("name", "脑环") if kind == "eeg" else entry.get("description", "串口")
                picker.addItem(f"{name or '设备'}  ·  {value}", value)
            index = picker.findData(previous)
            if index >= 0:
                picker.setCurrentIndex(index)
            elif devices:
                picker.setCurrentIndex(0)
            elif kind == "eeg" and previous and not devices:
                picker.setEditText(previous)
            if not devices:
                card.details.setText("未发现设备，请检查电源、蓝牙或 USB 连接")

        scan = {"eeg": controller.scan_eeg, "ppg": controller.list_ppg,
                "temperature": controller.list_temperature}[kind]
        self._run_async(f"scan_{kind}", scan, done)

    def _connect(self, kind: str) -> None:
        if "restart" in self._busy:
            return
        if kind == "temperature" and not self.temperature_enabled.isChecked():
            return
        controller = self.controller
        connected = self._snapshot.get("devices", {}).get(kind, {}).get("connected", False)
        if connected:
            if self._snapshot.get("state") == "running" and not self._confirm("断开设备", "断开会中断当前阶段并保存已采集的数据。确定断开？"):
                return
            self._run_async(f"connect_{kind}", lambda: controller.disconnect_device(kind))
            return
        picker = self.cards[kind].device_picker
        selected = picker.currentData()
        if kind == "eeg" and (picker.currentIndex() < 0 or picker.currentText() != picker.itemText(picker.currentIndex())):
            selected = picker.currentText().strip()
        if not selected:
            self._error({"eeg": "请先扫描并选择设备，或填写脑环蓝牙地址。",
                         "ppg": "请刷新并选择指夹串口。",
                         "temperature": "请插入 GT-M601 配套蓝牙接收器，再刷新并选择其串口。"}[kind])
            return
        connect = {"eeg": controller.connect_eeg, "ppg": controller.connect_ppg,
                   "temperature": controller.connect_temperature}[kind]
        self._run_async(f"connect_{kind}", lambda: connect(str(selected)))

    def _toggle_temperature(self, checked: bool) -> None:
        if self._snapshot.get("state", "setup") != "setup" or self._snapshot.get("session_path"):
            self.refresh()
            return
        if self._call(lambda: self.controller.set_temperature_enabled(checked)) and checked:
            self._scan("temperature")

    def _toggle_simulation(self, checked: bool) -> None:
        if self._snapshot.get("session_path"):
            return
        self.timer.stop()
        self._generation += 1
        self._busy.clear()
        try:
            self.controller.close()
            self.controller = ExperimentController(simulate=checked, output_root=Path(self.output_root.text()),
                                                   temperature_enabled=self.temperature_enabled.isChecked())
        except Exception as exc:
            self._error(exc)
        self.canvas.reset()
        self._last_state = ""
        self._last_scene_id = None
        for card in self.cards.values():
            card.device_picker.clear()
        self.timer.start()
        self.refresh()
        self._scan("ppg")
        if self.temperature_enabled.isChecked():
            self._scan("temperature")
        if checked:
            self._scan("eeg")

    def _choose_folder(self) -> None:
        chosen = QtWidgets.QFileDialog.getExistingDirectory(self, "选择保存位置", self.output_root.text())
        if chosen:
            self.output_root.setText(chosen)

    def _restart_experiment(self) -> None:
        snapshot = self.controller.snapshot()
        if (self._closed or self._busy or snapshot.get("saving")
                or snapshot.get("state") not in ("completed", "aborted")):
            return
        controller = self.controller
        simulate = snapshot.get("mode") == "simulate"
        temperature_enabled = bool(snapshot.get("temperature_enabled", True))
        output_root = Path(self.output_root.text().strip())
        # Ignore queued results from the previous experiment. Device shutdown and
        # writer draining can take seconds, so leave the Qt event loop running.
        self._generation += 1

        def done(_: Any) -> None:
            try:
                closed_snapshot = controller.snapshot()
                if closed_snapshot.get("state") == "error":
                    raise RuntimeError(closed_snapshot.get("last_error") or "记录发生错误，请先检查已保存文件。")
                replacement = ExperimentController(simulate=simulate, output_root=output_root,
                                                   temperature_enabled=temperature_enabled)
            except Exception as exc:
                self._error(exc)
                return
            self.controller = replacement
            self.subject_id.clear()
            self.age.setValue(25)
            self.sex.setCurrentIndex(0)
            self.note.clear()
            self.temperature_site.clear()
            self.answer.clear()
            self._rating_touched = False
            self.rating_slider.setValue(50)
            self.rating_button.setEnabled(False)
            self.rating_label.setText("请拖动滑块，选择你此刻的压力程度")
            self.canvas.reset()
            self._last_state = ""
            self._last_scene_id = None
            self._last_stage_key = None
            self._last_plot = 0.0
            self.setup_scroll.verticalScrollBar().setValue(0)
            for card in self.cards.values():
                card.device_picker.clear()
            self.refresh()
            self._scan("ppg")
            if temperature_enabled:
                self._scan("temperature")
            if simulate:
                self._scan("eeg")

        self._run_async("restart", controller.close, done)
        self.refresh()

    def _create_session(self) -> None:
        if not self.subject_id.text().strip():
            self._error("请填写被试编号。")
            self.subject_id.setFocus()
            return
        if not self.sex.currentData():
            self._error("请选择性别。")
            self.sex.setFocus()
            return
        if not self.output_root.text().strip():
            self._error("请选择数据保存位置。")
            return
        if self.temperature_enabled.isChecked() and not self.temperature_site.text().strip():
            self._error("请填写温度测量部位，例如左侧前臂内侧。")
            self.temperature_site.setFocus()
            return
        participant = {"id": self.subject_id.text().strip(), "age": self.age.value(),
                       "sex": self.sex.currentData(), "note": self.note.text().strip()}
        if self.temperature_enabled.isChecked():
            participant["temperature_site"] = self.temperature_site.text().strip()
        durations = {key: float(spin.value()) for key, spin in self.durations.items()}
        self._call(lambda: self.controller.create_session(participant, durations, Path(self.output_root.text().strip())))

    def _start_stage(self) -> None:
        self._call(self.controller.start_stage)

    def _submit_answer(self) -> None:
        value = self.answer.text().strip()
        if value and value != "-":
            if self._call(lambda: self.controller.submit_answer(value)):
                self.answer.clear()

    def _touch_rating(self, *_: Any) -> None:
        self._rating_touched = True
        self.rating_button.setEnabled(True)
        self._rating_changed(self.rating_slider.value())

    def _rating_changed(self, value: int) -> None:
        if self._rating_touched:
            self.rating_label.setText(f"此刻的压力程度：{value} / 100")

    def _submit_rating(self) -> None:
        if self._rating_touched:
            self._call(lambda: self.controller.submit_rating(self.rating_slider.value()))

    def _confirm(self, title: str, message: str) -> bool:
        return QtWidgets.QMessageBox.question(self, title, message,
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No) == QtWidgets.QMessageBox.StandardButton.Yes

    def _abort_stage(self) -> None:
        if self._confirm("中止当前阶段", "将保存当前已采集的数据。设备就绪后可重新开始本阶段；不会覆盖这次记录。"):
            self._call(lambda: self.controller.abort_stage("operator"))

    def _abort_session(self) -> None:
        if self._snapshot.get("state") in ("setup", "completed", "aborted"):
            return
        if self._confirm("结束本次实验", "将停止本次实验并保存已经采集的数据。确定结束？"):
            self._call(self.controller.abort_session)

    def _open_folder(self) -> None:
        folder = self._snapshot.get("session_path") or self.output_root.text()
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(folder)))

    @QtCore.Slot(int, object)
    def _mark_presented(self, scene_id: int, at_ns: int) -> None:
        if self._closed:
            return
        try:
            self.controller.mark_presented(scene_id, at_ns)
        except (RuntimeError, ValueError):
            # Queued delivery can arrive after a stage is deliberately aborted.
            pass

    @QtCore.Slot()
    def refresh(self) -> None:
        if self._closed:
            return
        try:
            snapshot = self.controller.snapshot()
        except Exception as exc:
            self.error_label.setText(str(exc))
            self.error_label.setVisible(True)
            return
        self._snapshot = snapshot
        state = snapshot.get("state", "setup")
        ready = bool(snapshot.get("ready"))
        scene = dict(snapshot.get("scene") or {})
        simulation = snapshot.get("mode") == "simulate"
        restarting = "restart" in self._busy
        self.simulation_banner.setVisible(simulation)
        self.simulation.setEnabled(not snapshot.get("session_path") and not self._busy)
        temperature_enabled = bool(snapshot.get("temperature_enabled", True))
        blocker = QtCore.QSignalBlocker(self.temperature_enabled)
        self.temperature_enabled.setChecked(temperature_enabled)
        del blocker
        self.temperature_enabled.setEnabled(state == "setup" and not snapshot.get("session_path") and not self._busy)
        self.temperature_site.setVisible(temperature_enabled)
        self.temperature_site_label.setVisible(temperature_enabled)
        self.pages.setCurrentIndex(0 if state == "setup" else 1)
        self.participant_group.setEnabled(ready and state == "setup")
        self.config_group.setEnabled(state == "setup")
        self.create_button.setEnabled(ready and state == "setup")
        self.gate_message.setText("01  设备已就绪，请填写被试信息" if ready else "01  先连接右侧已启用设备")
        compact = state in ("running", "saving", "rating", "instruction")
        self.connection_note.setVisible(not compact)
        now = time.monotonic()
        for kind, card in self.cards.items():
            data = snapshot.get("devices", {}).get(kind, {})
            card.update_status(data, state == "running", self._busy, compact=compact)
            if restarting:
                card.device_picker.setEnabled(False)
                card.scan_button.setEnabled(False)
                card.connect_button.setEnabled(False)
            if f"scan_{kind}" not in self._busy:
                card.scan_button.setText("扫描脑环" if kind == "eeg" else "刷新串口")
            if now - self._last_plot >= .1:
                card.update_plot(data)
        if now - self._last_plot >= .1:
            self._last_plot = now
        error = snapshot.get("last_error", "")
        self.error_label.setText(str(error))
        self.error_label.setVisible(bool(error))
        active = state not in ("setup", "completed", "aborted", "error")
        self.abort_session_button.setVisible(active)
        self.abort_stage_button.setVisible(state == "running")
        index = int(snapshot.get("stage_index") or 0)
        name = snapshot.get("stage_name") or "实验准备"
        self.stage_label.setText(f"{min(index + 1, 6):02d} / 06   {name}" if active else {"completed": "全部阶段已完成", "aborted": "实验已结束"}.get(state, "实验准备"))
        duration = float(snapshot.get("duration") or 180)
        remaining = max(0., float(snapshot.get("remaining") or 0))
        seconds = math.ceil(remaining)
        self.clock_label.setText(f"{seconds // 60:02d}:{seconds % 60:02d}")
        self.clock_label.setVisible(state in ("instruction", "running", "interrupted", "saving"))
        self.stage_progress.setValue(round(max(0., min(1., 1 - remaining / max(duration, .001))) * 1000))
        self.stage_progress.setVisible(active)
        if state == "interrupted":
            scene = {**scene, "kind": "instruction", "text": "当前阶段已中断",
                     "detail": "已保留本次数据。请检查设备连接，信号恢复稳定后重新开始本阶段。"}
        elif state == "saving":
            scene = {**scene, "kind": "instruction", "text": "正在保存本阶段数据", "detail": "请稍候，保存完成后进入压力评分。"}
        elif state == "completed":
            scene = {**scene, "kind": "complete", "text": "实验完成，感谢参与",
                     "detail": "本实验中的同伴平均表现和目标表现用于构造任务情境，并不代表对您个人能力的真实评价。\n\n所有阶段数据已保存。"}
        elif state == "aborted":
            scene = {**scene, "kind": "complete", "text": "实验已结束", "detail": "已采集的数据已保留，可打开数据文件夹查看。"}
        elif scene.get("kind") == "rest":
            stage_key = snapshot.get("stage_key")
            scene.update(
                fixation=stage_key == "eyes_open",
                text={"eyes_open": "请注视中央，保持放松", "eyes_closed": "请闭上眼睛，保持清醒",
                      "recovery": "请安静休息，逐渐放松"}.get(stage_key, "请保持放松"),
                detail="听到提示音后睁开眼睛" if stage_key == "eyes_closed" else "请尽量减少眨眼和身体移动",
            )
        self.canvas.set_scene(scene)
        question = state == "running" and scene.get("kind") == "question"
        self.answer_panel.setVisible(question)
        if scene.get("id") != self._last_scene_id:
            self.answer.clear()
            if question:
                self.answer.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)
            self._last_scene_id = scene.get("id")
        self.rating_panel.setVisible(state == "rating")
        if state == "rating" and self._last_state != "rating":
            self._rating_touched = False
            self.rating_slider.setValue(50)
            self.rating_button.setEnabled(False)
            self.rating_label.setText("请拖动滑块，选择你此刻的压力程度")
        self.stage_action.setVisible(state in ("instruction", "interrupted"))
        self.stage_action.setText("重新开始本阶段" if state == "interrupted" else "开始本阶段")
        self.stage_action.setEnabled(ready and not snapshot.get("saving"))
        path = snapshot.get("session_path") or ""
        self.session_path_label.setText(f"本次数据：{path}" if path else "")
        self.open_folder_button.setVisible(bool(path) and state in ("completed", "aborted", "error"))
        self.restart_button.setVisible(state in ("completed", "aborted"))
        self.restart_button.setEnabled(not self._busy and not snapshot.get("saving"))
        self.restart_button.setText("正在准备新实验…" if restarting else "再次实验")
        if (self._last_stage_key == "eyes_closed" and self._last_state in ("running", "saving")
                and state in ("rating", "interrupted", "error", "aborted")):
            if hasattr(self.controller, "mark_audio_requested"):
                self.controller.mark_audio_requested()
            QtWidgets.QApplication.beep()
        self._last_stage_key = snapshot.get("stage_key")
        self._last_state = state

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if "restart" in self._busy:
            # The worker owns controller.close() until its completion signal.
            event.ignore()
            return
        if self._closed:
            event.accept()
            return
        if self._snapshot.get("state") not in ("setup", "completed", "aborted", "error"):
            if not self._confirm("退出实验程序", "将结束本次实验，保存已采集的数据并断开设备。确定退出？"):
                event.ignore()
                return
        self.timer.stop()
        self._closed = True
        try:
            self.controller.close()
        except Exception as exc:
            self._closed = False
            self.timer.start()
            self._error(exc)
            event.ignore()
            return
        event.accept()


def main(args: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MIST 脑环、指夹与温度同步实验")
    parser.add_argument("--simulate", action="store_true", help="使用明确标记的模拟数据演示实验")
    parser.add_argument("--output", type=Path, default=None, help="被试数据保存根目录")
    parser.add_argument("--self-test", type=Path, default=None, metavar="DIRECTORY",
                        help="开发者验证：仅使用模拟设备自动完成六阶段并保存验证报告")
    parser.add_argument("--self-test-hidden", action="store_true",
                        help="开发者验证时在屏幕外绘制窗口（需 --self-test）")
    parsed = parser.parse_args(args)
    if parsed.self_test_hidden and parsed.self_test is None:
        parser.error("--self-test-hidden requires --self-test")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName("MIST EEG PPG")
    app.setOrganizationName("MIST Lab")
    app.setFont(QtGui.QFont("Microsoft YaHei UI", 10))
    app.setStyle("Fusion")
    pg.setConfigOptions(antialias=False)
    if parsed.self_test is not None:
        from .selftest import run_self_test
        return run_self_test(app, parsed.self_test, hidden=parsed.self_test_hidden)
    window = MainWindow(simulate=parsed.simulate, output_root=parsed.output)
    window.show()
    return app.exec()
