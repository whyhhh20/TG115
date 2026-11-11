"""窗口被内存/磁盘压到 0 时必须留下可见原因：限频警告日志 + /status 文本。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / "payload"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.db import TaskDB
from app.resources import (
    HOLD_LOG_INTERVAL_SECONDS,
    AdaptiveWindow,
    ResourceSnapshot,
)
from test_gdrive_service import make_service, make_settings

MB = 1024**2
GB = 1024**3


def window_settings(*, hard_mb: int = 512, soft_mb: int = 1024, min_free_gb: int = 6):
    return SimpleNamespace(
        memory_hard_min_bytes=hard_mb * MB,
        memory_soft_min_bytes=soft_mb * MB,
        min_free_disk_bytes=min_free_gb * GB,
        cpu_pressure=90,
        cpu_target_high=80,
        cpu_target_low=60,
        ramp_up_step=1,
        ramp_down_factor=0.5,
    )


def snapshot(*, memory_mb: int = 4096, disk_free_gb: float = 30.0) -> ResourceSnapshot:
    return ResourceSnapshot(
        cpu_percent=5,
        memory_available=memory_mb * MB,
        swap_used=0,
        disk_free=int(disk_free_gb * GB),
        network_bytes_per_second=MB,
        sampled_at=0,
    )


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_window(name: str = "download", **settings) -> tuple[AdaptiveWindow, FakeClock]:
    window = AdaptiveWindow(window_settings(**settings), name)
    clock = FakeClock()
    window._clock = clock
    return window, clock


def update(window: AdaptiveWindow, snap: ResourceSnapshot, *, healthy: bool = True,
           demand: bool = True) -> int:
    return window.update(snap, recent_errors=0, destination_healthy=healthy,
                         demand_present=demand)


class WindowHoldReasonTests(unittest.TestCase):
    def test_memory_hold_records_numbers_and_warns(self) -> None:
        window, _ = make_window()
        with self.assertLogs("tg115.resources", "WARNING") as logs:
            self.assertEqual(update(window, snapshot(memory_mb=340)), 0)
        self.assertIn("可用内存 340MB 低于硬阈值 512MB", window.hold_reason)
        self.assertEqual(len(logs.records), 1)
        self.assertIn("下载窗口暂停为 0", logs.output[0])
        self.assertIn("340MB", logs.output[0])

    def test_warning_is_rate_limited_while_the_cause_persists(self) -> None:
        window, clock = make_window()
        with self.assertLogs("tg115.resources", "WARNING") as logs:
            update(window, snapshot(memory_mb=340))
            for _ in range(20):
                clock.now += 3
                update(window, snapshot(memory_mb=339))
            self.assertEqual(len(logs.records), 1)
            clock.now += HOLD_LOG_INTERVAL_SECONDS
            update(window, snapshot(memory_mb=338))
        self.assertEqual(len(logs.records), 2)

    def test_recovery_clears_the_reason_and_reopens_the_window(self) -> None:
        window, _ = make_window()
        with self.assertLogs("tg115.resources", "WARNING"):
            update(window, snapshot(memory_mb=340))
        self.assertGreaterEqual(update(window, snapshot(memory_mb=4096)), 1)
        self.assertIsNone(window.hold_reason)

    def test_minimum_profile_floor_lets_a_921mb_vps_transfer(self) -> None:
        window, _ = make_window(hard_mb=128, soft_mb=256)
        self.assertGreaterEqual(update(window, snapshot(memory_mb=340)), 1)
        self.assertIsNone(window.hold_reason)

    def test_disk_hold_only_applies_to_downloads(self) -> None:
        download, _ = make_window("download")
        upload, _ = make_window("upload")
        with self.assertLogs("tg115.resources", "WARNING") as logs:
            self.assertEqual(update(download, snapshot(disk_free_gb=5.5)), 0)
        self.assertIn("磁盘剩余 5.5GB 不高于保留线 6.0GB", download.hold_reason)
        self.assertIn("下载窗口暂停为 0", logs.output[0])
        self.assertGreaterEqual(update(upload, snapshot(disk_free_gb=5.5)), 1)
        self.assertIsNone(upload.hold_reason)

    def test_changing_cause_warns_immediately(self) -> None:
        window, clock = make_window()
        with self.assertLogs("tg115.resources", "WARNING") as logs:
            update(window, snapshot(memory_mb=340))
            clock.now += 3
            update(window, snapshot(memory_mb=4096, disk_free_gb=1))
        self.assertEqual(len(logs.records), 2)
        self.assertIn("磁盘剩余", window.hold_reason)

    def test_idle_hold_sets_reason_without_logging(self) -> None:
        window, _ = make_window()
        with self.assertNoLogs("tg115.resources", "WARNING"):
            self.assertEqual(update(window, snapshot(memory_mb=340), demand=False), 0)
        self.assertIn("可用内存 340MB", window.hold_reason)

    def test_unhealthy_destination_sets_reason_without_duplicate_warning(self) -> None:
        window, _ = make_window()
        with self.assertNoLogs("tg115.resources", "WARNING"):
            self.assertEqual(update(window, snapshot(), healthy=False), 0)
        self.assertEqual(window.hold_reason, "目标网盘健康检查未通过")


class StatusPauseReasonTests(unittest.TestCase):
    def test_status_shows_why_windows_are_held(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = TaskDB(root / "tasks.db")
            try:
                service = make_service(make_settings(root), db)
                service.download_window = SimpleNamespace(
                    value=0, hold_reason="可用内存 340MB 低于硬阈值 512MB（MEMORY_HARD_MIN_MB）"
                )
                service.upload_window = SimpleNamespace(
                    value=0, hold_reason="可用内存 340MB 低于硬阈值 512MB（MEMORY_HARD_MIN_MB）"
                )
                status = service._format_status()
                self.assertIn("下载动态窗口：0", status)
                self.assertIn("下载暂停原因：可用内存 340MB 低于硬阈值 512MB", status)
                self.assertIn("上传暂停原因：可用内存 340MB", status)
            finally:
                db.close()

    def test_status_has_no_pause_line_when_windows_are_open(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = TaskDB(root / "tasks.db")
            try:
                service = make_service(make_settings(root), db)
                status = service._format_status()
                self.assertNotIn("暂停原因", status)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
