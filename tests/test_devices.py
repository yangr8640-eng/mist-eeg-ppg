from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from mist_app.devices import (
    EEGDevice, PPGDevice, PPGParser, SimulatedDevice, TemperatureDevice,
    TemperatureParser, list_temperature, parse_eeg_packet,
)
from mist_app.devices.protocol import EEG_NOTIFY_UUID, EEG_SERVICE_UUID, EEG_WRITE_UUID
from mist_app.models import EEG_UV_PER_COUNT


def eeg_packet(rows, flags=0, battery=197):
    return bytes([255, 253, flags, battery]) + b"".join(
        value.to_bytes(3, "big", signed=True) for row in rows for value in row)


def test_eeg_signed_extremes_channel_order_and_scale():
    rows = [(0, 1, -1, 8388607), (-8388608, 256, -256, 65536)]
    rows.extend([(i, i + 100, i + 200, i + 300) for i in range(6)])
    message = parse_eeg_packet(eeg_packet(rows, flags=1), 123456789)
    assert message.samples == tuple(rows)
    assert message.received_ns == 123456789
    assert message.samples[1][0] * EEG_UV_PER_COUNT == -25000
    assert message.meta["lead_off"] is True
    assert message.meta["electrode_raw"] == 1
    assert message.meta["electrode_off"] is True
    assert message.meta["battery_raw"] == 197
    assert message.meta["battery_calibrated"] is False
    assert not message.error


@pytest.mark.parametrize("raw", [b"", b"\xff\xfd" + bytes(97), b"\xff\xfd" + bytes(99), bytes(100)])
def test_eeg_invalid_packets_are_retained_and_never_decoded(raw):
    message = parse_eeg_packet(raw, 9)
    assert message.kind == "packet"
    assert message.raw == raw
    assert message.samples == ()
    assert message.error
    assert message.meta["parse_errors"] == 1


def test_ppg_partial_lines_bursts_crlf_and_receive_time():
    parser = PPGParser()
    first = parser.feed(b"12", 100)
    assert first.samples == ()
    assert first.raw == b"12"
    assert not first.error
    second = parser.feed(b"3\r\n-4\n +5 \n6", 200)
    assert second.samples == ((123,), (-4,), (5,))
    assert second.received_ns == 200
    assert second.meta["buffered_bytes"] == 1
    assert parser.feed(b"7\n", 300).samples == ((67,),)


def test_ppg_startup_text_invalid_bytes_and_long_range():
    parser = PPGParser()
    message = parser.feed(b"Intilaziting AFE44xx..\r\n\xff4\n1.2\n2147483648\n-2147483648\n17\n", 5)
    assert message.samples == ((-2147483648,), (17,))
    assert message.meta["parse_errors"] == 4
    assert message.raw.startswith(b"Intilaziting")


def test_ppg_oversized_line_is_discarded_until_newline_without_prefix_sample():
    parser = PPGParser(max_line_bytes=16)
    message = parser.feed(b"9" * 20, 0)
    assert message.samples == ()
    assert message.meta["parse_errors"] == 1
    assert message.meta["buffered_bytes"] == 0
    assert message.meta["discarding_oversized_line"] is True
    assert parser.feed(b"123\n42\n", 1).samples == ((42,),)


@pytest.mark.parametrize("device,expected_count", [("eeg", 8), ("ppg", 1), ("temperature", 1)])
def test_simulation_worker_lifecycle_and_real_protocol(device, expected_count):
    messages = []
    arrived = threading.Event()

    def receive(message):
        messages.append(message)
        if message.samples:
            arrived.set()

    adapter = SimulatedDevice(device, receive)
    before = time.perf_counter_ns()
    adapter.connect("simulation")
    assert arrived.wait(2)
    assert adapter.connected
    adapter.disconnect()
    assert not adapter.connected
    assert [m.kind for m in messages].count("connected") == 1
    assert [m.kind for m in messages].count("disconnected") == 1
    assert all(m.meta["simulated"] for m in messages)
    packet = next(m for m in messages if m.kind == "packet")
    assert len(packet.samples) == expected_count
    assert packet.received_ns >= before
    assert packet.raw
    assert not packet.error
    # A clean reconnect must create a new worker and emit another lifecycle.
    arrived.clear()
    adapter.connect("simulation")
    assert arrived.wait(2)
    adapter.disconnect()
    assert [m.kind for m in messages].count("connected") == 2
    assert [m.kind for m in messages].count("disconnected") == 2


