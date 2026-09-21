"""Threaded BLE, serial and simulation adapters; deliberately independent of Qt."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import logging
import math
import random
import sys
import threading
import time
from typing import Any

from mist_app.models import DeviceCallback, DeviceMessage
from .protocol import (
    EEG_NOTIFY_UUID, EEG_SERVICE_UUID, EEG_WRITE_UUID, PPGParser,
    TEMPERATURE_BAUD_RATE, TEMPERATURE_PROTOCOL, TemperatureParser, parse_eeg_packet,
)

log = logging.getLogger(__name__)


def _prepare_ble_worker() -> None:
    # These are dedicated workers without a Windows GUI message loop. Never use
    # allow_sta(): it suppresses the check but cannot supply the required pump.
    # Fresh workers are normally COM-uninitialized; this also undoes accidental
    # STA initialization by a dependency before WinRT establishes its MTA.
    if sys.platform == "win32":
        from bleak.backends.winrt.util import uninitialize_sta
        uninitialize_sta()


def scan_eeg(timeout: float = 5.0) -> list[dict[str, str]]:
    """Blocking scan intended for a UI worker thread; do not filter by name.

    Some firmware does not advertise the service UUID. A discovered device is
    therefore only a candidate; connection verifies the acquisition services.
    """
    from bleak import BleakScanner

    _prepare_ble_worker()

    async def scan() -> list[dict[str, str]]:
        discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
        found = []
        for device, advertisement in discovered.values():
            name = advertisement.local_name or device.name or "未命名蓝牙设备"
            found.append({"name": name, "address": device.address})
        return sorted(found, key=lambda item: (item["name"].casefold(), item["address"]))

    return asyncio.run(scan())


def _list_serial_ports() -> list[dict[str, str]]:
    from serial.tools import list_ports
    ports = list(list_ports.comports())
    ports.sort(key=lambda port: (port.vid is None, port.device))
    return [{"device": port.device, "description": port.description or port.device}
            for port in ports]


def list_ppg() -> list[dict[str, str]]:
    return _list_serial_ports()


def list_temperature() -> list[dict[str, str]]:
    """List candidates for both USB serial and the supplied Bluetooth receiver.

    CH340 is not unique to GT-M601; never identify the sensor by chipset alone.
    Its documented frames are verified only after opening the selected port.
    """
    return _list_serial_ports()


class _ThreadedDevice:
    device: str

    def __init__(self, callback: DeviceCallback):
        self.callback = callback
        self.identifier = ""
        self.connected = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lifecycle_lock = threading.Lock()

    def connect(self, address: str) -> None:
        """Return immediately; successful connection is reported by callback."""
        if not address.strip():
            raise ValueError("请选择设备地址")
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("设备正在连接或断开，请等待状态更新")
            self.identifier = address.strip()
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name=f"{self.device}-reader", daemon=True)
            self._thread.start()

    def disconnect(self) -> None:
        """Signal cancellation and bound caller waiting, including failed BLE IO."""
        self._stop.set()
        self._request_stop()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.75)

    def wait_disconnected(self, timeout: float = 5.0) -> bool:
        """Wait for transport cleanup before a new controller reuses the device."""
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        return thread is None or not thread.is_alive()

    def _request_stop(self) -> None:
        pass

    def _emit(self, message: DeviceMessage) -> None:
        self.callback(message)

    def _status(self, kind: str, error: str = "", **meta: Any) -> None:
        self._emit(DeviceMessage(self.device, kind, time.perf_counter_ns(), error=error,
                                 meta={"identifier": self.identifier, **meta}))

    def _run(self) -> None:
        failure = ""
        try:
            self._acquire()
        except Exception as exc:
            if not self._stop.is_set():
                failure = f"{type(exc).__name__}: {exc}"
                try:
                    self._status("error", failure)
                except Exception:
                    log.exception("Device callback failed while reporting acquisition error")
        finally:
            self.connected = False
            try:
                self._status("disconnected", failure)
            except Exception:
                log.exception("Device callback failed while reporting disconnection")

    def _acquire(self) -> None:
        raise NotImplementedError


class EEGDevice(_ThreadedDevice):
    device = "eeg"

    def __init__(self, callback: DeviceCallback):
        super().__init__(callback)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None

    def _request_stop(self) -> None:
        loop, task = self._loop, self._task
        if loop is not None and task is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # The worker has already exited.

    def _acquire(self) -> None:
        _prepare_ble_worker()
        try:
            asyncio.run(self._stream())
        finally:
            self._task = None
            self._loop = None

    async def _stream(self) -> None:
        from bleak import BleakClient

        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        disconnected = asyncio.Event()
        client = BleakClient(self.identifier, timeout=12.0,
                             disconnected_callback=lambda _: disconnected.set())
        subscribed = False
        started = False
        callback_failure: Exception | None = None

        def receive(_characteristic: Any, payload: bytearray) -> None:
            nonlocal callback_failure
            received_ns = time.perf_counter_ns()
            if not self._stop.is_set():
                try:
                    self._emit(parse_eeg_packet(bytes(payload), received_ns))
                except Exception as exc:
                    callback_failure = exc
                    disconnected.set()

        try:
            if self._stop.is_set():
                return
            await asyncio.wait_for(client.connect(), timeout=15.0)
            service = client.services.get_service(EEG_SERVICE_UUID)
            if service is None:
                raise RuntimeError("所选蓝牙设备没有 FE40 脑电服务，请选择正确脑环")
            if service.get_characteristic(EEG_WRITE_UUID) is None or service.get_characteristic(EEG_NOTIFY_UUID) is None:
                raise RuntimeError("脑环缺少 FE41 写入或 FE42 通知特征")
            await asyncio.wait_for(client.start_notify(EEG_NOTIFY_UUID, receive), timeout=5.0)
            subscribed = True
            started = True
            await asyncio.wait_for(client.write_gatt_char(EEG_WRITE_UUID, b"\x31", response=False), timeout=3.0)
            if callback_failure is not None:
                raise RuntimeError(f"脑电数据接收回调失败：{callback_failure}") from callback_failure
            if self._stop.is_set():
                return
            self.connected = True
            self._status("connected", nominal_rate_hz=500)
            while not self._stop.is_set() and not disconnected.is_set():
                try:
                    await asyncio.wait_for(disconnected.wait(), timeout=0.1)
                except asyncio.TimeoutError:
                    pass
            if callback_failure is not None:
                raise RuntimeError(f"脑电数据接收回调失败：{callback_failure}") from callback_failure
            if disconnected.is_set() and not self._stop.is_set():
                raise ConnectionError("脑环蓝牙连接已断开")
        except asyncio.CancelledError:
            if callback_failure is not None:
                raise RuntimeError(f"脑电数据接收回调失败：{callback_failure}") from callback_failure
            if not self._stop.is_set():
                raise
        finally:
            self._task = None  # A second disconnect must not interrupt cleanup.
            if client.is_connected:
                if started:
                    try:
                        await asyncio.wait_for(client.write_gatt_char(EEG_WRITE_UUID, b"\x32", response=False), timeout=0.7)
                    except Exception:
                        log.debug("BLE stop command failed during cleanup", exc_info=True)
                if subscribed:
                    try:
                        await asyncio.wait_for(client.stop_notify(EEG_NOTIFY_UUID), timeout=0.7)
                    except Exception:
                        log.debug("BLE notification cleanup failed", exc_info=True)
            try:
                await asyncio.wait_for(client.disconnect(), timeout=2.0)
            except Exception:
                log.debug("BLE disconnection cleanup failed", exc_info=True)


class _SerialDevice(_ThreadedDevice):
    baud_rate: int
    parser_type: type[PPGParser] | type[TemperatureParser]
    connection_meta: dict[str, Any] = {}

    def __init__(self, callback: DeviceCallback):
        super().__init__(callback)
        self._serial: Any = None

    def _request_stop(self) -> None:
        port = self._serial
        if port is not None:
            try:
                port.cancel_read()
            except (AttributeError, OSError):
                pass

    def _acquire(self) -> None:
        import serial

        # A reconnect starts a new parser: an unfinished frame from an earlier
        # connection must never be joined to data from the new connection.
        parser = self.parser_type()
        port = serial.Serial(self.identifier, baudrate=self.baud_rate,
                             bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                             stopbits=serial.STOPBITS_ONE, timeout=0.1, write_timeout=0.5)
        self._serial = port
        try:
            if self._stop.is_set():
                return
            self.connected = True
            self._status("connected", baud_rate=self.baud_rate, **self.connection_meta)
            while not self._stop.is_set():
                raw = port.read(min(max(port.in_waiting, 1), 4096))
                received_ns = time.perf_counter_ns()
                if raw and not self._stop.is_set():
                    self._emit(parser.feed(raw, received_ns))
        finally:
            self._serial = None
            port.close()


class PPGDevice(_SerialDevice):
    # Arduino may reset when the port opens. Startup lines remain diagnostics;
    # the readiness gate waits for continuous valid samples afterwards.
    device = "ppg"
    baud_rate = 57600
    parser_type = PPGParser


class TemperatureDevice(_SerialDevice):
    """GT-M601 USB serial or its factory-paired Bluetooth USB receiver.

    The manufacturer documents a COM port for both kits, with automatic
    streaming at 115200 8N1. No BLE UUID or start/stop commands are specified.
    """

    device = "temperature"
    baud_rate = TEMPERATURE_BAUD_RATE
    parser_type = TemperatureParser
    connection_meta = {"protocol": TEMPERATURE_PROTOCOL, "unit": "celsius",
                       "resolution_celsius": 0.1, "transport": "serial"}


class SimulatedDevice(_ThreadedDevice):
    """Protocol-realistic synthetic data, explicitly tagged at every callback."""

    def __init__(self, device: str, callback: DeviceCallback):
        if device not in ("eeg", "ppg", "temperature"):
            raise ValueError("device must be eeg, ppg or temperature")
        self.device = device
        super().__init__(callback)

    def _emit(self, message: DeviceMessage) -> None:
        super()._emit(replace(message, meta={**message.meta, "simulated": True}))

    def _acquire(self) -> None:
        self.connected = True
        self._status("connected", nominal_rate_hz={"eeg": 500, "ppg": 125, "temperature": 1}[self.device])
        parser = TemperatureParser() if self.device == "temperature" else PPGParser()
        rng = random.Random({"eeg": 101, "ppg": 102, "temperature": 103}[self.device])
        index = 0
        interval = {"eeg": 0.016, "ppg": 0.008, "temperature": 1.0}[self.device]
        deadline = time.perf_counter()
        while not self._stop.is_set():
            if self.device == "eeg":
                raw = bytearray(b"\xff\xfd\x00\xb4")
                for i in range(8):
                    for channel in range(4):
                        phase = 2 * math.pi * (8 + channel) * (index + i) / 500
                        count = round(8000 * math.sin(phase) + rng.gauss(0, 500))
                        raw.extend(count.to_bytes(3, "big", signed=True))
                index += 8
                self._emit(parse_eeg_packet(bytes(raw), time.perf_counter_ns()))
            elif self.device == "ppg":
                value = round(100000 + 8000 * math.sin(2 * math.pi * 1.2 * index / 125) + rng.gauss(0, 100))
                index += 1
                self._emit(parser.feed(f"{value}\r\n".encode("ascii"), time.perf_counter_ns()))
            else:
                value = 33.0 + 0.3 * math.sin(2 * math.pi * index / 120) + rng.gauss(0, 0.03)
                index += 1
                self._emit(parser.feed(f"A{value:+05.1f}B\r\n".encode("ascii"), time.perf_counter_ns()))
            deadline += interval
            now = time.perf_counter()
            # Avoid unbounded catch-up bursts after suspension or debugging.
            if deadline < now - interval:
                deadline = now + interval
            self._stop.wait(max(0, deadline - now))
