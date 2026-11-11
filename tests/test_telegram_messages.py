"""Bot 只主动发三类消息：开始传输、完成、失败（失败只发一次）。

进度不主动推送，只在 /status、/task N 时回复。转发文件不再回复“在排队”，
启动、下载完成、校验重排队、其他目标网盘提醒都只写日志。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "payload"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import main as app_main
from app.db import TaskDB
from app.main import format_duration
from test_gdrive_service import (
    FakeRclone,
    FakeTelegram,
    make_service,
    make_settings,
    message_event,
)
from test_local_review_regressions import make_settings as startable_settings
from test_local_review_regressions import make_startable_service, run_start

PAYLOAD = b"google-drive-pay"  # 16 bytes = message_event default size


class FlakyTelegram(FakeTelegram):
    """第一次下载失败，之后成功：中途重试不能产生额外消息。"""

    async def download_media(self, message, *, file, progress_callback):
        self.downloads += 1
        if self.downloads == 1:
            raise RuntimeError("temporary network error")
        Path(file).write_bytes(self.payload)
        progress_callback(len(self.payload), len(self.payload))
        return file


class BrokenTelegram(FakeTelegram):
    async def download_media(self, message, *, file, progress_callback):
        raise RuntimeError("telegram unavailable")


class MessagePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.db = TaskDB(self.root / "tasks.db")
        self.addCleanup(self._temp.cleanup)
        self.addCleanup(self.db.close)

    def prepare(self, **settings_kwargs):
        settings = make_settings(self.root, **settings_kwargs)
        service = make_service(settings, self.db)
        event = message_event()
        asyncio.run(service._on_message(event))
        task_id = int(self.db.list_recent(1)[0]["id"])
        sent: list[str] = []

        async def notify(message: str) -> None:
            sent.append(message)

        service._notify = notify  # type: ignore[method-assign]
        return service, event, task_id, sent

    def test_forwarding_a_file_sends_no_queue_reply(self) -> None:
        _service, event, task_id, _sent = self.prepare()
        event.reply.assert_not_awaited()
        self.assertEqual(self.db.get(task_id)["state"], "queued")
        self.assertIsNone(self.db.get(task_id)["started_at"])

    def test_successful_transfer_sends_exactly_started_and_completed(self) -> None:
        service, _, task_id, sent = self.prepare()
        service.client = FakeTelegram(PAYLOAD)  # type: ignore[assignment]
        service.rclone = FakeRclone()  # type: ignore[assignment]
        asyncio.run(service._download_one(task_id))
        asyncio.run(service._upload_one(task_id))

        self.assertEqual(len(sent), 2, sent)
        started, completed = sent
        self.assertIn(f"开始传输 #{task_id}", started)
        self.assertIn("文件：影片.mp4", started)
        self.assertIn("大小：16.00B", started)
        self.assertIn("目标：Google Drive：telegram", started)
        self.assertIn(f"/task {task_id}", started)

        self.assertIn(f"Bot 传输已完成 #{task_id}", completed)
        self.assertIn("路径：Google Drive：telegram/影片.mp4", completed)
        self.assertIn("大小：16.00B", completed)
        self.assertIn("用时：", completed)
        self.assertIn(f"/confirm #{task_id}", completed)
        self.assertLessEqual(len(completed.splitlines()), 6)
        self.assertIsNotNone(self.db.get(task_id)["started_at"])

    def test_retries_and_restarts_do_not_repeat_the_started_message(self) -> None:
        service, _, task_id, sent = self.prepare()
        service.client = FlakyTelegram(PAYLOAD)  # type: ignore[assignment]
        service.rclone = FakeRclone()  # type: ignore[assignment]
        with patch.object(app_main.asyncio, "sleep", AsyncMock()):
            asyncio.run(service._download_one(task_id))
        self.assertEqual(self.db.get(task_id)["state"], "waiting_upload")
        # 模拟重启恢复后再次进入下载：已开始过的任务不再通知。
        self.db.update(task_id, state="queued", local_path=None, downloaded_bytes=0)
        service.client = FakeTelegram(PAYLOAD)  # type: ignore[assignment]
        asyncio.run(service._download_one(task_id))
        self.assertEqual([m for m in sent if "开始传输" in m], sent[:1])
        self.assertEqual(len(sent), 1, sent)

    def test_final_failure_is_reported_once_with_retry_hint(self) -> None:
        service, _, task_id, sent = self.prepare(max_retries=2)
        service.client = BrokenTelegram(PAYLOAD)  # type: ignore[assignment]
        service.rclone = FakeRclone()  # type: ignore[assignment]
        with patch.object(app_main.asyncio, "sleep", AsyncMock()):
            asyncio.run(service._download_one(task_id))
        self.assertEqual(self.db.get(task_id)["state"], "download_failed")
        self.assertEqual(len(sent), 2, sent)
        self.assertIn("开始传输", sent[0])
        self.assertIn(f"任务失败 #{task_id}", sent[1])
        self.assertIn("原因：", sent[1])
        self.assertIn(f"/retry {task_id}", sent[1])

    def test_upload_failure_is_the_single_final_message(self) -> None:
        service, _, task_id, sent = self.prepare(max_retries=1)
        service.client = FakeTelegram(PAYLOAD)  # type: ignore[assignment]
        service.rclone = FakeRclone(fail_upload=True)  # type: ignore[assignment]
        asyncio.run(service._download_one(task_id))
        asyncio.run(service._upload_one(task_id))
        self.assertEqual(len(sent), 2, sent)
        self.assertIn(f"任务失败 #{task_id}", sent[1])
        self.assertIn("本地完整文件已保留", sent[1])

    def test_user_retry_announces_the_new_attempt(self) -> None:
        service, _, task_id, sent = self.prepare(max_retries=1)
        service.client = BrokenTelegram(PAYLOAD)  # type: ignore[assignment]
        service.rclone = FakeRclone()  # type: ignore[assignment]
        asyncio.run(service._download_one(task_id))
        retry = SimpleNamespace(reply=AsyncMock())
        asyncio.run(service._command_retry(retry, ["/retry", str(task_id)]))
        self.assertIsNone(self.db.get(task_id)["started_at"])
        service.client = FakeTelegram(PAYLOAD)  # type: ignore[assignment]
        asyncio.run(service._download_one(task_id))
        self.assertEqual(sum("开始传输" in m for m in sent), 2)

    def test_startup_sends_nothing(self) -> None:
        db = TaskDB(self.root / "startup.db")
        try:
            service = make_startable_service(startable_settings(self.root / "s"), db)
            run_start(service)
            service._notify.assert_not_awaited()
        finally:
            db.close()


class ProgressOnDemandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.db = TaskDB(self.root / "tasks.db")
        self.addCleanup(self._temp.cleanup)
        self.addCleanup(self.db.close)
        self.service = make_service(make_settings(self.root), self.db)
        asyncio.run(self.service._on_message(message_event(size=1000 * 1024**2)))
        self.task_id = int(self.db.list_recent(1)[0]["id"])

    def test_task_command_shows_percent_bytes_speed_and_eta(self) -> None:
        self.db.update(
            self.task_id, state="downloading", downloaded_bytes=250 * 1024**2,
            started_at=1000.0,
        )
        clock = iter([100.0, 110.0])
        with patch.object(app_main.time, "monotonic", lambda: next(clock)):
            self.service._record_progress(self.task_id, 150 * 1024**2)
            self.service._record_progress(self.task_id, 250 * 1024**2)
        with patch.object(app_main.time, "monotonic", lambda: 111.0), \
                patch.object(app_main.time, "time", lambda: 1240.0):
            event = SimpleNamespace(reply=AsyncMock())
            asyncio.run(self.service._handle_command(event, f"/task {self.task_id}"))
        reply = event.reply.await_args.args[0]
        self.assertIn("进度：25.0%（250.00MB / 1000.00MB）", reply)
        self.assertIn("10.00MB/s", reply)
        self.assertIn("约剩 1 分 15 秒", reply)
        self.assertIn("已用 4 分 0 秒", reply)

    def test_status_lists_active_transfers(self) -> None:
        self.db.update(self.task_id, state="downloading", downloaded_bytes=500 * 1024**2)
        self.service.download_tasks = {self.task_id: object()}
        status = self.service._format_status()
        self.assertIn("进行中：", status)
        self.assertIn(f"#{self.task_id} 影片.mp4：进度：50.0%", status)

    def test_stage_without_bytes_shows_the_stage(self) -> None:
        self.db.update(self.task_id, state="uploading")
        text = self.service._format_task(self.db.get(self.task_id))
        self.assertIn("阶段：正在写入 CloudDrive2", text)

    def test_idle_task_has_no_progress_line(self) -> None:
        text = self.service._format_task(self.db.get(self.task_id))
        self.assertNotIn("进度：", text)
        self.assertNotIn("阶段：", text)


class FormatDurationTests(unittest.TestCase):
    def test_formats(self) -> None:
        self.assertEqual(format_duration(5), "5 秒")
        self.assertEqual(format_duration(234), "3 分 54 秒")
        self.assertEqual(format_duration(3 * 3600 + 120), "3 小时 2 分")
        self.assertEqual(format_duration(-3), "0 秒")


class StartedAtMigrationTests(unittest.TestCase):
    def test_existing_database_gains_started_at(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "old.db"
            TaskDB(path).close()
            conn = sqlite3.connect(path)
            conn.execute("ALTER TABLE tasks DROP COLUMN started_at")
            conn.commit()
            conn.close()
            db = TaskDB(path)
            try:
                cols = {r[1] for r in db._conn.execute("PRAGMA table_info(tasks)")}
                self.assertIn("started_at", cols)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