def test_simulation_rejects_concurrent_connect():
    adapter = SimulatedDevice("eeg", lambda _message: None)
    adapter.connect("simulation")
    try:
        with pytest.raises(RuntimeError):
            adapter.connect("simulation")
    finally:
        adapter.disconnect()


@pytest.mark.parametrize("device_type,baud_rate,chunks_data,sample", [
    (PPGDevice, 57600, [b"boot\r\n1", b"23\n"], 123),
    (TemperatureDevice, 115200, [b"boot\r\nA+3", b"6.5B\r\n"], 36.5),
])
def test_serial_reader_preserves_chunks_and_reports_unexpected_loss(monkeypatch, device_type, baud_rate,
                                                                  chunks_data, sample):
    import serial

    messages = []
    ended = threading.Event()
    chunks = iter(chunks_data)

    class FakePort:
        closed = False
        in_waiting = 32

        def read(self, _size):
            try:
                return next(chunks)
            except StopIteration:
                raise serial.SerialException("USB unplugged")

        def close(self):
            self.closed = True

    port = FakePort()

    def open_serial(identifier, **kwargs):
        assert identifier == "COM_TEST"
        assert kwargs["baudrate"] == baud_rate
        assert kwargs["bytesize"] == serial.EIGHTBITS
        assert kwargs["parity"] == serial.PARITY_NONE
        assert kwargs["stopbits"] == serial.STOPBITS_ONE
        assert kwargs["timeout"] == 0.1
        return port

    def receive(message):
        messages.append(message)
        if message.kind == "disconnected":
            ended.set()

    monkeypatch.setattr(serial, "Serial", open_serial)
    adapter = device_type(receive)
    adapter.connect("COM_TEST")
    assert ended.wait(2)
    adapter.disconnect()
    assert port.closed
    assert not adapter.connected
    assert [m.kind for m in messages] == ["connected", "packet", "packet", "error", "disconnected"]
    assert messages[1].raw == chunks_data[0]
    assert messages[1].samples == ()
    assert messages[2].samples == ((sample,),)
    assert messages[1].received_ns <= messages[2].received_ns


@pytest.mark.parametrize("fail_callback", [False, True])
def test_ble_subscription_commands_and_packet_reception(monkeypatch, fail_callback):
    import bleak

    messages = []
    ready = threading.Event()
    calls = []
    packet = eeg_packet([(1, -2, 3, -4)] * 8)

    class FakeClient:
        def __init__(self, identifier, **kwargs):
            assert identifier == "AA:BB:CC:DD:EE:FF"
            self.is_connected = False
            self.services = SimpleNamespace(get_service=self.get_service)
            self.disconnected_callback = kwargs["disconnected_callback"]
            self.callback = None

        def get_service(self, uuid):
            assert uuid == EEG_SERVICE_UUID
            return SimpleNamespace(get_characteristic=lambda _uuid: object())

        async def connect(self):
            self.is_connected = True
            calls.append("connect")

        async def start_notify(self, uuid, callback):
            assert uuid == EEG_NOTIFY_UUID
            self.callback = callback
            calls.append("subscribe")

        async def write_gatt_char(self, uuid, value, response):
            assert uuid == EEG_WRITE_UUID
            assert response is False
            calls.append(value)
            if value == b"\x31":
                self.callback(None, bytearray(packet))

        async def stop_notify(self, uuid):
            calls.append("unsubscribe")

        async def disconnect(self):
            calls.append("disconnect")
            self.is_connected = False
            self.disconnected_callback(self)

    def receive(message):
        messages.append(message)
        if fail_callback and message.kind == "packet":
            raise OSError("recording sink failed")
        if message.kind == ("disconnected" if fail_callback else "connected"):
            ready.set()

    monkeypatch.setattr(bleak, "BleakClient", FakeClient)
    adapter = EEGDevice(receive)
    adapter.connect("AA:BB:CC:DD:EE:FF")
    assert ready.wait(2)
    adapter.disconnect()
    assert not adapter.connected
    assert calls == ["connect", "subscribe", b"\x31", b"\x32", "unsubscribe", "disconnect"]
    assert [m.kind for m in messages].count("connected") == (0 if fail_callback else 1)
    assert [m.kind for m in messages].count("disconnected") == 1
    assert next(m for m in messages if m.samples).raw == packet
    if fail_callback:
        assert "recording sink failed" in next(m for m in messages if m.kind == "error").error


