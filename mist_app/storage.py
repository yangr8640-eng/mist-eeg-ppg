"""Bounded asynchronous, per-attempt recording with explicit receive-time windows."""
from __future__ import annotations

import csv
import json
import math
import os
import queue
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import __version__
from .clock import SessionClock
from .models import DeviceMessage, EEG_UV_PER_COUNT, SOURCE_VERSION, Stage


def atomic_json(path: Path, value: dict):
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2, allow_nan=False)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp, path)


def safe_id(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 64 or re.search(r'[<>:"/\\|?*\x00-\x1f]', value):
        raise ValueError("被试编号需为 1–64 个字符，不能包含路径符号或控制字符。")
    if value.endswith((".", " ")) or value in (".", ".."):
        raise ValueError("被试编号不能以句点或空格结尾。")
    return value


@dataclass
class StageWindow:
    stage: Stage
    attempt: int
    path: Path
    start_ns: int = 0
    end_ns: int = 0
    status: str = "preparing"
    reason: str = ""


class Quality:
    def __init__(self):
        self.samples = 0
        self.packets = 0
        self.invalid_packets = 0
        self.parse_errors = 0
        self.first_ns = None
        self.last_ns = None
        self.first_batch = 0
        self.intervals = 0
        self.interval_sum = 0.0
        self.interval_sq_sum = 0.0
        self.max_gap = 0.0
        self.long_gaps = 0

    def add(self, msg: DeviceMessage):
        self.packets += 1
        self.parse_errors += int(msg.meta.get("parse_errors", 0))
        if not msg.samples:
            self.invalid_packets += 1
            return
        count = len(msg.samples)
        if self.first_ns is None:
            self.first_ns = msg.received_ns
            self.first_batch = count
        if self.last_ns is not None:
            gap = max(0, (msg.received_ns - self.last_ns) / 1e9)
            self.intervals += 1
            self.interval_sum += gap
            self.interval_sq_sum += gap * gap
            self.max_gap = max(self.max_gap, gap)
            if gap >= .1:
                self.long_gaps += 1
        self.last_ns = msg.received_ns
        self.samples += count

    def report(self):
        span = (self.last_ns - self.first_ns) / 1e9 if self.last_ns is not None else 0
        mean = self.interval_sum / self.intervals if self.intervals else 0
        variance = self.interval_sq_sum / self.intervals - mean**2 if self.intervals else 0
        return {
            "samples": self.samples, "packets_or_reads": self.packets,
            "invalid_or_non_sample_reads": self.invalid_packets,
            "parse_errors": self.parse_errors,
            "observed_sample_rate_hz": (self.samples - self.first_batch) / span if span > 0 else None,
            "first_receive_ns": self.first_ns, "last_receive_ns": self.last_ns,
            "mean_receive_interval_seconds": mean,
            "receive_interval_std_seconds": math.sqrt(max(0, variance)),
            "maximum_receive_gap_seconds": self.max_gap,
            "receive_gaps_at_least_100ms": self.long_gaps,
            "exact_device_loss_count": None,
        }


class SessionRecorder:
    """A single writer owns all handles; errors are latched and observed by controller."""
    def __init__(self, root: Path, participant: dict, stages: list[Stage], simulate: bool,
                 clock: SessionClock, queue_size: int = 10000):
        self.clock = clock
        self.simulate = simulate
        root = Path(root).expanduser().resolve()
        if simulate:
            root /= "SIMULATED"
        root.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(root).free < 100 * 1024**2:
            raise OSError("保存磁盘剩余空间不足 100 MB。")
        stem = f"{safe_id(str(participant['id']))}_{datetime.now():%Y%m%d_%H%M%S_%f}"
        self.path = root / stem
        self.path.mkdir(exist_ok=False)
        self.session_id = uuid.uuid4().hex
        self.error = ""
        self._error_lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._stop = threading.Event()
        self._handles = []
        self._window: StageWindow | None = None
        self._stage_files = []
        self._sample_writers = {}
        self._raw_writers = {}
        self._quality = {}
        self.saved_counts = {"eeg": 0, "ppg": 0}
        self._attempts = []
        self._manifest = {
            "schema_version": 1, "app_version": __version__, "session_id": self.session_id,
            "mode": "simulate" if simulate else "hardware", "status": "in_progress",
            "hardware_acceptance": "not_verified", "clock": clock.metadata(),
            "participant": dict(participant), "source_paradigm": SOURCE_VERSION,
            "stages": [dict(key=s.key, name=s.name, duration_seconds=s.duration) for s in stages],
            "recording_window": "start_ns <= host_received_ns < end_ns",
            "attempts": self._attempts,
            "synchronization": {
                "device_timestamps_available": False, "device_counters_available": False,
                "hardware_alignment_error_ms": None,
                "limitation": "Receive-time alignment only; physical acquisition and screen onset delays are unknown.",
            },
        }
        atomic_json(self.path / "session.json", self._manifest)
        atomic_json(self.path / "participant.json", dict(participant))
        self._thread = threading.Thread(target=self._run, name="session-writer", daemon=True)
        self._thread.start()

    def _fail(self, message: str):
        with self._error_lock:
            if not self.error:
                self.error = message

    def _put(self, kind: str, payload=None, done=None):
        if self._stop.is_set() or self.error:
            if done:
                done.set()
            return False
        try:
            self._queue.put_nowait((kind, payload, done))
            return True
        except queue.Full:
            self._fail("写入队列已满，实验停止；请检查磁盘或后台负载。")
            if done:
                done.set()
            return False

    def prepare(self, stage: Stage, attempt: int) -> StageWindow:
        window = StageWindow(stage, attempt, self.path / f"{stage.key}_{stage.name}_attempt_{attempt:02d}")
        done = threading.Event()
        if not self._put("open", window, done):
            raise OSError(self.error or "记录器已经关闭。")
        if not done.wait(10) or self.error:
            raise OSError(self.error or "等待创建阶段文件超时。")
        return window

    def packet(self, msg: DeviceMessage, sequence: int):
        return self._put("packet", (msg, sequence))

    def event(self, kind: str, at_ns: int, **fields):
        return self._put("event", {"kind": kind, "at_ns": at_ns,
            "session_seconds": self.clock.relative(at_ns), **fields})

    def trial(self, value: dict):
        return self._put("trial", value)

    def rating(self, value: dict):
        return self._put("rating", value)

    def finish_stage(self, window: StageWindow) -> threading.Event:
        done = threading.Event()
        self._put("close", window, done)
        return done

    def finish(self, status: str) -> threading.Event:
        done = threading.Event()
        self._put("finish", status, done)
        return done

    def shutdown(self):
        self._stop.set()
        self._thread.join(timeout=12)
        if self._thread.is_alive():
            self._fail("文件写入线程未能在关闭时完成。")

    def _open_csv(self, path: Path, fields: list[str], stage=False):
        file = path.open("x", newline="", encoding="utf-8-sig")
        self._handles.append(file)
        if stage:
            self._stage_files.append(file)
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        return writer

    def _jsonline(self, file, value):
        file.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")

    def _stage_metadata(self, window):
        return {"stage": window.stage.key, "name": window.stage.name, "attempt": window.attempt,
            "status": window.status, "reason": window.reason,
            "start_ns": window.start_ns, "end_ns": window.end_ns,
            "duration_seconds": max(0, window.end_ns - window.start_ns) / 1e9,
            "clock": self.clock.metadata(),
            "mode": "simulate" if self.simulate else "hardware",
            "quality": {d: q.report() for d, q in self._quality.items()},
            "timing_limit": "Host receive timestamps; EEG sample times are estimates. No hardware sync validation.",
        }

    def _open_stage(self, window):
        if self._window is not None:
            raise RuntimeError("上阶段文件尚未关闭。")
        window.path.mkdir(exist_ok=False)
        self._window = window
        self.saved_counts = {"eeg": 0, "ppg": 0}
        self._quality = {"eeg": Quality(), "ppg": Quality()}
        common = ["session_id", "stage", "attempt", "host_sequence", "sample_in_packet",
            "received_ns", "session_seconds", "stage_seconds"]
        for device in ("eeg", "ppg"):
            extra = (["estimated_sample_ns", "estimated_stage_seconds", "timing_basis"] +
                     [f"ch{i}_raw" for i in range(1, 5)] + [f"ch{i}_uv" for i in range(1, 5)] +
                     ["electrode_raw", "electrode_off", "battery_raw"]) if device == "eeg" else ["red"]
            self._sample_writers[device] = self._open_csv(window.path / f"{window.stage.key}_{device}.csv", common + extra, True)
            raw = (window.path / f"{window.stage.key}_{device}_raw.jsonl").open("x", encoding="utf-8")
            self._raw_writers[device] = raw
            self._handles.append(raw)
            self._stage_files.append(raw)
        atomic_json(window.path / "stage.json", self._stage_metadata(window))

    def _write_packet(self, msg, sequence):
        window = self._window
        if window is None or not (window.start_ns <= msg.received_ns < window.end_ns):
            return
        self._quality[msg.device].add(msg)
        self._jsonline(self._raw_writers[msg.device], {
            "session_id": self.session_id, "stage": window.stage.key, "attempt": window.attempt,
            "received_ns": msg.received_ns, "session_seconds": self.clock.relative(msg.received_ns),
            "stage_seconds": (msg.received_ns - window.start_ns) / 1e9,
            "host_sequence": sequence, "sample_count": len(msg.samples), "raw_hex": msg.raw.hex(),
            "error": msg.error, "meta": msg.meta,
        })
        for i, sample in enumerate(msg.samples):
            row = {"session_id": self.session_id, "stage": window.stage.key, "attempt": window.attempt,
                "host_sequence": sequence, "sample_in_packet": i, "received_ns": msg.received_ns,
                "session_seconds": f"{self.clock.relative(msg.received_ns):.9f}",
                "stage_seconds": f"{(msg.received_ns-window.start_ns)/1e9:.9f}"}
            if msg.device == "eeg":
                estimated = msg.received_ns - (7 - i) * 2_000_000
                row.update(estimated_sample_ns=estimated,
                    estimated_stage_seconds=f"{(estimated-window.start_ns)/1e9:.9f}",
                    timing_basis="estimate_from_packet_receive_500Hz",
                    electrode_raw=msg.meta.get("electrode_raw", ""),
                    electrode_off=msg.meta.get("electrode_off", ""),
                    battery_raw=msg.meta.get("battery_raw", ""))
                row.update({f"ch{c+1}_raw": v for c, v in enumerate(sample)})
                row.update({f"ch{c+1}_uv": v * EEG_UV_PER_COUNT for c, v in enumerate(sample)})
            else:
                row["red"] = sample[0]
            self._sample_writers[msg.device].writerow(row)
        self.saved_counts[msg.device] += len(msg.samples)

    def _flush(self, sync=False):
        for file in self._handles:
            if not file.closed:
                file.flush()
                if sync:
                    os.fsync(file.fileno())

    def _close_stage(self, window):
        self._flush(sync=True)
        for file in self._stage_files:
            file.close()
            self._handles.remove(file)
        self._stage_files.clear()
        metadata = self._stage_metadata(window)
        atomic_json(window.path / "stage.json", metadata)
        self._attempts.append({"directory": window.path.name, **metadata})
        atomic_json(self.path / "session.json", self._manifest)
        self._window = None

    def _run(self):
        pending_done = None
        try:
            events = (self.path / "events.jsonl").open("x", encoding="utf-8")
            self._handles.append(events)
            trials = self._open_csv(self.path / "trials.csv", ["stage", "attempt", "trial", "prompt",
                "correct_answer", "answer", "correct", "outcome", "rt", "limit_seconds",
                "question_scheduled_ns", "question_presented_ns", "response_ns"])
            ratings = self._open_csv(self.path / "ratings.csv", ["stage", "attempt", "rating", "at_ns", "session_seconds"])
            last_flush = time.monotonic()
            last_disk_check = last_flush
            while not self._stop.is_set() or not self._queue.empty():
                if self.error:
                    raise OSError(self.error)
                try:
                    kind, payload, pending_done = self._queue.get(timeout=.1)
                except queue.Empty:
                    kind, payload, pending_done = "idle", None, None
                if kind == "open":
                    self._open_stage(payload)
                elif kind == "packet":
                    self._write_packet(*payload)
                elif kind == "event":
                    self._jsonline(events, payload)
                    if payload["kind"] == "stage_start" and self._window:
                        atomic_json(self._window.path / "stage.json", self._stage_metadata(self._window))
                elif kind == "trial":
                    trials.writerow(payload)
                elif kind == "rating":
                    ratings.writerow(payload)
                elif kind == "close":
                    self._close_stage(payload)
                elif kind == "finish":
                    self._manifest["status"] = payload
                    self._flush(sync=True)
                    atomic_json(self.path / "session.json", self._manifest)
                    atomic_json(self.path / "quality_report.json", {
                        "session_id": self.session_id, "status": payload,
                        "hardware_alignment_error_ms": None, "hardware_acceptance": "not_verified",
                        "attempts": self._attempts, "clock": self.clock.metadata(),
                        "note": "Intervals and rates are measured host reception metrics, not measured hardware synchronization error.",
                    })
                now = time.monotonic()
                if now - last_flush >= 1:
                    self._flush()
                    last_flush = now
                if now - last_disk_check >= 5:
                    if shutil.disk_usage(self.path).free < 50 * 1024**2:
                        raise OSError("剩余磁盘空间不足 50 MB，已停止实验。")
                    last_disk_check = now
                if pending_done:
                    pending_done.set()
                    pending_done = None
        except Exception as exc:
            self._fail(f"数据写入失败：{exc}")
        finally:
            if self.error:
                try:
                    if self._window:
                        self._window.status = "incomplete"
                        self._window.reason = self.error
                        failed_at = time.perf_counter_ns()
                        if not self._window.start_ns:
                            self._window.start_ns = failed_at
                        self._window.end_ns = max(self._window.start_ns,
                            min(self._window.end_ns or failed_at, failed_at))
                        atomic_json(self._window.path / "stage.json", self._stage_metadata(self._window))
                    self._manifest.update(status="error", error=self.error)
                    atomic_json(self.path / "session.json", self._manifest)
                except OSError:
                    pass
            for file in self._handles:
                try:
                    file.close()
                except OSError:
                    pass
            if pending_done:
                pending_done.set()
            while True:
                try:
                    _, _, done = self._queue.get_nowait()
                    if done:
                        done.set()
                except queue.Empty:
                    break
