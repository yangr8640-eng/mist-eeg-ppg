"""Device wire formats. All timestamps describe reception on this host."""
from __future__ import annotations

import re

from mist_app.models import DeviceMessage

EEG_SERVICE_UUID = "0000fe40-8e22-4541-9d4c-21edae82ed19"
EEG_WRITE_UUID = "0000fe41-8e22-4541-9d4c-21edae82ed19"
EEG_NOTIFY_UUID = "0000fe42-8e22-4541-9d4c-21edae82ed19"
INTEGER_LINE = re.compile(rb"[+-]?[0-9]+\Z")
TEMPERATURE_BAUD_RATE = 115200
TEMPERATURE_PROTOCOL = "gt-m601-ascii"
# GT-M601 module manual section 3.4. The newline is consumed by the framer;
# unlike the PPG protocol, both the sign and CRLF are mandatory here.
TEMPERATURE_LINE = re.compile(rb"A([+-][0-9]{2}\.[0-9])B\r\Z")


def parse_eeg_packet(raw: bytes, received_ns: int) -> DeviceMessage:
    """Decode one notification without joining or guessing malformed packets."""
    raw = bytes(raw)
    if len(raw) != 100 or raw[:2] != b"\xff\xfd":
        error = f"EEG 数据包异常：长度 {len(raw)}（应为 100），包头 {raw[:2].hex()}（应为 fffd）"
        return DeviceMessage("eeg", "packet", received_ns, raw, error=error,
                             meta={"parse_errors": 1, "parse_error_details": [error]})
    samples = tuple(
        tuple(int.from_bytes(raw[4 + index * 12 + channel * 3:
                                     7 + index * 12 + channel * 3],
                             "big", signed=True) for channel in range(4))
        for index in range(8)
    )
    return DeviceMessage("eeg", "packet", received_ns, raw, samples, meta={
        "electrode_raw": raw[2], "electrode_off": raw[2] != 0,
        "lead_off_raw": raw[2], "lead_off": raw[2] != 0,
        "battery_raw": raw[3], "battery_calibrated": False,
        "nominal_rate_hz": 500, "parse_errors": 0,
    })


class PPGParser:
    """Incremental newline framing with bounded memory and resynchronization.

    A sample is timestamped when its terminating newline arrives. A chunk's
    raw bytes are retained even when it contains no complete or valid lines.
    Arduino's RED_data is a signed 32-bit long; startup text is diagnostic data.
    """

    def __init__(self, max_line_bytes: int = 256):
        if max_line_bytes < 16:
            raise ValueError("max_line_bytes must be at least 16")
        self.max_line_bytes = max_line_bytes
        self._buffer = bytearray()
        self._discarding = False

    def feed(self, raw: bytes, received_ns: int) -> DeviceMessage:
        samples: list[tuple[int, ...]] = []
        errors: list[str] = []
        raw = bytes(raw)
        for byte in raw:
            if self._discarding:
                if byte == 10:
                    self._discarding = False
                continue
            if byte != 10:
                self._buffer.append(byte)
                if len(self._buffer) > self.max_line_bytes:
                    self._buffer.clear()
                    self._discarding = True
                    errors.append("PPG 行超过长度上限，丢弃至下一个换行符")
                continue
            line = bytes(self._buffer).strip(b" \t\r")
            self._buffer.clear()
            if not INTEGER_LINE.fullmatch(line):
                errors.append(f"PPG 非整数行：{line[:64]!r}")
                continue
            value = int(line)
            if not -(2 ** 31) <= value < 2 ** 31:
                errors.append("PPG 整数超出 Arduino signed long 范围")
                continue
            samples.append((value,))
        return DeviceMessage("ppg", "packet", received_ns, raw, tuple(samples),
                             error="；".join(errors), meta={
                                 "parse_errors": len(errors),
                                 "parse_error_details": errors,
                                 "buffered_bytes": len(self._buffer),
                                 "discarding_oversized_line": self._discarding,
                                 "baud_rate": 57600,
                             })


class TemperatureParser:
    """Incrementally decode GT-M601's documented ``A+XX.XB\r\n`` frames.

    Each returned message preserves the original chunk, including malformed
    data and partial frames. Samples use host receipt time at the terminating
    newline; the module supplies neither device timestamps nor sequence IDs.
    The module's output resolution is 0.1 C, not the chip's internal 0.004 C.
    """

    def __init__(self, max_line_bytes: int = 64):
        if max_line_bytes < 8:
            raise ValueError("max_line_bytes must be at least 8")
        self.max_line_bytes = max_line_bytes
        self._buffer = bytearray()
        self._discarding = False

    def feed(self, raw: bytes, received_ns: int) -> DeviceMessage:
        raw = bytes(raw)
        samples: list[tuple[float, ...]] = []
        errors: list[str] = []
        for byte in raw:
            if self._discarding:
                if byte == 10:
                    self._discarding = False
                continue
            if byte != 10:
                self._buffer.append(byte)
                if len(self._buffer) > self.max_line_bytes:
                    self._buffer.clear()
                    self._discarding = True
                    errors.append("温度数据行超过长度上限，丢弃至下一个换行符")
                continue
            line = bytes(self._buffer)
            self._buffer.clear()
            match = TEMPERATURE_LINE.fullmatch(line)
            if match is None:
                errors.append(f"温度数据格式异常（应为 A+XX.XB\\r\\n）：{line[:64]!r}")
                continue
            value = float(match[1])
            if not -70.0 <= value <= 150.0:
                errors.append(f"温度值超出芯片工作范围 -70～150 °C：{value:.1f}")
                continue
            samples.append((value,))
        return DeviceMessage("temperature", "packet", received_ns, raw, tuple(samples),
                             error="；".join(errors), meta={
                                 "protocol": TEMPERATURE_PROTOCOL,
                                 "baud_rate": TEMPERATURE_BAUD_RATE,
                                 "unit": "celsius",
                                 "resolution_celsius": 0.1,
                                 "parse_errors": len(errors),
                                 "parse_error_details": errors,
                                 "buffered_bytes": len(self._buffer),
                                 "discarding_oversized_line": self._discarding,
                             })
