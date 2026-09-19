"""Monotonic session clock with a documented UTC anchor uncertainty."""
from __future__ import annotations
import time


class SessionClock:
    def __init__(self):
        before = time.perf_counter_ns()
        self.utc_anchor_ns = time.time_ns()
        after = time.perf_counter_ns()
        self.anchor_ns = (before + after) // 2
        self.anchor_uncertainty_ns = (after - before) // 2

    def relative(self, at_ns: int) -> float:
        return (at_ns - self.anchor_ns) / 1e9

    def metadata(self) -> dict:
        return {
            "clock": "time.perf_counter_ns", "anchor_monotonic_ns": self.anchor_ns,
            "anchor_utc_ns": self.utc_anchor_ns,
            "anchor_uncertainty_ns": self.anchor_uncertainty_ns,
            "utc_mapping": "utc_anchor_ns + received_ns - anchor_monotonic_ns",
            "timing_basis": "host_receive_time; not hardware synchronized",
        }
