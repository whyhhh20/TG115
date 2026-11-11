"""Service tests for the two CloudDrive2-backed destinations (115 / Google Drive).

Both UI options transfer through CloudDrive2; only the final netdisk differs.
These tests therefore assert a shared CD2 transport, a per-netdisk task binding
and per-netdisk isolation, never a direct Google Drive connection.
"""
from __future__ import annotations

# The tests insert the packaged payload path before importing the app.
import asyncio
import sqlite3
import sys
import tempfile
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

SOURCE = Path(__file__).resolve().parents[1]
PAYLOAD = SOURCE / "payload"
sys.path.insert(0, str(PAYLOAD))

from app.db import (
    TaskDB,
    destination_matches,
    destination_values,
)
from app.main import (
    DESTINATION_CLOUDDRIVE2,
    DESTINATION_GDRIVE,
    STATE_LABELS,
    TransferService,
    clouddrive2_identity,
    destination_profile,
    normalize_destination,
    state_label,
)
from app.resources import ResourceSnapshot
from app.verify_destination import verify_destination

GB = 1024**3
MB = 1024**2

CD2_URL = "http://clouddrive2:19798/dav"
CD2_USER = "owner"


def make_settings(
    root: Path,
    *,
    destination_type: str = DESTINATION_GDRIVE,
    target: str = "telegram",
    cd2_url: str = CD2_URL,
    cd2_user: str = CD2_USER,
    max_retries: int = 2,
) -> SimpleNamespace:
    """Settings stand-in carrying the destination contract fields.

    ``gdrive_target``/``gdrive_root_folder_id`` are deliberately set to stale
    values: the binding must ignore them, because Google Drive is mounted by
    CloudDrive2 and no direct Google configuration exists any more.
    """
    data_dir = root / "data"
    download_dir = root / "downloads"
    log_dir = root / "logs"
    for path in (data_dir, download_dir, log_dir):
        path.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        allowed_user_id=987654321,
        data_dir=data_dir,
        download_dir=download_dir,
        log_dir=log_dir,
        local_budget_bytes=20 * GB,
        min_free_disk_bytes=20 * GB,
        max_retries=max_retries,
        control_interval=1.0,
        remote_health_interval=30.0,
        destination_type=destination_type,
        destination_target=target,
        cd2_target=target,
        cd2_url=cd2_url,
        cd2_user=cd2_user,
        cd2_password="local-test-password",
        gdrive_target="stale-direct-path",
        gdrive_root_folder_id="stale-folder-id",
    )


def make_service(settings: SimpleNamespace, db: TaskDB) -> TransferService:
    service = TransferService.__new__(TransferService)
    service.settings = settings
    service.db = db
    service.log = Mock()
    service.recent_download_errors = deque(maxlen=50)
    service.recent_upload_errors = deque(maxlen=50)
    service._finalize_lock = asyncio.Lock()
    service.download_tasks = {}
    service.upload_tasks = {}
    service.destination_healthy = True
    service.snapshot = ResourceSnapshot(
        cpu_percent=10,
        memory_available=4 * GB,
        swap_used=0,
        disk_free=100 * GB,
        network_bytes_per_second=MB,
        sampled_at=0,
    )
    service.download_window = SimpleNamespace(value=5)
    service.upload_window = SimpleNamespace(value=5)
    return service


class FakeRclone:
    """In-memory CloudDrive2 WebDAV stand-in."""

    def __init__(self, *, fail_upload: bool = False) -> None:
        self.files: dict[str, bytes] = {}
        self.removed: list[str] = []
        self.prepared = False
        self.fail_upload = fail_upload

    async def prepare_destination(self) -> None:
        self.prepared = True

    async def upload(self, local_path: Path, remote_path: str) -> None:
        if self.fail_upload:
            raise RuntimeError("CloudDrive2 WebDAV 不可写")
        self.files[remote_path] = local_path.read_bytes()

    async def open_upload_stream(self, remote_path: str, exact_size: int):
        owner = self

        class Stream:
            def __init__(self) -> None:
                self.data = bytearray()

            async def write(self, data: bytes) -> int:
                self.data.extend(data)
                return len(data)

            def tell(self) -> int:
                return len(self.data)

            def flush(self) -> None:
                return None

            async def finish(self) -> None:
                if len(self.data) != exact_size:
                    raise RuntimeError("stream size mismatch")
                owner.files[remote_path] = bytes(self.data)

            async def abort(self) -> None:
                return None

        return Stream()

    async def remote_size(self, remote_path: str) -> int:
        return len(self.files[remote_path])

    async def exists(self, remote_path: str) -> bool:
        return remote_path in self.files

    async def move(self, source: str, destination: str) -> None:
        self.files[destination] = self.files.pop(source)

    async def remove(self, remote_path: str) -> None:
        self.removed.append(remote_path)
        self.files.pop(remote_path, None)


class FakeTelegram:
    def __init__(self, payload: bytes, *, stream: bool = False) -> None:
        self.payload = payload
        self.stream = stream
        self.downloads = 0

    async def get_messages(self, *_: object, **__: object):
        return SimpleNamespace(media=True)

    async def download_media(
        self, _: object, *, file: object, progress_callback
    ) -> object:
        self.downloads += 1
        if self.stream:
            for chunk in (self.payload[:7], self.payload[7:]):
                await file.write(chunk)  # type: ignore[attr-defined]
                progress_callback(file.tell(), len(self.payload))  # type: ignore[attr-defined]
            return None
        Path(file).write_bytes(self.payload)  # type: ignore[arg-type]
        progress_callback(len(self.payload), len(self.payload))
        return file


def message_event(
    task_id: int = 101, file_name: str = "影片.mp4", size: int = 16
) -> SimpleNamespace:
    message = SimpleNamespace(
        id=task_id,
        media=True,
        document=None,
        photo=None,
        file=SimpleNamespace(size=size, name=file_name, ext=".mp4"),
    )
    return SimpleNamespace(
        sender_id=987654321,
        chat_id=1,
        raw_text="",
        message=message,
        reply=AsyncMock(),
    )