def test_ble_rejects_wrong_device_and_closes_connection(monkeypatch):
    import bleak

    messages = []
    ended = threading.Event()
    closed = threading.Event()

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            self.is_connected = False
            self.services = SimpleNamespace(get_service=lambda _uuid: None)

        async def connect(self):
            self.is_connected = True

        async def disconnect(self):
            self.is_connected = False
            closed.set()

    def receive(message):
        messages.append(message)
        if message.kind == "disconnected":
            ended.set()

    monkeypatch.setattr(bleak, "BleakClient", FakeClient)
    adapter = EEGDevice(receive)
    adapter.connect("wrong device")
    assert ended.wait(2)
    assert closed.is_set()
    assert [m.kind for m in messages] == ["error", "disconnected"]
    assert "FE40" in messages[0].error
    adapter.disconnect()


@pytest.mark.parametrize("device_type", [PPGDevice, TemperatureDevice])
def test_serial_with_no_data_remains_cancellable_and_does_not_invent_samples(monkeypatch, device_type):
    import serial

    messages = []
    ready = threading.Event()
    cancelled = threading.Event()

    class FakePort:
        in_waiting = 0
        closed = False

        def read(self, size):
            assert size == 1
            cancelled.wait(0.1)
            return b""

        def cancel_read(self):
            cancelled.set()

        def close(self):
            self.closed = True

    port = FakePort()
    monkeypatch.setattr(serial, "Serial", lambda *_args, **_kwargs: port)

    def receive(message):
        messages.append(message)
        if message.kind == "connected":
            ready.set()

    adapter = device_type(receive)
    adapter.connect("COM_TEST")
    assert ready.wait(2)
    started = time.perf_counter()
    adapter.disconnect()
    assert time.perf_counter() - started < 1
    assert cancelled.is_set()
    assert port.closed
    assert [m.kind for m in messages] == ["connected", "disconnected"]


@pytest.mark.parametrize("split", range(1, 9))
def test_temperature_partial_frame_at_every_boundary_and_coalesced_frames(split):
    raw = b"A+36.5B\r\n"
    parser = TemperatureParser()
    first = parser.feed(raw[:split], 100)
    assert first.samples == ()
    assert first.raw == raw[:split]
    assert not first.error
    second = parser.feed(raw[split:] + b"A-05.2B\r\nA+00.0B\r\nA+3", 200)
    assert second.device == "temperature"
    assert second.samples == ((36.5,), (-5.2,), (0.0,))
    assert all(isinstance(row[0], float) for row in second.samples)
    assert second.received_ns == 200
    assert second.meta["buffered_bytes"] == 3
    assert second.meta["resolution_celsius"] == 0.1
    assert second.meta["baud_rate"] == 115200
    assert second.meta["protocol"] == "gt-m601-ascii"
    assert "nominal_rate_hz" not in second.meta
    assert not second.error
    assert parser.feed(b"7.1B\r\n", 300).samples == ((37.1,),)


