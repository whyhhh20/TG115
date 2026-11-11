"""Concrete regression cases found during the parent integration review.

Both UI options transfer through CloudDrive2. Tasks left behind by the earlier
direct-Google implementation carry a Google folder / OAuth binding and must
never be silently rewritten into CloudDrive2 tasks.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "payload"))
from app.db import TaskDB
from app.main import (
    DESTINATION_CLOUDDRIVE2,
    DESTINATION_GDRIVE,
    TransferService,
    clouddrive2_identity,
)


def make_settings(
    root: Path, destination_type: str = DESTINATION_GDRIVE
) -> SimpleNamespace:
    for name in ("data", "downloads", "logs"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        destination_type=destination_type,
        destination_target="telegram",
        cd2_target="telegram",
        cd2_url="http://clouddrive2:19798/dav",
        cd2_user="owner",
        cd2_password="local-test-password",
        download_dir=root / "downloads",
        data_dir=root / "data",
        log_dir=root / "logs",
        bot_token="local-test-only",
        allowed_user_id=1,
    )


def make_startable_service(settings: SimpleNamespace, db: TaskDB) -> TransferService:
    service = TransferService.__new__(TransferService)
    service.settings = settings
    service.db = db
    service.log = Mock()
    service.client = SimpleNamespace(
        start=AsyncMock(),
        get_me=AsyncMock(return_value=SimpleNamespace(username="local_test")),
    )
    service._resource_loop = AsyncMock()
    service._scheduler_loop = AsyncMock()
    service._notify = AsyncMock()
    return service


def run_start(service: TransferService) -> None:
    async def start() -> None:
        await service.start()
        await asyncio.gather(*service._background)

    asyncio.run(start())


class LocalReviewRegressions(unittest.TestCase):
    def test_webdav_path_case_remains_part_of_destination_identity(self) -> None:
        settings = SimpleNamespace(
            cd2_url="https://NAS.EXAMPLE/dav/Folder", cd2_user="owner"
        )
        original = clouddrive2_identity(settings)
        settings.cd2_url = "https://nas.example/dav/Folder/"
        self.assertEqual(clouddrive2_identity(settings), original)
        settings.cd2_url = "https://nas.example/dav/folder"
        self.assertNotEqual(clouddrive2_identity(settings), original)

    def test_start_does_not_adopt_a_legacy_direct_google_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = TaskDB(root / "tasks.db")
            try:
                settings = make_settings(root)
                task, _ = db.create_task(
                    chat_id=1,
                    message_id=1,
                    sender_id=1,
                    file_name="pending.bin",
                    file_size=1,
                    destination={
                        "destination_type": DESTINATION_GDRIVE,
                        "destination_root": "folder-abc",
                        "destination_account": "gdrive:0123456789abcdef",
                    },
                )
                service = make_startable_service(settings, db)
                run_start(service)

                stored = db.get(task["id"])
                # 旧直连绑定原样保留，没有被改成 CD2。
                self.assertEqual(stored["destination_root"], "folder-abc")
                self.assertEqual(
                    stored["destination_account"], "gdrive:0123456789abcdef"
                )
                self.assertEqual(stored["destination_type"], DESTINATION_GDRIVE)
                self.assertEqual(db.pending_mismatch_count(service._binding()), 1)
                # 启动时不发 Telegram 消息；不匹配的任务只记日志（/status 也会显示）。
                service._notify.assert_not_awaited()
                warnings = " ".join(
                    str(call.args[0]) for call in service.log.warning.call_args_list
                )
                self.assertIn("不会自动传输", warnings)
            finally:
                db.close()

    def test_start_does_not_adopt_a_google_task_with_unknown_account(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = TaskDB(root / "tasks.db")
            try:
                settings = make_settings(root)
                task, _ = db.create_task(
                    chat_id=1,
                    message_id=1,
                    sender_id=1,
                    file_name="pending.bin",
                    file_size=1,
                    destination={"destination_type": DESTINATION_GDRIVE},
                )
                service = make_startable_service(settings, db)
                run_start(service)

                stored = db.get(task["id"])
                self.assertIsNone(stored["destination_account"])
                self.assertIsNone(stored["destination_root"])
                # 目录相同不足以证明它属于当前 CD2 目标，必须保持未认领。
                self.assertEqual(db.pending_mismatch_count(service._binding()), 1)
            finally:
                db.close()

    def test_start_still_adopts_unbound_clouddrive2_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = TaskDB(root / "tasks.db")
            try:
                settings = make_settings(root, DESTINATION_CLOUDDRIVE2)
                task, _ = db.create_task(
                    chat_id=1,
                    message_id=1,
                    sender_id=1,
                    file_name="legacy.bin",
                    file_size=1,
                )
                service = make_startable_service(settings, db)
                run_start(service)

                stored = db.get(task["id"])
                self.assertEqual(
                    stored["destination_type"], DESTINATION_CLOUDDRIVE2
                )
                self.assertEqual(stored["destination_target"], "telegram")
                self.assertTrue(
                    str(stored["destination_account"]).startswith("clouddrive2:")
                )
                self.assertEqual(db.pending_mismatch_count(service._binding()), 0)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