class StateLabelTests(unittest.TestCase):
    def test_default_labels_are_unchanged(self) -> None:
        self.assertEqual(state_label("queued"), "在排队")
        self.assertEqual(
            state_label("completed"), "Bot 传输已完成，115 官方端待确认"
        )
        self.assertEqual(state_label("confirmed"), "115 官方端已由你确认")
        self.assertEqual(state_label("uploading"), "正在写入 CloudDrive2")
        self.assertEqual(state_label("unknown-state"), "unknown-state")
        self.assertEqual(
            state_label("completed", DESTINATION_CLOUDDRIVE2),
            STATE_LABELS["completed"],
        )
        self.assertEqual(state_label("completed", None), STATE_LABELS["completed"])
        self.assertEqual(
            state_label("completed", "115"), STATE_LABELS["completed"]
        )

    def test_gdrive_labels_separate_transport_from_final_netdisk(self) -> None:
        self.assertEqual(
            state_label("completed", DESTINATION_GDRIVE),
            "CloudDrive2 已接收，Google Drive 官方端待确认",
        )
        self.assertEqual(
            state_label("confirmed", DESTINATION_GDRIVE),
            "Google Drive 官方端已由你确认",
        )
        for state in ("uploading", "streaming", "verifying"):
            label = state_label(state, DESTINATION_GDRIVE)
            self.assertIn("CloudDrive2", label, state)
            self.assertIn("Google Drive", label, state)
        for state in STATE_LABELS:
            label = state_label(state, DESTINATION_GDRIVE)
            # Google Drive 分支不得再声称走 115。
            self.assertNotIn("115", label, state)

    def test_destination_normalisation(self) -> None:
        self.assertEqual(normalize_destination(None), DESTINATION_CLOUDDRIVE2)
        self.assertEqual(normalize_destination(""), DESTINATION_CLOUDDRIVE2)
        self.assertEqual(normalize_destination(" GDrive "), DESTINATION_GDRIVE)
        self.assertEqual(normalize_destination("115"), DESTINATION_CLOUDDRIVE2)
        self.assertEqual(
            normalize_destination("google_drive"), DESTINATION_GDRIVE
        )
        with self.assertRaises(ValueError):
            normalize_destination("dropbox")
        self.assertEqual(
            destination_profile("dropbox").key, DESTINATION_CLOUDDRIVE2
        )

    def test_gdrive_profile_names_clouddrive2_as_transport(self) -> None:
        profile = destination_profile(DESTINATION_GDRIVE)
        self.assertEqual(profile.label, "Google Drive")
        self.assertEqual(profile.storage, "CloudDrive2")
        self.assertEqual(profile.official, "Google Drive 官方端")
        self.assertEqual(profile.health_name, "CloudDrive2 WebDAV")
        legacy = destination_profile(DESTINATION_CLOUDDRIVE2)
        # 网盘选项只有「115」和「Google Drive」，CD2 不是其中一个选项。
        self.assertEqual(legacy.label, "115")
        self.assertEqual(legacy.storage, "CloudDrive2")
        self.assertEqual(legacy.official, "115 官方端")
        self.assertNotEqual(profile.label, legacy.label)


