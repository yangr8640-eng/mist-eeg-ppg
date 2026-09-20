"""Public acquisition adapters. connect() starts a background reader."""
from .adapters import EEGDevice, PPGDevice, SimulatedDevice, TemperatureDevice, list_ppg, list_temperature, scan_eeg
from .protocol import PPGParser, TemperatureParser, parse_eeg_packet

__all__ = ["EEGDevice", "PPGDevice", "TemperatureDevice", "SimulatedDevice", "list_ppg", "list_temperature", "scan_eeg",
           "PPGParser", "TemperatureParser", "parse_eeg_packet"]
