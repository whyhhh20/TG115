"""TRANSFER_MODE=stream：所有文件流式传输，不占本地额度，但仍受磁盘安全线约束。"""
from __future__ import annotations

import asyncio
import base64
import os
import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
sys.path.insert(0, str(SOURCE / "payload"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import main as app_main
from app.config import Settings
from app.db import TaskDB
from test_gdrive_deployment import cd2_config, check_config
from test_gdrive_installer import parse_config, valid_values
from test_gdrive_service import (
    FakeRclone,
    FakeTelegram,
    make_service,
    make_settings,
    message_event,
)

import installer
from installer import InstallerApp, build_deploy_config, transfer_mode_value

GB = 1024**3
PAYLOAD = b"google-drive-pay"  # 16 bytes = message_event default size


def env_settings(**extra: str) -> Settings:
    def enc(value: str) -> str:
        return base64.b64encode(value.encode()).decode()

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        env = {
            "TELEGRAM_API_ID": "1",
            "TELEGRAM_API_HASH_B64": enc("a" * 32),
            "BOT_TOKEN_B64": enc("1:" + "a" * 25),
            "ALLOWED_USER_ID": "2",
            "CD2_WEBDAV_URL_B64": enc("http://cd2/dav"),
            "CD2_WEBDAV_USERNAME_B64": enc("u"),
            "CD2_WEBDAV_PASSWORD_B64": enc("p"),
            "CD2_TARGET_PATH_B64": enc("115/T"),
            "DATA_DIR": str(root / "data"),
            "DOWNLOAD_DIR": str(root / "downloads"),
            "LOG_DIR": str(root / "logs"),
            "RCLONE_CONFIG_PATH": str(root / "rclone.conf"),
            **extra,
        }
        with patch.dict(os.environ, env, clear=True):
            return Settings.from_env()


class SettingsTests(unittest.TestCase):
    def test_default_is_auto(self) -> None:
        settings = env_settings()
        self.assertEqual(settings.transfer_mode, "auto")
        self.assertFalse(settings.always_stream)

    def test_stream_mode(self) -> None:
        settings = env_settings(TRANSFER_MODE=" Stream ")
        self.assertEqual(settings.transfer_mode, "stream")
        self.assertTrue(settings.always_stream)

    def test_invalid_mode_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "TRANSFER_MODE"):
            env_settings(TRANSFER_MODE="disk")


class ReserveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.db = TaskDB(Path(self._temp.name) / "tasks.db")
        self.addCleanup(self._temp.cleanup)
        self.addCleanup(self.db.close)

    def task(self, message_id: int, size: int) -> int:
        task, _ = self.db.create_task(
            chat_id=1, message_id=message_id, sender_id=9,
            file_name=f"{message_id}.bin", file_size=size,
        )
        return int(task["id"])

    def reserve(self, task_id: int, *, free: int = 50 * GB, always: bool = True):
        return self.db.reserve(
            task_id, budget_bytes=2 * GB, current_free_bytes=free,
            minimum_free_bytes=6 * GB, always_stream=always,
        )

    def test_small_file_streams_and_uses_no_local_budget(self) -> None:
        task_id = self.task(1, 100 * 1024**2)
        ok, reason = self.reserve(task_id)
        self.assertTrue(ok)
        self.assertIn("TRANSFER_MODE=stream", reason)
        self.assertEqual(self.db.get(task_id)["transfer_mode"], "stream")
        self.assertEqual(self.db.used_local_bytes(), 0)

    def test_full_local_budget_does_not_block_streams(self) -> None:
        local = self.task(1, 2 * GB)
        self.assertTrue(self.reserve(local, always=False)[0])
        self.assertEqual(self.db.used_local_bytes(), 2 * GB)
        streamed = self.task(2, GB)
        self.assertTrue(self.reserve(streamed)[0])
        self.assertEqual(self.db.get(streamed)["transfer_mode"], "stream")

    def test_disk_floor_still_holds_streams(self) -> None:
        task_id = self.task(1, 10 * 1024**2)
        ok, reason = self.reserve(task_id, free=6 * GB)
        self.assertFalse(ok)
        self.assertIn("磁盘", reason)
        self.assertEqual(self.db.get(task_id)["state"], "queued")

    def test_auto_keeps_small_files_local(self) -> None:
        task_id = self.task(1, 100 * 1024**2)
        self.assertTrue(self.reserve(task_id, always=False)[0])
        self.assertEqual(self.db.get(task_id)["transfer_mode"], "local")


class StreamServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.db = TaskDB(self.root / "tasks.db")
        self.addCleanup(self._temp.cleanup)
        self.addCleanup(self.db.close)
        self.settings = make_settings(self.root, max_retries=2)
        self.settings.transfer_mode = "stream"
        self.service = make_service(self.settings, self.db)
        asyncio.run(self.service._on_message(message_event()))
        self.task_id = int(self.db.list_recent(1)[0]["id"])
        self.sent: list[str] = []

        async def notify(message: str) -> None:
            self.sent.append(message)

        self.service._notify = notify  # type: ignore[method-assign]
        self.service.rclone = FakeRclone()  # type: ignore[assignment]

    def run_scheduler_once(self) -> None:
        async def scenario() -> None:
            await self.service._start_downloads()
            await asyncio.gather(*self.service.download_tasks.values())

        asyncio.run(scenario())

    def test_small_file_streams_end_to_end_with_two_messages(self) -> None:
        self.service.client = FakeTelegram(PAYLOAD, stream=True)  # type: ignore[assignment]
        self.run_scheduler_once()
        task = self.db.get(self.task_id)
        self.assertEqual(task["state"], "completed")
        self.assertEqual(task["transfer_mode"], "stream")
        self.assertEqual(self.service.rclone.files, {"影片.mp4": PAYLOAD})  # type: ignore[attr-defined]
        self.assertEqual(list(self.settings.download_dir.iterdir()), [])
        self.assertEqual(len(self.sent), 2, self.sent)
        self.assertIn("开始传输", self.sent[0])
        self.assertIn("Bot 传输已完成", self.sent[1])

    def test_failed_attempt_restarts_cleanly(self) -> None:
        class FlakyStream(FakeTelegram):
            async def download_media(self, message, *, file, progress_callback):
                self.downloads += 1
                await file.write(self.payload[:5])
                progress_callback(5, len(self.payload))
                if self.downloads == 1:
                    raise RuntimeError("connection reset")
                await file.write(self.payload[5:])
                progress_callback(len(self.payload), len(self.payload))

        self.service.client = FlakyStream(PAYLOAD, stream=True)  # type: ignore[assignment]
        with patch.object(app_main.asyncio, "sleep", AsyncMock()):
            self.run_scheduler_once()
        task = self.db.get(self.task_id)
        self.assertEqual(task["state"], "completed")
        self.assertEqual(task["download_retries"], 1)
        self.assertEqual(self.service.rclone.files, {"影片.mp4": PAYLOAD})  # type: ignore[attr-defined]
        self.assertIn(f".uploading-{self.task_id}-影片.mp4", self.service.rclone.removed)  # type: ignore[attr-defined]
        self.assertEqual(len(self.sent), 2, self.sent)

    def test_final_failure_then_user_retry_streams_again(self) -> None:
        class Broken(FakeTelegram):
            async def download_media(self, message, *, file, progress_callback):
                raise RuntimeError("telegram unavailable")

        self.service.client = Broken(PAYLOAD, stream=True)  # type: ignore[assignment]
        with patch.object(app_main.asyncio, "sleep", AsyncMock()):
            self.run_scheduler_once()
        self.assertEqual(self.db.get(self.task_id)["state"], "download_failed")
        self.assertEqual(len(self.sent), 2)
        self.assertIn(f"任务失败 #{self.task_id}", self.sent[1])
        self.assertIn(f"/retry {self.task_id}", self.sent[1])

        event = SimpleNamespace(reply=AsyncMock())
        asyncio.run(self.service._command_retry(event, ["/retry", str(self.task_id)]))
        self.assertIn("重新进入下载队列", event.reply.await_args.args[0])
        self.service.client = FakeTelegram(PAYLOAD, stream=True)  # type: ignore[assignment]
        self.run_scheduler_once()
        task = self.db.get(self.task_id)
        self.assertEqual(task["state"], "completed")
        self.assertEqual(task["transfer_mode"], "stream")
        self.assertEqual(sum("开始传输" in m for m in self.sent), 2)

    def test_restart_mid_stream_requeues_without_a_second_started_message(self) -> None:
        self.db.update(
            self.task_id, state="streaming", transfer_mode="stream",
            downloaded_bytes=7, remote_path=f".uploading-{self.task_id}-影片.mp4",
            started_at=1000.0,
        )
        recovered = self.db.recover(self.settings.download_dir)
        self.assertEqual(recovered["queued"], 1)
        task = self.db.get(self.task_id)
        self.assertEqual((task["state"], task["downloaded_bytes"]), ("queued", 0))
        self.service.client = FakeTelegram(PAYLOAD, stream=True)  # type: ignore[assignment]
        self.run_scheduler_once()
        self.assertEqual(self.db.get(self.task_id)["state"], "completed")
        self.assertEqual(len(self.sent), 1, self.sent)
        self.assertIn("Bot 传输已完成", self.sent[0])

    def test_task_and_status_show_streaming_progress(self) -> None:
        self.db.update(
            self.task_id, state="streaming", transfer_mode="stream", downloaded_bytes=8
        )
        event = SimpleNamespace(reply=AsyncMock())
        asyncio.run(self.service._handle_command(event, f"/task {self.task_id}"))
        self.assertIn("进度：50.0%", event.reply.await_args.args[0])
        self.service.download_tasks = {self.task_id: object()}
        status = self.service._format_status()
        self.assertIn("传输方式：全部流式", status)
        self.assertIn(f"#{self.task_id} 影片.mp4：进度：50.0%", status)

    def test_auto_mode_status_label(self) -> None:
        self.settings.transfer_mode = "auto"
        self.assertIn("传输方式：自动", self.service._format_status())


class InstallerTests(unittest.TestCase):
    def test_config_defaults_to_auto_and_writes_stream(self) -> None:
        values = valid_values(installer.DESTINATION_GDRIVE)
        self.assertEqual(parse_config(build_deploy_config(values))["TRANSFER_MODE"], "auto")
        values["transfer_mode"] = "stream"
        self.assertEqual(parse_config(build_deploy_config(values))["TRANSFER_MODE"], "stream")

    def test_invalid_mode_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "传输方式"):
            transfer_mode_value({"transfer_mode": "disk"})

    def test_gui_choice_reaches_the_generated_config(self) -> None:
        root = tk.Tk()
        root.withdraw()
        app = InstallerApp(root)
        try:
            self.assertEqual(app.values["transfer_mode"].get(), "auto")
            self.assertEqual(set(app.transfer_mode_buttons), {"auto", "stream"})
            app.transfer_mode_buttons["stream"].invoke()
            config = parse_config(app._build_config(app._snapshot()))
            self.assertEqual(config["TRANSFER_MODE"], "stream")
        finally:
            app.busy = False
            app._stop_ui_drain()
            root.destroy()


@unittest.skipUnless(__import__("shutil").which("bash"), "需要 bash")
class RemoteInstallCheckTests(unittest.TestCase):
    def test_check_config_reports_stream(self) -> None:
        result = check_config(cd2_config(extra={"TRANSFER_MODE": "stream"}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TG115_TRANSFER_MODE=stream", result.stdout)

    def test_check_config_defaults_to_auto(self) -> None:
        result = check_config(cd2_config())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TG115_TRANSFER_MODE=auto", result.stdout)

    def test_check_config_rejects_unknown_mode(self) -> None:
        result = check_config(cd2_config(extra={"TRANSFER_MODE": "disk"}))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TRANSFER_MODE 只能是 auto 或 stream", result.stderr)


if __name__ == "__main__":
    unittest.main()