class DestinationBindingTests(unittest.TestCase):
    def test_destination_values_normalises_missing_fields(self) -> None:
        self.assertEqual(
            destination_values(None),
            {
                "destination_type": "",
                "destination_target": "",
                "destination_root": "",
                "destination_account": "",
            },
        )

    def test_matching_rules(self) -> None:
        current = {
            "destination_type": DESTINATION_GDRIVE,
            "destination_target": "telegram",
            "destination_root": "",
            "destination_account": "clouddrive2:abc",
        }
        bound = {
            "destination_type": DESTINATION_GDRIVE,
            "destination_target": "telegram",
            "destination_root": "",
            "destination_account": "clouddrive2:abc",
        }
        self.assertTrue(destination_matches(bound, current))
        self.assertFalse(
            destination_matches(
                {**bound, "destination_target": "other"}, current
            )
        )
        self.assertFalse(
            destination_matches(
                {**bound, "destination_account": "clouddrive2:other"}, current
            )
        )
        self.assertFalse(
            destination_matches(
                {**bound, "destination_type": DESTINATION_CLOUDDRIVE2}, current
            )
        )
        # 旧直连 Google 的文件夹绑定不可能匹配新的 CD2 绑定。
        self.assertFalse(
            destination_matches(
                {**bound, "destination_root": "folder-abc"}, current
            )
        )
        # 有绑定的任务必须精确匹配：清空或修改目录都算换目标。
        self.assertFalse(
            destination_matches({"destination_type": DESTINATION_GDRIVE}, current)
        )
        # 升级前的记录只允许继续走 CloudDrive2。
        self.assertFalse(destination_matches({"destination_type": None}, current))
        self.assertTrue(
            destination_matches(
                {"destination_type": None},
                {"destination_type": DESTINATION_CLOUDDRIVE2},
            )
        )

    def test_both_destinations_bind_the_same_clouddrive2_transport(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = TaskDB(root / "tasks.db")
            try:
                legacy = make_service(
                    make_settings(root, destination_type=DESTINATION_CLOUDDRIVE2),
                    db,
                )._binding()
                google = make_service(
                    make_settings(root, destination_type=DESTINATION_GDRIVE), db
                )._binding()
                for binding in (legacy, google):
                    self.assertEqual(binding["destination_target"], "telegram")
                    self.assertEqual(binding["destination_root"], "")
                    self.assertTrue(
                        binding["destination_account"].startswith("clouddrive2:")
                    )
                    self.assertNotIn(
                        "local-test-password", " ".join(binding.values())
                    )
                self.assertEqual(
                    legacy["destination_account"], google["destination_account"]
                )
                # 共同传输层不等于同一目标：最终网盘必须区分。
                self.assertNotEqual(
                    legacy["destination_type"], google["destination_type"]
                )
                self.assertFalse(destination_matches(legacy, google))
                self.assertFalse(destination_matches(google, legacy))
            finally:
                db.close()

    def test_binding_ignores_stale_direct_google_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = TaskDB(root / "tasks.db")
            try:
                settings = make_settings(root)
                service = make_service(settings, db)
                binding = service._binding()
                self.assertEqual(binding["destination_target"], "telegram")
                self.assertEqual(binding["destination_root"], "")
                self.assertNotIn("stale-direct-path", binding.values())
                self.assertNotIn("stale-folder-id", binding.values())
                # 清空选中路径（WebDAV 根目录就是网盘根）也是合法绑定。
                settings.destination_target = ""
                settings.cd2_target = ""
                self.assertEqual(service._binding()["destination_target"], "")
            finally:
                db.close()

    def test_created_task_persists_binding_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = TaskDB(root / "tasks.db")
            try:
                service = make_service(make_settings(root), db)
                event = message_event()
                asyncio.run(service._on_message(event))
                task = db.list_recent(1)[0]
                self.assertEqual(task["destination_type"], DESTINATION_GDRIVE)
                self.assertEqual(task["destination_target"], "telegram")
                self.assertIsNone(task["destination_root"])
                self.assertTrue(
                    str(task["destination_account"]).startswith("clouddrive2:")
                )
                # 转发后不再回复“已接收/在排队”，开始传输时才通知。
                event.reply.assert_not_awaited()
                stored = " ".join(
                    str(value) for value in task.values() if value is not None
                )
                self.assertNotIn("local-test-password", stored)
                self.assertNotIn("stale-folder-id", stored)
                self.assertNotIn("stale-direct-path", stored)
            finally:
                db.close()


class DestinationIdentityTests(unittest.TestCase):
    """The persisted identity is the CD2 endpoint + user for both netdisks."""

    @staticmethod
    def _service(**overrides: object) -> TransferService:
        values: dict[str, object] = {
            "destination_type": DESTINATION_GDRIVE,
            "destination_target": "telegram",
            "cd2_target": "telegram",
            "cd2_url": CD2_URL,
            "cd2_user": CD2_USER,
            "cd2_password": "password-one",
            "gdrive_target": "stale-direct-path",
            "gdrive_root_folder_id": "stale-folder-id",
        }
        values.update(overrides)
        service = TransferService.__new__(TransferService)
        service.settings = SimpleNamespace(**values)
        return service

    def test_identity_is_stable_for_both_destinations(self) -> None:
        google = self._service()
        legacy = self._service(destination_type=DESTINATION_CLOUDDRIVE2)
        self.assertTrue(google._binding()["destination_account"])
        self.assertEqual(
            google._binding()["destination_account"],
            legacy._binding()["destination_account"],
        )

    def test_identity_ignores_the_password_and_trailing_slash(self) -> None:
        original = self._service()._binding()
        service = self._service(cd2_password="password-two")
        self.assertEqual(service._binding(), original)
        service = self._service(cd2_url=f"{CD2_URL}/")
        self.assertEqual(service._binding(), original)

    def test_identity_changes_with_endpoint_or_user(self) -> None:
        original = self._service()._binding()["destination_account"]
        self.assertNotEqual(
            self._service(cd2_url="http://other-nas:19798/dav")
            ._binding()["destination_account"],
            original,
        )
        self.assertNotEqual(
            self._service(cd2_user="another-user")._binding()[
                "destination_account"
            ],
            original,
        )

    def test_webdav_path_case_is_part_of_the_identity(self) -> None:
        settings = SimpleNamespace(
            cd2_url="https://NAS.EXAMPLE/dav/Folder", cd2_user="owner"
        )
        original = clouddrive2_identity(settings)
        settings.cd2_url = "https://nas.example/dav/Folder/"
        self.assertEqual(clouddrive2_identity(settings), original)
        settings.cd2_url = "https://nas.example/dav/folder"
        self.assertNotEqual(clouddrive2_identity(settings), original)

    def test_identity_is_empty_without_endpoint_details(self) -> None:
        self.assertEqual(
            self._service(cd2_url="", cd2_user="")._binding()[
                "destination_account"
            ],
            "",
        )

    def test_identity_never_persists_credentials(self) -> None:
        binding = self._service(
            cd2_password="password-secret", cd2_user="owner-secret"
        )._binding()
        stored = " ".join(binding.values())
        self.assertNotIn("password-secret", stored)
        self.assertNotIn("owner-secret", stored)
        self.assertNotIn("clouddrive2:19798", stored)


class MediaDedupeScopeTests(unittest.TestCase):
    def test_same_media_is_deduped_within_one_destination_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = TaskDB(Path(temp) / "tasks.db")
            try:
                common: dict[str, object] = {
                    "chat_id": 1,
                    "sender_id": 7,
                    "media_key": "document:42",
                    "file_name": "one.bin",
                    "file_size": 1,
                }
                base = {
                    "destination_type": DESTINATION_GDRIVE,
                    "destination_target": "telegram",
                    "destination_root": "",
                    "destination_account": "clouddrive2:one",
                }
                first, created = db.create_task(
                    message_id=1, destination=base, **common
                )
                self.assertTrue(created)
                duplicate, created = db.create_task(
                    message_id=2, destination=base, **common
                )
                self.assertFalse(created)
                self.assertEqual(duplicate["id"], first["id"])

                for offset, other in enumerate(
                    (
                        # 同一个 CD2 上换路径
                        {**base, "destination_target": "another/path"},
                        # 同一个 CD2、同一路径，换最终网盘
                        {
                            **base,
                            "destination_type": DESTINATION_CLOUDDRIVE2,
                        },
                        # 同一个 CD2 换端点账号
                        {**base, "destination_account": "clouddrive2:two"},
                    ),
                    start=3,
                ):
                    task, created = db.create_task(
                        message_id=offset, destination=other, **common
                    )
                    self.assertTrue(
                        created,
                        "a previous destination must not consume the new "
                        f"destination's duplicate key: {other}",
                    )
                    self.assertNotEqual(task["id"], first["id"])
            finally:
                db.close()

    def test_replaying_the_same_message_is_still_deduped(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = TaskDB(Path(temp) / "tasks.db")
            try:
                first, created = db.create_task(
                    chat_id=1,
                    message_id=1,
                    sender_id=7,
                    media_key="document:42",
                    file_name="one.bin",
                    file_size=1,
                    destination={
                        "destination_type": DESTINATION_GDRIVE,
                        "destination_target": "telegram",
                    },
                )
                self.assertTrue(created)
                replay, created = db.create_task(
                    chat_id=1,
                    message_id=1,
                    sender_id=7,
                    media_key="document:42",
                    file_name="one.bin",
                    file_size=1,
                    destination={
                        "destination_type": DESTINATION_GDRIVE,
                        "destination_target": "another/path",
                    },
                )
                self.assertFalse(created)
                self.assertEqual(replay["id"], first["id"])
            finally:
                db.close()

    def test_legacy_media_index_is_replaced_without_losing_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "tasks.db"
            TaskDB(path).close()
            # 还原成旧版本的全局唯一索引，并写入一条旧任务。
            conn = sqlite3.connect(path)
            try:
                conn.execute("DROP INDEX idx_tasks_sender_media_destination")
                conn.execute(
                    "CREATE UNIQUE INDEX idx_tasks_sender_media "
                    "ON tasks(sender_id, media_key) WHERE media_key IS NOT NULL"
                )
                conn.execute(
                    """
                    INSERT INTO tasks (
                        chat_id, message_id, sender_id, media_key, file_name,
                        file_size, destination_type, destination_target,
                        destination_root, destination_account, state,
                        created_at, updated_at
                    ) VALUES (1, 1, 7, 'document:42', 'one.bin', 1, ?, ?, ?, ?, 'queued', 1, 1)
                    """,
                    (DESTINATION_GDRIVE, "telegram", None, "clouddrive2:one"),
                )
                conn.commit()
            finally:
                conn.close()

            db = TaskDB(path)
            try:
                conn = sqlite3.connect(path)
                try:
                    indexes = {
                        row[1]
                        for row in conn.execute("PRAGMA index_list(tasks)")
                    }
                finally:
                    conn.close()
                self.assertNotIn("idx_tasks_sender_media", indexes)
                self.assertIn("idx_tasks_sender_media_destination", indexes)
                self.assertEqual(db.get(1)["media_key"], "document:42")

                same, created = db.create_task(
                    chat_id=1,
                    message_id=2,
                    sender_id=7,
                    media_key="document:42",
                    file_name="one.bin",
                    file_size=1,
                    destination={
                        "destination_type": DESTINATION_GDRIVE,
                        "destination_target": "telegram",
                        "destination_account": "clouddrive2:one",
                    },
                )
                self.assertFalse(created)
                self.assertEqual(same["id"], 1)

                other, created = db.create_task(
                    chat_id=1,
                    message_id=3,
                    sender_id=7,
                    media_key="document:42",
                    file_name="one.bin",
                    file_size=1,
                    destination={
                        "destination_type": DESTINATION_CLOUDDRIVE2,
                        "destination_target": "telegram",
                        "destination_account": "clouddrive2:one",
                    },
                )
                self.assertTrue(created)
                self.assertNotEqual(other["id"], 1)
            finally:
                db.close()


class LegacyAccountAdoptionTests(unittest.TestCase):
    def test_adoption_only_stamps_clouddrive2_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = TaskDB(Path(temp) / "tasks.db")
            try:
                matching, _ = db.create_task(
                    chat_id=1,
                    message_id=1,
                    sender_id=7,
                    file_name="one.bin",
                    file_size=1,
                    destination={
                        "destination_type": DESTINATION_CLOUDDRIVE2,
                        "destination_target": "telegram",
                    },
                )
                other_target, _ = db.create_task(
                    chat_id=1,
                    message_id=2,
                    sender_id=7,
                    file_name="two.bin",
                    file_size=1,
                    destination={
                        "destination_type": DESTINATION_CLOUDDRIVE2,
                        "destination_target": "other/path",
                    },
                )
                other_account, _ = db.create_task(
                    chat_id=1,
                    message_id=3,
                    sender_id=7,
                    file_name="three.bin",
                    file_size=1,
                    destination={
                        "destination_type": DESTINATION_CLOUDDRIVE2,
                        "destination_target": "telegram",
                        "destination_account": "clouddrive2:old",
                    },
                )
                binding = {
                    "destination_type": DESTINATION_CLOUDDRIVE2,
                    "destination_target": "telegram",
                    "destination_account": "clouddrive2:new",
                }
                self.assertEqual(db.adopt_legacy_account(binding), 1)
                self.assertEqual(
                    db.get(matching["id"])["destination_account"],
                    "clouddrive2:new",
                )
                self.assertIsNone(
                    db.get(other_target["id"])["destination_account"]
                )
                self.assertEqual(
                    db.get(other_account["id"])["destination_account"],
                    "clouddrive2:old",
                )
                # 已经带指纹的任务不会再被改写。
                self.assertEqual(db.adopt_legacy_account(binding), 0)
            finally:
                db.close()

    def test_google_binding_never_adopts_legacy_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = TaskDB(Path(temp) / "tasks.db")
            try:
                unbound, _ = db.create_task(
                    chat_id=1,
                    message_id=1,
                    sender_id=7,
                    file_name="old.bin",
                    file_size=1,
                )
                direct, _ = db.create_task(
                    chat_id=1,
                    message_id=2,
                    sender_id=7,
                    file_name="direct.bin",
                    file_size=1,
                    destination={
                        "destination_type": DESTINATION_GDRIVE,
                        "destination_root": "folder-abc",
                        "destination_account": "gdrive:abc",
                    },
                )
                google_binding = {
                    "destination_type": DESTINATION_GDRIVE,
                    "destination_target": "telegram",
                    "destination_root": "",
                    "destination_account": "clouddrive2:new",
                }
                # 未绑定任务属于旧版 CloudDrive2，Google 分支不得认领。
                self.assertEqual(db.bind_unbound_tasks(google_binding), 0)
                self.assertIsNone(db.get(unbound["id"])["destination_type"])
                # 旧直连 Google 任务不得被改成 CD2 绑定。
                self.assertEqual(db.adopt_legacy_account(google_binding), 0)
                self.assertEqual(
                    db.get(direct["id"])["destination_account"], "gdrive:abc"
                )
                self.assertEqual(
                    db.get(direct["id"])["destination_root"], "folder-abc"
                )
                self.assertEqual(
                    db.pending_mismatch_count(google_binding), 2
                )
            finally:
                db.close()


class TransferFlowTests(unittest.TestCase):
    def _prepare(
        self, root: Path, *, size: int = 16, **settings_kwargs: Any
    ):
        settings = make_settings(root, **settings_kwargs)
        db = TaskDB(root / "tasks.db")
        service = make_service(settings, db)
        asyncio.run(service._on_message(message_event(size=size)))
        task_id = int(db.list_recent(1)[0]["id"])
        notifications: list[str] = []

        async def notify(message: str) -> None:
            notifications.append(message)

        service._notify = notify  # type: ignore[method-assign]
        return settings, db, service, task_id, notifications

    def test_google_mode_completes_through_clouddrive2(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = b"google-drive-pay"  # 16 bytes, matches message_event size
            _settings, db, service, task_id, notifications = self._prepare(root)
            try:
                service.client = FakeTelegram(payload)  # type: ignore[assignment]
                service.rclone = FakeRclone()  # type: ignore[assignment]
                asyncio.run(service._download_one(task_id))
                asyncio.run(service._upload_one(task_id))

                task = db.get(task_id)
                self.assertEqual(task["state"], "completed")
                self.assertEqual(task["remote_path"], "影片.mp4")
                self.assertEqual(task["destination_type"], DESTINATION_GDRIVE)
                completion = next(
                    text for text in notifications if "已接收" in text
                )
                self.assertIn("CloudDrive2 已接收", completion)
                self.assertIn("Google Drive 官方端", completion)
                self.assertIn(f"/confirm #{task_id}", completion)
                self.assertNotIn("115", completion)
                for text in notifications:
                    self.assertNotIn("115", text)
                self.assertEqual(
                    service.rclone.files,  # type: ignore[attr-defined]
                    {"影片.mp4": payload},
                )
                self.assertEqual(
                    state_label(task["state"], task["destination_type"]),
                    "CloudDrive2 已接收，Google Drive 官方端待确认",
                )
            finally:
                db.close()

    def test_google_mode_streams_through_clouddrive2(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = b"streamed-video-payload"
            settings, db, service, task_id, notifications = self._prepare(
                root, size=len(payload), target=""
            )
            try:
                db.update(
                    task_id, state="reserved", transfer_mode="stream"
                )
                service.client = FakeTelegram(  # type: ignore[assignment]
                    payload, stream=True
                )
                service.rclone = FakeRclone()  # type: ignore[assignment]
                asyncio.run(service._download_one(task_id))

                task = db.get(task_id)
                self.assertEqual(task["state"], "completed")
                self.assertEqual(task["transfer_mode"], "stream")
                self.assertIsNone(task["local_path"])
                self.assertEqual(
                    service.rclone.files,  # type: ignore[attr-defined]
                    {"影片.mp4": payload},
                )
                completion = next(
                    text for text in notifications if "已接收" in text
                )
                self.assertIn("CloudDrive2 已接收", completion)
                self.assertIn("Google Drive 官方端", completion)
                self.assertEqual(db.used_local_bytes(), 0)
                self.assertEqual(list(settings.download_dir.iterdir()), [])
            finally:
                db.close()

    def test_google_mode_upload_failure_keeps_local_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = b"google-drive-pay"
            _settings, db, service, task_id, notifications = self._prepare(
                root, max_retries=1
            )
            try:
                service.client = FakeTelegram(payload)  # type: ignore[assignment]
                service.rclone = FakeRclone(fail_upload=True)  # type: ignore[assignment]
                asyncio.run(service._download_one(task_id))
                asyncio.run(service._upload_one(task_id))

                task = db.get(task_id)
                self.assertEqual(task["state"], "upload_failed_retained")
                self.assertTrue(Path(task["local_path"]).is_file())
                self.assertIn("上传重试次数", str(task["error"]))
                failure = next(
                    text for text in notifications if "上传失败" in text
                )
                self.assertIn("CloudDrive2", failure)
                self.assertIn("/retry", failure)
                self.assertNotIn("115", failure)
                # 失败任务仍属于 Google Drive 目标，不会被 115 认领。
                self.assertEqual(
                    db.pending_mismatch_count(
                        {
                            **service._binding(),
                            "destination_type": DESTINATION_CLOUDDRIVE2,
                        }
                    ),
                    1,
                )
            finally:
                db.close()

    def test_115_mode_keeps_legacy_completion_wording(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = b"google-drive-pay"
            _settings, db, service, task_id, notifications = self._prepare(
                root, destination_type=DESTINATION_CLOUDDRIVE2
            )
            try:
                service.client = FakeTelegram(payload)  # type: ignore[assignment]
                service.rclone = FakeRclone()  # type: ignore[assignment]
                asyncio.run(service._download_one(task_id))
                asyncio.run(service._upload_one(task_id))

                completion = next(
                    text for text in notifications if "已接收" in text
                )
                self.assertIn("CloudDrive2 已接收", completion)
                self.assertIn("115 官方端", completion)
                self.assertNotIn("Google Drive", completion)
                self.assertEqual(db.get(task_id)["state"], "completed")
            finally:
                db.close()


class ConfirmTests(unittest.TestCase):
    def _completed_task(self, root: Path, destination_type: str):
        settings = make_settings(root, destination_type=destination_type)
        db = TaskDB(root / "tasks.db")
        service = make_service(settings, db)
        asyncio.run(service._on_message(message_event()))
        task_id = int(db.list_recent(1)[0]["id"])
        db.update(task_id, state="completed", remote_path="影片.mp4")
        return db, service, task_id

    def test_google_confirm_is_a_manual_google_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db, service, task_id = self._completed_task(
                Path(temp), DESTINATION_GDRIVE
            )
            try:
                event = SimpleNamespace(reply=AsyncMock())
                asyncio.run(
                    service._command_confirm(event, ["/confirm", str(task_id)])
                )
                reply = event.reply.await_args.args[0]
                self.assertIn("Google Drive 官方端已由你确认", reply)
                self.assertIn("人工确认", reply)
                self.assertIn("Google Drive 官方端接口", reply)
                self.assertNotIn("115", reply)
                self.assertEqual(db.get(task_id)["state"], "confirmed")
            finally:
                db.close()

    def test_115_confirm_keeps_legacy_wording(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db, service, task_id = self._completed_task(
                Path(temp), DESTINATION_CLOUDDRIVE2
            )
            try:
                event = SimpleNamespace(reply=AsyncMock())
                asyncio.run(
                    service._command_confirm(event, ["/confirm", str(task_id)])
                )
                reply = event.reply.await_args.args[0]
                self.assertIn("115 官方端已由你确认", reply)
                self.assertIn("115 官方端接口", reply)
                self.assertNotIn("Google Drive", reply)
            finally:
                db.close()

    def test_google_confirm_refuses_unfinished_transfer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = make_settings(root)
            db = TaskDB(root / "tasks.db")
            try:
                service = make_service(settings, db)
                asyncio.run(service._on_message(message_event()))
                task_id = int(db.list_recent(1)[0]["id"])
                db.update(task_id, state="uploading")
                event = SimpleNamespace(reply=AsyncMock())
                asyncio.run(
                    service._command_confirm(event, ["/confirm", str(task_id)])
                )
                reply = event.reply.await_args.args[0]
                self.assertIn("还不能确认", reply)
                self.assertIn("Google Drive 官方端", reply)
                self.assertEqual(db.get(task_id)["state"], "uploading")
            finally:
                db.close()

    def test_confirm_delivery_and_confirm_115_are_equivalent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = TaskDB(Path(temp) / "tasks.db")
            try:
                task, _ = db.create_task(
                    chat_id=1,
                    message_id=1,
                    sender_id=9,
                    file_name="a.mp4",
                    file_size=10,
                )
                db.update(task["id"], state="completed")
                result, row = db.confirm_delivery(task["id"])
                self.assertEqual(result, "confirmed")
                self.assertEqual(row["state"], "confirmed")
                self.assertEqual(db.confirm_115(task["id"])[0], "already")
            finally:
                db.close()


class DestinationSwitchTests(unittest.TestCase):
    def test_115_task_is_not_scheduled_after_switch_to_google(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = TaskDB(root / "tasks.db")
            try:
                service = make_service(
                    make_settings(root, destination_type=DESTINATION_CLOUDDRIVE2),
                    db,
                )
                asyncio.run(service._on_message(message_event()))
                task_id = int(db.list_recent(1)[0]["id"])

                # 同一个 CD2 端点、同一条路径，只换最终网盘。
                service.settings = make_settings(root)
                service.rclone = FakeRclone()  # type: ignore[assignment]
                asyncio.run(service._start_downloads())
                self.assertEqual(service.download_tasks, {})
                self.assertEqual(
                    db.list_states(("queued",), destination=service._binding()), []
                )
                self.assertEqual(db.pending_mismatch_count(service._binding()), 1)

                asyncio.run(service._download_one(task_id))
                blocked = db.get(task_id)
                self.assertEqual(blocked["state"], "queued")
                self.assertIn("为避免写入错误的网盘已暂停", blocked["wait_reason"])
                self.assertEqual(
                    service.rclone.files, {}  # type: ignore[attr-defined]
                )
            finally:
                db.close()

    def test_google_task_is_not_scheduled_after_switch_to_115(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = TaskDB(root / "tasks.db")
            try:
                service = make_service(make_settings(root), db)
                asyncio.run(service._on_message(message_event()))
                task_id = int(db.list_recent(1)[0]["id"])

                service.settings = make_settings(
                    root, destination_type=DESTINATION_CLOUDDRIVE2
                )
                service.rclone = FakeRclone()  # type: ignore[assignment]
                asyncio.run(service._start_downloads())
                self.assertEqual(service.download_tasks, {})
                asyncio.run(service._download_one(task_id))
                self.assertEqual(db.get(task_id)["state"], "queued")
                self.assertEqual(db.pending_mismatch_count(service._binding()), 1)
            finally:
                db.close()

    def test_same_netdisk_different_cd2_path_is_not_resumed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = TaskDB(root / "tasks.db")
            try:
                service = make_service(make_settings(root, target="telegram"), db)
                asyncio.run(service._on_message(message_event()))
                task_id = int(db.list_recent(1)[0]["id"])

                service.settings = make_settings(root, target="other/path")
                service.rclone = FakeRclone()  # type: ignore[assignment]
                self.assertEqual(
                    db.list_states(("queued",), destination=service._binding()), []
                )
                asyncio.run(service._download_one(task_id))
                self.assertEqual(db.get(task_id)["state"], "queued")
                self.assertIn("为避免写入错误的网盘已暂停", db.get(task_id)["wait_reason"])
            finally:
                db.close()

    def test_different_cd2_endpoint_is_not_resumed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = TaskDB(root / "tasks.db")
            try:
                service = make_service(make_settings(root), db)
                asyncio.run(service._on_message(message_event()))
                task_id = int(db.list_recent(1)[0]["id"])

                service.settings = make_settings(
                    root, cd2_url="http://other-nas:19798/dav"
                )
                self.assertEqual(db.pending_mismatch_count(service._binding()), 1)
                asyncio.run(service._download_one(task_id))
                self.assertEqual(db.get(task_id)["state"], "queued")
            finally:
                db.close()

    def test_two_netdisks_on_one_cd2_transfer_independently(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = TaskDB(root / "tasks.db")
            try:
                service = make_service(
                    make_settings(root, destination_type=DESTINATION_CLOUDDRIVE2),
                    db,
                )
                asyncio.run(service._on_message(message_event()))
                legacy_id = int(db.list_recent(1)[0]["id"])

                service.settings = make_settings(root)
                asyncio.run(
                    service._on_message(message_event(task_id=102, size=16))
                )
                google_id = int(db.list_recent(1)[0]["id"])
                self.assertNotEqual(legacy_id, google_id)

                # 当前是 Google Drive：只调度自己的任务。
                self.assertEqual(
                    [
                        row["id"]
                        for row in db.list_states(
                            ("queued",), destination=service._binding()
                        )
                    ],
                    [google_id],
                )
                self.assertEqual(db.pending_mismatch_count(service._binding()), 1)

                payload = b"google-drive-pay"
                service.client = FakeTelegram(payload)  # type: ignore[assignment]
                service.rclone = FakeRclone()  # type: ignore[assignment]
                asyncio.run(service._download_one(google_id))
                asyncio.run(service._upload_one(google_id))
                self.assertEqual(db.get(google_id)["state"], "completed")
                self.assertEqual(db.get(legacy_id)["state"], "queued")

                # 切回 115：原来的任务继续，Google 任务不再被调度。
                service.settings = make_settings(
                    root, destination_type=DESTINATION_CLOUDDRIVE2
                )
                self.assertEqual(
                    [
                        row["id"]
                        for row in db.list_states(
                            ("queued",), destination=service._binding()
                        )
                    ],
                    [legacy_id],
                )
                self.assertEqual(db.pending_mismatch_count(service._binding()), 0)
            finally:
                db.close()

    def test_upload_waits_when_task_bound_to_another_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cd2 = make_settings(root, destination_type=DESTINATION_CLOUDDRIVE2)
            db = TaskDB(root / "tasks.db")
            try:
                service = make_service(cd2, db)
                asyncio.run(service._on_message(message_event()))
                task_id = int(db.list_recent(1)[0]["id"])
                local = cd2.download_dir / f"{task_id}-影片.mp4"
                local.write_bytes(b"x" * 16)
                db.update(
                    task_id,
                    state="waiting_upload",
                    local_path=str(local),
                    downloaded_bytes=16,
                )

                service.settings = make_settings(root)  # 切到 Google Drive
                service.rclone = FakeRclone()  # type: ignore[assignment]
                asyncio.run(service._start_uploads())
                self.assertEqual(service.upload_tasks, {})

                asyncio.run(service._upload_one(task_id))
                blocked = db.get(task_id)
                self.assertEqual(blocked["state"], "waiting_upload")
                self.assertIn("为避免写入错误的网盘已暂停", blocked["wait_reason"])
                self.assertEqual(
                    service.rclone.files, {}  # type: ignore[attr-defined]
                )
                self.assertTrue(local.is_file())
            finally:
                db.close()

    def test_retry_and_cancel_refuse_cross_destination_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = TaskDB(root / "tasks.db")
            try:
                service = make_service(
                    make_settings(root, destination_type=DESTINATION_CLOUDDRIVE2),
                    db,
                )
                asyncio.run(service._on_message(message_event()))
                task_id = int(db.list_recent(1)[0]["id"])
                db.update(
                    task_id,
                    state="upload_failed_retained",
                    remote_path="影片.mp4",
                )
                service.settings = make_settings(root)
                service.rclone = FakeRclone()  # type: ignore[assignment]

                retry_event = SimpleNamespace(reply=AsyncMock())
                asyncio.run(
                    service._command_retry(retry_event, ["/retry", str(task_id)])
                )
                self.assertIn("不能重试", retry_event.reply.await_args.args[0])
                self.assertEqual(db.get(task_id)["state"], "upload_failed_retained")

                cancel_event = SimpleNamespace(reply=AsyncMock())
                asyncio.run(
                    service._command_cancel(cancel_event, ["/cancel", str(task_id)])
                )
                cancel_reply = cancel_event.reply.await_args.args[0]
                self.assertIn("取消中止", cancel_reply)
                self.assertIn("不会清理远端文件", cancel_reply)
                self.assertEqual(db.get(task_id)["state"], "upload_failed_retained")
                self.assertEqual(
                    service.rclone.removed, []  # type: ignore[attr-defined]
                )
            finally:
                db.close()

    def test_status_reports_other_destination_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = TaskDB(root / "tasks.db")
            try:
                service = make_service(
                    make_settings(root, destination_type=DESTINATION_CLOUDDRIVE2),
                    db,
                )
                asyncio.run(service._on_message(message_event()))
                service.settings = make_settings(root)
                status = service._format_status()
                self.assertIn("目标网盘：Google Drive", status)
                self.assertIn("CloudDrive2 WebDAV：可写", status)
                self.assertIn("Google Drive 官方端：Bot 不自动判断", status)
                self.assertIn("其他目标网盘的未完成任务：1 个", status)
                self.assertNotIn("115", status)
            finally:
                db.close()


class LegacyDirectGdriveTests(unittest.TestCase):
    """旧直连 Google 版本留下的任务不能被悄悄改写成 CD2 任务。"""

    @staticmethod
    def _insert_direct_task(db: TaskDB, message_id: int = 1) -> int:
        task, _ = db.create_task(
            chat_id=1,
            message_id=message_id,
            sender_id=987654321,
            file_name="direct.mp4",
            file_size=16,
            destination={
                "destination_type": DESTINATION_GDRIVE,
                "destination_root": "folder-abc",
                "destination_account": "gdrive:0123456789abcdef",
            },
        )
        return int(task["id"])

    def test_direct_google_task_is_blocked_and_explained(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = TaskDB(root / "tasks.db")
            try:
                task_id = self._insert_direct_task(db)
                service = make_service(make_settings(root), db)
                service.rclone = FakeRclone()  # type: ignore[assignment]

                self.assertEqual(
                    db.list_states(("queued",), destination=service._binding()), []
                )
                self.assertEqual(db.pending_mismatch_count(service._binding()), 1)

                asyncio.run(service._download_one(task_id))
                blocked = db.get(task_id)
                self.assertEqual(blocked["state"], "queued")
                reason = str(blocked["wait_reason"])
                self.assertIn("旧版直连 Google Drive", reason)
                self.assertIn("CloudDrive2", reason)
                self.assertIn("旧版本", reason)
                # 绑定保持原样，没有被改成 CD2。
                self.assertEqual(blocked["destination_root"], "folder-abc")
                self.assertEqual(
                    blocked["destination_account"], "gdrive:0123456789abcdef"
                )
                self.assertEqual(
                    service.rclone.files, {}  # type: ignore[attr-defined]
                )

                retry_event = SimpleNamespace(reply=AsyncMock())
                asyncio.run(
                    service._command_retry(retry_event, ["/retry", str(task_id)])
                )
                self.assertIn("不能重试", retry_event.reply.await_args.args[0])
                self.assertIn(
                    "旧版直连 Google Drive",
                    retry_event.reply.await_args.args[0],
                )
            finally:
                db.close()

    def test_direct_google_task_can_only_be_cancelled_without_remote_delete(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = TaskDB(root / "tasks.db")
            try:
                task_id = self._insert_direct_task(db)
                db.update(task_id, remote_path="direct.mp4")
                service = make_service(make_settings(root), db)
                service.rclone = FakeRclone()  # type: ignore[assignment]

                event = SimpleNamespace(reply=AsyncMock())
                asyncio.run(
                    service._command_cancel(event, ["/cancel", str(task_id)])
                )
                reply = event.reply.await_args.args[0]
                self.assertIn("已取消", reply)
                self.assertIn("没有通过 CloudDrive2 删除", reply)
                self.assertEqual(db.get(task_id)["state"], "cancelled")
                # 绝不通过 CD2 删除可能同名的其它文件。
                self.assertEqual(
                    service.rclone.removed, []  # type: ignore[attr-defined]
                )
            finally:
                db.close()

    def test_direct_google_task_detail_warns_about_the_old_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = TaskDB(root / "tasks.db")
            try:
                task_id = self._insert_direct_task(db)
                service = make_service(make_settings(root), db)
                text = service._format_task(db.get(task_id), verbose=True)
                self.assertIn("旧版直连 Google Drive", text)
                self.assertIn("无法通过 CloudDrive2 继续", text)
            finally:
                db.close()


class RestartCompatibilityTests(unittest.TestCase):
    def test_google_task_survives_restart_and_stays_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = make_settings(root)
            db_path = root / "tasks.db"
            db = TaskDB(db_path)
            service = make_service(settings, db)
            asyncio.run(service._on_message(message_event()))
            task_id = int(db.list_recent(1)[0]["id"])
            local = settings.download_dir / f"{task_id}-影片.mp4"
            local.write_bytes(b"x" * 16)
            db.update(
                task_id,
                state="downloading",
                local_path=str(local),
                downloaded_bytes=16,
            )
            db.close()

            reopened = TaskDB(db_path)
            try:
                recovered = reopened.recover(settings.download_dir)
                self.assertEqual(recovered["waiting_upload"], 1)
                row = reopened.get(task_id)
                self.assertEqual(row["destination_type"], DESTINATION_GDRIVE)
                self.assertIsNone(row["destination_root"])
                service = make_service(settings, reopened)
                listed = reopened.list_states(
                    ("waiting_upload",), destination=service._binding()
                )
                self.assertEqual([item["id"] for item in listed], [task_id])
                self.assertEqual(
                    reopened.pending_mismatch_count(service._binding()), 0
                )
            finally:
                reopened.close()

    def test_legacy_database_upgrade_keeps_queue_running(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db_path = root / "tasks.db"
            db = TaskDB(db_path)
            task, _ = db.create_task(
                chat_id=1,
                message_id=9,
                sender_id=9,
                file_name="old.mp4",
                file_size=MB,
            )
            db.close()

            settings = make_settings(root, destination_type=DESTINATION_CLOUDDRIVE2)
            reopened = TaskDB(db_path)
            try:
                service = make_service(settings, reopened)
                self.assertEqual(
                    reopened.list_states(
                        ("queued",), destination=service._binding()
                    )[0]["id"],
                    task["id"],
                )
                self.assertEqual(
                    reopened.bind_unbound_tasks(service._binding()), 1
                )
                self.assertEqual(
                    reopened.pending_mismatch_count(service._binding()), 0
                )
            finally:
                reopened.close()


class VerifyDestinationTests(unittest.TestCase):
    class FakeRclone:
        def __init__(self, wrong_size: bool = False) -> None:
            self.files: dict[str, bytes] = {}
            self.removed: list[str] = []
            self.prepared = False
            self.wrong_size = wrong_size

        async def prepare_destination(self) -> None:
            self.prepared = True

        async def upload(self, local_path: Path, remote_path: str) -> None:
            self.files[remote_path] = local_path.read_bytes()

        async def remote_size(self, remote_path: str) -> int:
            size = len(self.files[remote_path])
            return size - 1 if self.wrong_size else size

        async def move(self, source: str, destination: str) -> None:
            self.files[destination] = self.files.pop(source)

        async def remove(self, remote_path: str) -> None:
            self.removed.append(remote_path)
            self.files.pop(remote_path, None)

        async def exists(self, remote_path: str) -> bool:
            return remote_path in self.files

    def test_google_verification_only_proves_clouddrive2(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = SimpleNamespace(
                data_dir=root, destination_type=DESTINATION_GDRIVE
            )
            client = self.FakeRclone(wrong_size=True)
            with self.assertRaisesRegex(
                RuntimeError, "CloudDrive2 临时测试文件大小错误"
            ):
                asyncio.run(
                    verify_destination(settings, client)  # type: ignore[arg-type]
                )
            self.assertEqual(client.files, {})
            self.assertEqual(list(root.iterdir()), [])

    def test_115_verification_keeps_legacy_wording(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = SimpleNamespace(data_dir=root)
            client = self.FakeRclone(wrong_size=True)
            with self.assertRaisesRegex(
                RuntimeError, "CloudDrive2 临时测试文件大小错误"
            ):
                asyncio.run(
                    verify_destination(settings, client)  # type: ignore[arg-type]
                )

    def test_google_verification_succeeds_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = SimpleNamespace(
                data_dir=root, destination_type=DESTINATION_GDRIVE
            )
            client = self.FakeRclone()
            remote_path = asyncio.run(
                verify_destination(settings, client)  # type: ignore[arg-type]
            )
            self.assertTrue(remote_path.endswith(".ok"))
            self.assertTrue(client.prepared)
            self.assertEqual(client.files, {})
            self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
