"""Public acquisition adapters. connect() starts a background reader."""
from .adapters import EEGDevice, PPGDevice, SimulatedDevice, list_ppg, scan_eeg
from .protocol import PPGParser, parse_eeg_packet

__all__ = ["EEGDevice", "PPGDevice", "SimulatedDevice", "list_ppg", "scan_eeg",
           "PPGParser", "parse_eeg_packet"]