@pytest.mark.parametrize("invalid", [
    b"36.5\r\n", b"A36.5B\r\n", b"A+3.5B\r\n", b"A+036.5B\r\n",
    b"A+36.50B\r\n", b"A+36.5B\n", b" A+36.5B\r\n", b"A+36.5B \r\n",
    b"A+36.5C\r\n", b"A+NaNB\r\n", b"\xffA+36.5B\r\n", b"A-70.1B\r\n",
    b"A-99.9B\r\n", b"A+36.5BA+37.5B\r\n", b"\r\n",
])
def test_temperature_invalid_frames_retained_and_resynchronized(invalid):
    raw = invalid + b"A+36.5B\r\n"
    message = TemperatureParser().feed(raw, 12)
    assert message.raw == raw
    assert message.samples == ((36.5,),)
    assert message.meta["parse_errors"] == 1
    assert len(message.meta["parse_error_details"]) == 1
    assert message.error


def test_temperature_documented_format_limits():
    message = TemperatureParser().feed(b"A-70.0B\r\nA+99.9B\r\nA+100.0B\r\n", 0)
    assert message.samples == ((-70.0,), (99.9,))
    assert message.meta["parse_errors"] == 1


def test_temperature_oversized_unterminated_data_is_bounded_and_never_decodes_suffix():
    parser = TemperatureParser(max_line_bytes=8)
    message = parser.feed(b"x" * 10000, 0)
    assert message.samples == ()
    assert message.meta["parse_errors"] == 1
    assert message.meta["buffered_bytes"] == 0
    assert message.meta["discarding_oversized_line"] is True
    recovered = parser.feed(b"A+36.5B\r\nA+37.0B\r\n", 1)
    assert recovered.samples == ((37.0,),)
    assert not recovered.error


def test_temperature_serial_port_candidates_are_not_assumed_to_be_sensors(monkeypatch):
    from serial.tools import list_ports

    ports = [
        SimpleNamespace(device="COM8", description="Bluetooth receiver", vid=None),
        SimpleNamespace(device="COM5", description="USB-SERIAL CH340", vid=0x1A86),
        SimpleNamespace(device="COM2", description="", vid=0x1234),
    ]
    monkeypatch.setattr(list_ports, "comports", lambda: ports)
    assert list_temperature() == [
        {"device": "COM2", "description": "COM2"},
        {"device": "COM5", "description": "USB-SERIAL CH340"},
        {"device": "COM8", "description": "Bluetooth receiver"},
    ]


def test_temperature_reconnect_does_not_reuse_partial_frame(monkeypatch):
    import serial

    messages = []
    ended = threading.Event()
    ports = []

    class FakePort:
        in_waiting = 32
        closed = False

        def __init__(self, chunks):
            self.chunks = iter(chunks)

        def read(self, _size):
            try:
                return next(self.chunks)
            except StopIteration:
                raise serial.SerialException("USB unplugged")

        def close(self):
            self.closed = True

    streams = iter([[b"A+3"], [b"6.5B\r\nA+37.0B\r\n"]])

    def open_serial(*_args, **_kwargs):
        port = FakePort(next(streams))
        ports.append(port)
        return port

    def receive(message):
        messages.append(message)
        if message.kind == "disconnected":
            ended.set()

    monkeypatch.setattr(serial, "Serial", open_serial)
    adapter = TemperatureDevice(receive)
    for _ in range(2):
        ended.clear()
        adapter.connect("COM_TEST")
        assert ended.wait(2)
        adapter.disconnect()
    packets = [message for message in messages if message.kind == "packet"]
    assert packets[0].samples == ()
    assert packets[1].samples == ((37.0,),)
    assert packets[1].meta["parse_errors"] == 1
    assert all(port.closed for port in ports)
