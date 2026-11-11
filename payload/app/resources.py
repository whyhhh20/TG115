from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import psutil

from .config import Settings

# 窗口被内存/磁盘压到 0 时的警告间隔：同一种原因持续存在时最多这么久提醒一次。
HOLD_LOG_INTERVAL_SECONDS = 300.0
MIB = 1024**2
GIB = 1024**3


@dataclass(frozen=True)
class ResourceSnapshot:
    cpu_percent: float
    memory_available: int
    swap_used: int
    disk_free: int
    network_bytes_per_second: float
    sampled_at: float


class ResourceMonitor:
    def __init__(self, download_dir: Path):
        self.download_dir = download_dir
        self._cpu_samples: deque[float] = deque(maxlen=5)
        self._last_net = psutil.net_io_counters()
        self._last_time = time.monotonic()
        psutil.cpu_percent(interval=None)

    def sample(self) -> ResourceSnapshot:
        now = time.monotonic()
        cpu = psutil.cpu_percent(interval=None)
        self._cpu_samples.append(float(cpu))
        cpu_average = sum(self._cpu_samples) / len(self._cpu_samples)
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        disk = psutil.disk_usage(str(self.download_dir))
        net = psutil.net_io_counters()
        elapsed = max(0.001, now - self._last_time)
        network_delta = (net.bytes_recv - self._last_net.bytes_recv) + (
            net.bytes_sent - self._last_net.bytes_sent
        )
        network_rate = max(0.0, network_delta / elapsed)
        self._last_net = net
        self._last_time = now
        return ResourceSnapshot(
            cpu_percent=cpu_average,
            memory_available=int(memory.available),
            swap_used=int(swap.used),
            disk_free=int(disk.free),
            network_bytes_per_second=network_rate,
            sampled_at=time.time(),
        )


class AdaptiveWindow:
    """Feedback-based task window with no user-defined fixed task count."""

    def __init__(self, settings: Settings, name: str):
        self.settings = settings
        self.name = name
        self.value = 1
        self._last_network_rate = 0.0
        self._high_pressure_cycles = 0
        # 当前把窗口压到 0 的原因（给 /status 展示）；未被压住时为 None。
        self.hold_reason: str | None = None
        self._hold_kind: str | None = None
        self._hold_logged_at: float | None = None
        self._clock = time.monotonic
        self.log = logging.getLogger("tg115.resources")

    @staticmethod
    def _memory_derived_ceiling(memory_available: int) -> int:
        # A safety ceiling derived only from current memory, not a fixed task cap.
        # Each active orchestration task receives at least a 32 MiB allowance.
        return max(1, memory_available // (32 * 1024**2))

    def update(
        self,
        snapshot: ResourceSnapshot,
        *,
        recent_errors: int,
        destination_healthy: bool,
        demand_present: bool,
    ) -> int:
        memory_cap = self._memory_derived_ceiling(snapshot.memory_available)
        hold = self._hold_cause(snapshot, destination_healthy)
        if hold is not None:
            self.value = 0
            self._record_hold(*hold, demand_present=demand_present)
            return self.value
        self.hold_reason = None
        self._hold_kind = None
        self._hold_logged_at = None

        if snapshot.cpu_percent >= self.settings.cpu_pressure or recent_errors >= 3:
            self._high_pressure_cycles += 1
        else:
            self._high_pressure_cycles = 0

        if self._high_pressure_cycles >= 2:
            self.value = max(
                1, math.floor(max(1, self.value) * self.settings.ramp_down_factor)
            )
        elif (
            (
                snapshot.cpu_percent < self.settings.cpu_pressure
                and snapshot.cpu_percent > self.settings.cpu_target_high
            )
            or snapshot.memory_available < self.settings.memory_soft_min_bytes
        ):
            self.value = max(1, self.value - 1)
        elif (
            demand_present
            and snapshot.cpu_percent < self.settings.cpu_target_low
            and recent_errors == 0
        ):
            throughput_not_worse = (
                self._last_network_rate <= 0
                or snapshot.network_bytes_per_second
                >= self._last_network_rate * 0.90
            )
            if throughput_not_worse:
                self.value += self.settings.ramp_up_step

        self.value = min(max(1, self.value), memory_cap)
        self._last_network_rate = snapshot.network_bytes_per_second
        return self.value

    def _hold_cause(
        self, snapshot: ResourceSnapshot, destination_healthy: bool
    ) -> tuple[str, str] | None:
        """返回 (原因类别, 带实际数值的说明)；不需要把窗口压到 0 时返回 None。"""
        hard_min = self.settings.memory_hard_min_bytes
        if snapshot.memory_available < hard_min:
            return (
                "memory",
                (
                    f"可用内存 {snapshot.memory_available // MIB}MB 低于硬阈值 "
                    f"{hard_min // MIB}MB（MEMORY_HARD_MIN_MB）"
                ),
            )
        if not destination_healthy:
            return ("destination", "目标网盘健康检查未通过")
        min_free = self.settings.min_free_disk_bytes
        if self.name == "download" and snapshot.disk_free <= min_free:
            return (
                "disk",
                (
                    f"磁盘剩余 {snapshot.disk_free / GIB:.1f}GB 不高于保留线 "
                    f"{min_free / GIB:.1f}GB（MIN_FREE_DISK_GB）"
                ),
            )
        return None

    def _record_hold(self, kind: str, reason: str, *, demand_present: bool) -> None:
        self.hold_reason = reason
        # 目标网盘不健康时 rclone 健康检查自己会记录警告，这里不重复。
        if kind == "destination" or not demand_present:
            self._hold_kind = kind
            return
        now = self._clock()
        if (
            kind != self._hold_kind
            or self._hold_logged_at is None
            or now - self._hold_logged_at >= HOLD_LOG_INTERVAL_SECONDS
        ):
            label = "下载" if self.name == "download" else "上传"
            self.log.warning("%s窗口暂停为 0：%s，任务会继续排队", label, reason)
            self._hold_logged_at = now
        self._hold_kind = kind
