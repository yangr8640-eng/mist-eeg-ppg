from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from mist_app.devices import EEGDevice, PPGDevice, PPGParser, SimulatedDevice, parse_eeg_packet
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


@pytest.mark.parametrize("device,expected_count", [("eeg", 8), ("ppg", 1)])
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


def test_serial_reader_preserves_chunks_and_reports_unexpected_loss(monkeypatch):
    import serial

    messages = []
    ended = threading.Event()
    chunks = iter([b"boot\r\n1", b"23\n"])

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
        assert kwargs["baudrate"] == 57600
        assert kwargs["timeout"] == 0.1
        return port

    def receive(message):
        messages.append(message)
        if message.kind == "disconnected":
            ended.set()

    monkeypatch.setattr(serial, "Serial", open_serial)
    adapter = PPGDevice(receive)
    adapter.connect("COM_TEST")
    assert ended.wait(2)
    adapter.disconnect()
    assert port.closed
    assert not adapter.connected
    assert [m.kind for m in messages] == ["connected", "packet", "packet", "error", "disconnected"]
    assert messages[1].raw == b"boot\r\n1"
    assert messages[1].samples == ()
    assert messages[2].samples == ((123,),)
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


def test_serial_with_no_data_remains_cancellable_and_does_not_invent_samples(monkeypatch):
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

    adapter = PPGDevice(receive)
    adapter.connect("COM_TEST")
    assert ready.wait(2)
    started = time.perf_counter()
    adapter.disconnect()
    assert time.perf_counter() - started < 1
    assert cancelled.is_set()
    assert port.closed
    assert [m.kind for m in messages] == ["connected", "disconnected"]
