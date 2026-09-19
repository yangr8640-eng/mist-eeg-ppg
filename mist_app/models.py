"""Small, Qt-free contracts shared by acquisition, storage and presentation."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

EEG_UV_PER_COUNT = 0.0029802322387695312
EEG_RATE = 500
STALE_SECONDS = 3.0
READY_SECONDS = 3.0


@dataclass(frozen=True)
class DeviceMessage:
    device: str  # eeg | ppg
    kind: str  # connected | disconnected | packet | error
    received_ns: int
    raw: bytes = b""
    samples: tuple[tuple[int, ...], ...] = ()
    error: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


DeviceCallback = Callable[[DeviceMessage], None]


@dataclass(frozen=True)
class Stage:
    key: str
    name: str
    duration: float = 180.0


DEFAULT_STAGES = (
    Stage("eyes_open", "睁眼静息"),
    Stage("eyes_closed", "闭眼静息"),
    Stage("practice", "算术练习"),
    Stage("control", "对照任务"),
    Stage("stress", "压力任务"),
    Stage("recovery", "恢复阶段"),
)

SOURCE_VERSION = "Pixxrick/eeg_mist@d6be12e52dcf1b2185e0f2fb728ec77b973fe0b1"
