from __future__ import annotations

# The tests insert the packaged payload path before importing the app.
import asyncio
import base64
import os
import sqlite3
import sys
import tempfile
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

SOURCE = Path(__file__).resolve().parents[1]
PAYLOAD = SOURCE / "payload"
sys.path.insert(0, str(SOURCE))
sys.path.insert(0, str(PAYLOAD))

from app import __version__
from app.config import Settings
from app.db import TaskDB
from app.main import TransferService, safe_file_name, state_label
from app.rclone_client import RcloneClient, RcloneError
from app.resources import AdaptiveWindow, ResourceSnapshot
from app.verify_destination import verify_destination

from installer import (
    APP_VERSION,
    MANAGED_CD2_WEBDAV_URL,
    InstallerApp,
    b64,
    re_safe_remote_stage,
)


def encoded(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def valid_installer_values() -> dict[str, str]:
    return {
        "vps_host": "203.0.113.10",
        "vps_port": "22",
        "vps_user": "root",
        "auth_method": "密码",
        "vps_password": "vps-password",  # pragma: allowlist secret
        "ssh_key_path": "",
        "ssh_key_passphrase": "",
        "sudo_password": "",
        "bot_token": "123456:" + "a" * 30,
        "telegram_api_id": "12345",
        "telegram_api_hash": "a" * 32,
        "allowed_user_id": "987654321",
        "cd2_url": "http://clouddrive2:19798/dav",
        "cd2_username": "user",
        "cd2_password": "password",  # pragma: allowlist secret
        "cd2_target": "",
        "local_budget_gb": "20",
        "min_free_disk_gb": "20",
        "install_dir": "/opt/tg115",
        "timezone": "Asia/Shanghai",
        "deploy_clouddrive2": "true",
    }


class InstallerHelpersTests(unittest.TestCase):
    def test_base64_round_trip(self) -> None:
        value = "p@ss$ word/中文"
        self.assertEqual(base64.b64decode(b64(value)).decode(), value)

    def test_remote_cleanup_path_guard(self) -> None:
        good = "/tmp/tg115-deploy-" + "a" * 32
        self.assertTrue(re_safe_remote_stage(good))
        self.assertFalse(re_safe_remote_stage("/tmp/tg115-deploy-"))
        self.assertFalse(re_safe_remote_stage("/opt/tg115"))
        self.assertFalse(re_safe_remote_stage("/tmp/tg115-deploy-" + "g" * 32))
        self.assertFalse(re_safe_remote_stage("/tmp/tg115-deploy-" + "a" * 31))

    def test_full_validation_rejects_shell_metacharacters_in_install_dir(self) -> None:
        app = InstallerApp.__new__(InstallerApp)
        values = valid_installer_values()
        values["install_dir"] = "/opt/tg115;touch /tmp/pwned"
        with self.assertRaisesRegex(ValueError, "安装目录"):
            app._validate(values)

    def test_full_validation_rejects_non_finite_disk_values(self) -> None:
        app = InstallerApp.__new__(InstallerApp)
        values = valid_installer_values()
        values["local_budget_gb"] = "nan"
        with self.assertRaisesRegex(ValueError, "有限数字"):
            app._validate(values)

    def test_public_http_webdav_is_rejected(self) -> None:
        app = InstallerApp.__new__(InstallerApp)
        values = valid_installer_values()
        values["deploy_clouddrive2"] = "false"
        values["cd2_url"] = "http://webdav.example.com/dav"
        with self.assertRaisesRegex(ValueError, "必须使用 HTTPS"):
            app._validate(values)

    def test_private_http_webdav_is_allowed(self) -> None:
        app = InstallerApp.__new__(InstallerApp)
        values = valid_installer_values()
        values["deploy_clouddrive2"] = "false"
        values["cd2_url"] = "http://192.168.1.10:19798/dav"
        app._validate(values)

    def test_managed_clouddrive_always_uses_internal_webdav_url(self) -> None:
        app = InstallerApp.__new__(InstallerApp)
        values = valid_installer_values()
        values["cd2_url"] = "https://203.0.113.10:19798/dav"
        app._validate(values)
        config = dict(
            line.split("=", 1)
            for line in app._build_config(values).splitlines()
        )
        actual = base64.b64decode(config["CD2_WEBDAV_URL_B64"]).decode()
        self.assertEqual(actual, MANAGED_CD2_WEBDAV_URL)

    def test_blank_webdav_relative_target_is_allowed(self) -> None:
        app = InstallerApp.__new__(InstallerApp)
        values = valid_installer_values()
        values["cd2_target"] = ""
        app._validate(values)
        config = dict(
            line.split("=", 1)
            for line in app._build_config(values).splitlines()
        )
        self.assertEqual(config["CD2_TARGET_PATH_B64"], "")

    def test_malformed_webdav_port_is_rejected(self) -> None:
        app = InstallerApp.__new__(InstallerApp)
        values = valid_installer_values()
        values["deploy_clouddrive2"] = "false"
        values["cd2_url"] = "http://clouddrive2:not-a-port/dav"
        with self.assertRaisesRegex(ValueError, "端口"):
            app._validate(values)

    def test_install_dir_with_dot_segment_is_rejected(self) -> None:
        app = InstallerApp.__new__(InstallerApp)
        values = valid_installer_values()
        values["install_dir"] = "/opt/tg115/./nested"
        with self.assertRaisesRegex(ValueError, "安装目录"):
            app._validate(values)

    def test_deploy_runs_full_validation_before_remote_work(self) -> None:
        app = InstallerApp.__new__(InstallerApp)
        app._snapshot = Mock(return_value=valid_installer_values())
        app._validate = Mock(side_effect=ValueError("full-validation-marker"))
        app._run_worker = lambda action: action()
        with self.assertRaisesRegex(ValueError, "full-validation-marker"):
            app.deploy()
        app._validate.assert_called_once()


class FileNameSafetyTests(unittest.TestCase):
    def test_long_utf8_name_stays_below_filesystem_byte_limit(self) -> None:
        result = safe_file_name("测试视频" * 80 + ".mp4", "fallback.bin")
        self.assertLessEqual(len(result.encode("utf-8")), 180)
        self.assertTrue(result.endswith(".mp4"))
        self.assertFalse(result.encode("utf-8").endswith(b"\xef\xbf\xbd"))


class StatusTextTests(unittest.TestCase):
    def test_queue_and_completed_states_are_user_facing_chinese(self) -> None:
        self.assertEqual(state_label("queued"), "在排队")
        self.assertEqual(
            state_label("completed"),
            "Bot 传输已完成，115 官方端待确认",
        )
        self.assertEqual(state_label("confirmed"), "115 官方端已由你确认")
        self.assertNotIn("正在上传", state_label("completed"))
        self.assertNotIn("后台处理中", state_label("completed"))


class SettingsTests(unittest.TestCase):
    def test_settings_decode_and_create_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = {
                "TELEGRAM_API_ID": "12345",
                "TELEGRAM_API_HASH_B64": encoded("a" * 32),
                "BOT_TOKEN_B64": encoded("12345:abcdefghijklmnopqrstuvwxyz"),
                "ALLOWED_USER_ID": "987654321",
                "CD2_WEBDAV_URL_B64": encoded("http://clouddrive2:19798/dav"),
                "CD2_WEBDAV_USERNAME_B64": encoded("user@example.com"),
                "CD2_WEBDAV_PASSWORD_B64": encoded("secret"),
                "CD2_TARGET_PATH_B64": encoded("115/Telegram"),
                "DATA_DIR": str(root / "data"),
                "DOWNLOAD_DIR": str(root / "downloads"),
                "LOG_DIR": str(root / "logs"),
                "RCLONE_CONFIG_PATH": str(root / "config" / "rclone.conf"),
            }
            with patch.dict(os.environ, env, clear=True):
                settings = Settings.from_env()
            self.assertEqual(settings.api_hash, "a" * 32)
            self.assertEqual(settings.cd2_target, "115/Telegram")
            self.assertEqual(settings.local_budget_bytes, 20 * 1024**3)
            self.assertTrue(settings.download_dir.is_dir())
            self.assertTrue(settings.rclone_config_path.parent.is_dir())

    def test_blank_target_means_webdav_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = {
                "TELEGRAM_API_ID": "12345",
                "TELEGRAM_API_HASH_B64": encoded("a" * 32),
                "BOT_TOKEN_B64": encoded("12345:abcdefghijklmnopqrstuvwxyz"),
                "ALLOWED_USER_ID": "987654321",
                "CD2_WEBDAV_URL_B64": encoded(MANAGED_CD2_WEBDAV_URL),
                "CD2_WEBDAV_USERNAME_B64": encoded("user"),
                "CD2_WEBDAV_PASSWORD_B64": encoded("secret"),
                "CD2_TARGET_PATH_B64": "",
                "DATA_DIR": str(root / "data"),
                "DOWNLOAD_DIR": str(root / "downloads"),
                "LOG_DIR": str(root / "logs"),
                "RCLONE_CONFIG_PATH": str(root / "config" / "rclone.conf"),
            }
            with patch.dict(os.environ, env, clear=True):
                settings = Settings.from_env()
            self.assertEqual(settings.cd2_target, "")

    def test_non_finite_budget_is_rejected_even_if_env_is_edited_manually(
        self,
    ) -> None:
        env = {
            "TELEGRAM_API_ID": "12345",
            "TELEGRAM_API_HASH_B64": encoded("a" * 32),
            "BOT_TOKEN_B64": encoded("12345:abcdefghijklmnopqrstuvwxyz"),
            "ALLOWED_USER_ID": "987654321",
            "CD2_WEBDAV_URL_B64": encoded("http://clouddrive2:19798/dav"),
            "CD2_WEBDAV_USERNAME_B64": encoded("user"),
            "CD2_WEBDAV_PASSWORD_B64": encoded("secret"),
            "CD2_TARGET_PATH_B64": encoded("115/Telegram"),
            "LOCAL_TEMP_BUDGET_GB": "nan",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            self.assertRaisesRegex(RuntimeError, "有限数字"),
        ):
            Settings.from_env()


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = TaskDB(self.root / "tasks.db")

    def tearDown(self) -> None:
        self.db.close()
        self.temp.cleanup()

    def make_task(self, message_id: int, size: int) -> dict:
        task, created = self.db.create_task(
            chat_id=1,
            message_id=message_id,
            sender_id=9,
            file_name=f"{message_id}.bin",
            file_size=size,
        )
        self.assertTrue(created)
        return task

    def test_duplicate_message_returns_original_task(self) -> None:
        first = self.make_task(100, 1000)
        second, created = self.db.create_task(
            chat_id=1,
            message_id=100,
            sender_id=9,
            file_name="changed.bin",
            file_size=9999,
        )
        self.assertFalse(created)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["file_size"], 1000)

    def test_same_telegram_media_id_is_not_downloaded_twice(self) -> None:
        first, created = self.db.create_task(
            chat_id=1,
            message_id=1,
            sender_id=9,
            media_key="document:123456",
            file_name="first.mp4",
            file_size=1000,
        )
        self.assertTrue(created)
        second, created = self.db.create_task(
            chat_id=1,
            message_id=2,
            sender_id=9,
            media_key="document:123456",
            file_name="second.mp4",
            file_size=1000,
        )
        self.assertFalse(created)
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(self.db.counts(), {"queued": 1})

    def test_existing_v1_database_is_migrated_without_losing_tasks(self) -> None:
        self.db.close()
        legacy_path = self.root / "legacy.db"
        connection = sqlite3.connect(legacy_path)
        connection.executescript(
            """
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                sender_id INTEGER NOT NULL,
                file_name TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                state TEXT NOT NULL,
                local_path TEXT,
                remote_path TEXT,
                downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                uploaded_bytes INTEGER NOT NULL DEFAULT 0,
                download_retries INTEGER NOT NULL DEFAULT 0,
                upload_retries INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                wait_reason TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                next_retry_at REAL NOT NULL DEFAULT 0,
                UNIQUE(chat_id, message_id)
            );
            INSERT INTO tasks (
                chat_id, message_id, sender_id, file_name, file_size,
                state, created_at, updated_at
            ) VALUES (1, 1, 9, 'legacy.bin', 10, 'queued', 1, 1);
            """
        )
        connection.commit()
        connection.close()

        migrated = TaskDB(legacy_path)
        columns = {
            row[1]
            for row in migrated._conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        self.assertIn("media_key", columns)
        self.assertIn("transfer_mode", columns)
        self.assertEqual(migrated.get(1)["transfer_mode"], "local")
        self.assertEqual(migrated.get(1)["file_name"], "legacy.bin")
        migrated.close()
        self.db = TaskDB(self.root / "tasks.db")

    def test_atomic_budget_reservation(self) -> None:
        gb = 1024**3
        first = self.make_task(1, 4 * gb)
        second = self.make_task(2, 4 * gb)
        ok, _ = self.db.reserve(
            first["id"],
            budget_bytes=6 * gb,
            current_free_bytes=50 * gb,
            minimum_free_bytes=20 * gb,
        )
        self.assertTrue(ok)
        ok, reason = self.db.reserve(
            second["id"],
            budget_bytes=6 * gb,
            current_free_bytes=50 * gb,
            minimum_free_bytes=20 * gb,
        )
        self.assertFalse(ok)
        self.assertIn("本地临时空间", reason)
        self.assertEqual(self.db.used_local_bytes(), 4 * gb)

    def test_twenty_gb_budget_batches_fifteen_four_gb_tasks(self) -> None:
        gb = 1024**3
        tasks = [self.make_task(index, 4 * gb) for index in range(1, 16)]
        results = [
            self.db.reserve(
                task["id"],
                budget_bytes=20 * gb,
                current_free_bytes=50 * gb,
                minimum_free_bytes=20 * gb,
            )
            for task in tasks
        ]
        self.assertEqual(sum(ok for ok, _ in results), 5)
        self.assertEqual(self.db.counts()["queued"], 10)

    def test_twenty_gb_budget_batches_seven_three_gb_tasks(self) -> None:
        gb = 1024**3
        tasks = [self.make_task(index, 3 * gb) for index in range(1, 8)]
        results = [
            self.db.reserve(
                task["id"],
                budget_bytes=20 * gb,
                current_free_bytes=50 * gb,
                minimum_free_bytes=20 * gb,
            )
            for task in tasks
        ]
        self.assertEqual(sum(ok for ok, _ in results), 6)
        self.assertEqual(self.db.counts()["queued"], 1)

    def test_real_disk_safety_line(self) -> None:
        gb = 1024**3
        task = self.make_task(1, 4 * gb)
        ok, reason = self.db.reserve(
            task["id"],
            budget_bytes=20 * gb,
            current_free_bytes=22 * gb,
            minimum_free_bytes=20 * gb,
        )
        self.assertFalse(ok)
        self.assertIn("磁盘", reason)

    def test_single_file_larger_than_budget_switches_to_stream_mode(self) -> None:
        gb = 1024**3
        task = self.make_task(1, 21 * gb)
        ok, reason = self.db.reserve(
            task["id"],
            budget_bytes=20 * gb,
            current_free_bytes=50 * gb,
            minimum_free_bytes=20 * gb,
        )
        self.assertTrue(ok)
        self.assertIn("流式模式", reason)
        reserved = self.db.get(task["id"])
        self.assertEqual(reserved["state"], "reserved")
        self.assertEqual(reserved["transfer_mode"], "stream")
        self.assertEqual(self.db.used_local_bytes(), 0)

    def test_large_stream_file_waits_at_real_disk_safety_line(self) -> None:
        gb = 1024**3
        task = self.make_task(1, 100 * gb)
        ok, reason = self.db.reserve(
            task["id"],
            budget_bytes=20 * gb,
            current_free_bytes=20 * gb,
            minimum_free_bytes=20 * gb,
        )
        self.assertFalse(ok)
        self.assertIn("磁盘", reason)
        self.assertEqual(self.db.get(task["id"])["transfer_mode"], "local")

    def test_disk_safety_counts_reserved_growth_in_same_cycle(self) -> None:
        gb = 1024**3
        tasks = [self.make_task(index, 4 * gb) for index in range(1, 6)]
        results = [
            self.db.reserve(
                task["id"],
                budget_bytes=20 * gb,
                current_free_bytes=35 * gb,
                minimum_free_bytes=20 * gb,
            )
            for task in tasks
        ]
        self.assertEqual(sum(ok for ok, _ in results), 3)
        self.assertEqual(self.db.used_local_bytes(), 12 * gb)
        self.assertIn("磁盘", results[3][1])

    def test_disk_safety_does_not_double_count_complete_local_file(self) -> None:
        gb = 1024**3
        first = self.make_task(1, 4 * gb)
        second = self.make_task(2, 4 * gb)
        ok, _ = self.db.reserve(
            first["id"],
            budget_bytes=20 * gb,
            current_free_bytes=50 * gb,
            minimum_free_bytes=20 * gb,
        )
        self.assertTrue(ok)
        self.db.update(
            first["id"],
            state="waiting_upload",
            downloaded_bytes=4 * gb,
        )
        ok, _ = self.db.reserve(
            second["id"],
            budget_bytes=20 * gb,
            current_free_bytes=46 * gb,
            minimum_free_bytes=20 * gb,
        )
        self.assertTrue(ok)

    def test_restart_recovery_keeps_complete_local_file(self) -> None:
        task = self.make_task(1, 10)
        local = self.root / "downloads" / "1-file.bin"
        local.parent.mkdir()
        local.write_bytes(b"0123456789")
        self.db.update(
            task["id"], state="uploading", local_path=str(local)
        )
        result = self.db.recover(local.parent)
        recovered = self.db.get(task["id"])
        self.assertEqual(result["waiting_upload"], 1)
        self.assertEqual(recovered["state"], "waiting_upload")
        self.assertEqual(recovered["upload_retries"], 0)

    def test_restart_recovery_requeues_truncated_local_file(self) -> None:
        task = self.make_task(1, 10)
        local = self.root / "downloads" / "1-file.bin"
        local.parent.mkdir()
        local.write_bytes(b"too-short")
        self.db.update(
            task["id"],
            state="uploading",
            local_path=str(local),
            downloaded_bytes=9,
            download_retries=2,
            upload_retries=2,
        )
        result = self.db.recover(local.parent)
        recovered = self.db.get(task["id"])
        self.assertEqual(result["queued"], 1)
        self.assertEqual(recovered["state"], "queued")
        self.assertIsNone(recovered["local_path"])
        self.assertEqual(recovered["downloaded_bytes"], 0)
        self.assertEqual(recovered["download_retries"], 0)
        self.assertEqual(recovered["upload_retries"], 0)

    def test_restart_recovers_file_renamed_before_database_commit(self) -> None:
        task = self.make_task(1, 10)
        downloads = self.root / "downloads"
        downloads.mkdir()
        part = downloads / f"{task['id']}.part"
        final = downloads / f"{task['id']}-{task['file_name']}"
        self.db.update(
            task["id"],
            state="downloading",
            local_path=str(part),
            downloaded_bytes=10,
        )
        final.write_bytes(b"0123456789")

        result = self.db.recover(downloads)
        recovered = self.db.get(task["id"])

        self.assertEqual(result["waiting_upload"], 1)
        self.assertEqual(recovered["state"], "waiting_upload")
        self.assertEqual(recovered["local_path"], str(final))
        self.assertTrue(final.exists())

    def test_missing_retained_file_is_requeued_and_releases_budget(self) -> None:
        gb = 1024**3
        task = self.make_task(1, 8 * gb)
        self.db.update(
            task["id"],
            state="verification_failed_retained",
            local_path=str(self.root / "missing.bin"),
        )
        self.assertEqual(self.db.used_local_bytes(), 8 * gb)

        result = self.db.recover(self.root / "downloads")

        self.assertEqual(result["queued"], 1)
        self.assertEqual(self.db.get(task["id"])["state"], "queued")
        self.assertEqual(self.db.used_local_bytes(), 0)

    def test_only_completed_task_can_be_confirmed_for_115(self) -> None:
        completed = self.make_task(20, 10)
        active = self.make_task(21, 10)
        self.db.update(
            completed["id"],
            state="completed",
            remote_path="video.mp4",
            uploaded_bytes=10,
        )
        self.db.update(active["id"], state="uploading")

        result, confirmed = self.db.confirm_115(completed["id"])
        self.assertEqual(result, "confirmed")
        self.assertEqual(confirmed["state"], "confirmed")
        self.assertEqual(confirmed["remote_path"], "video.mp4")

        result, repeated = self.db.confirm_115(completed["id"])
        self.assertEqual(result, "already")
        self.assertEqual(repeated["state"], "confirmed")

        result, unchanged = self.db.confirm_115(active["id"])
        self.assertEqual(result, "invalid")
        self.assertEqual(unchanged["state"], "uploading")
        self.assertEqual(self.db.get(active["id"])["state"], "uploading")

    def test_restart_recovery_does_not_change_confirmed_task(self) -> None:
        task = self.make_task(30, 10)
        self.db.update(
            task["id"],
            state="completed",
            remote_path="confirmed.mp4",
            uploaded_bytes=10,
        )
        result, _ = self.db.confirm_115(task["id"])
        self.assertEqual(result, "confirmed")

        recovered = self.db.recover(self.root / "downloads")

        self.assertEqual(recovered, {"queued": 0, "waiting_upload": 0, "failed": 0})
        current = self.db.get(task["id"])
        self.assertEqual(current["state"], "confirmed")
        self.assertEqual(current["remote_path"], "confirmed.mp4")


class AdaptiveWindowTests(unittest.TestCase):
    def settings(self) -> Settings:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = {
                "TELEGRAM_API_ID": "1",
                "TELEGRAM_API_HASH_B64": encoded("a" * 32),
                "BOT_TOKEN_B64": encoded("1:" + "a" * 25),
                "ALLOWED_USER_ID": "2",
                "CD2_WEBDAV_URL_B64": encoded("http://cd2/dav"),
                "CD2_WEBDAV_USERNAME_B64": encoded("u"),
                "CD2_WEBDAV_PASSWORD_B64": encoded("p"),
                "CD2_TARGET_PATH_B64": encoded("115/T"),
                "DATA_DIR": str(root / "data"),
                "DOWNLOAD_DIR": str(root / "downloads"),
                "LOG_DIR": str(root / "logs"),
                "RCLONE_CONFIG_PATH": str(root / "rclone.conf"),
            }
            with patch.dict(os.environ, env, clear=True):
                return Settings.from_env()

    @staticmethod
    def snapshot(cpu: float, memory_mb: int = 2048) -> ResourceSnapshot:
        return ResourceSnapshot(
            cpu_percent=cpu,
            memory_available=memory_mb * 1024**2,
            swap_used=0,
            disk_free=30 * 1024**3,
            network_bytes_per_second=100 * 1024**2,
            sampled_at=0,
        )

    def test_ramps_up_and_stops_when_destination_unhealthy(self) -> None:
        window = AdaptiveWindow(self.settings(), "download")
        self.assertEqual(
            window.update(
                self.snapshot(20),
                recent_errors=0,
                destination_healthy=True,
                demand_present=True,
            ),
            2,
        )
        self.assertEqual(
            window.update(
                self.snapshot(20),
                recent_errors=0,
                destination_healthy=False,
                demand_present=True,
            ),
            0,
        )

    def test_sustained_pressure_shrinks_window(self) -> None:
        window = AdaptiveWindow(self.settings(), "upload")
        window.value = 8
        window.update(
            self.snapshot(95),
            recent_errors=0,
            destination_healthy=True,
            demand_present=True,
        )
        reduced = window.update(
            self.snapshot(95),
            recent_errors=0,
            destination_healthy=True,
            demand_present=True,
        )
        self.assertEqual(reduced, 4)

    def test_download_stops_but_upload_continues_at_disk_safety_line(self) -> None:
        settings = self.settings()
        download = AdaptiveWindow(settings, "download")
        upload = AdaptiveWindow(settings, "upload")
        snapshot = self.snapshot(20)
        snapshot = ResourceSnapshot(
            cpu_percent=snapshot.cpu_percent,
            memory_available=snapshot.memory_available,
            swap_used=snapshot.swap_used,
            disk_free=settings.min_free_disk_bytes,
            network_bytes_per_second=snapshot.network_bytes_per_second,
            sampled_at=snapshot.sampled_at,
        )
        self.assertEqual(
            download.update(
                snapshot,
                recent_errors=0,
                destination_healthy=True,
                demand_present=True,
            ),
            0,
        )
        self.assertGreater(
            upload.update(
                snapshot,
                recent_errors=0,
                destination_healthy=True,
                demand_present=True,
            ),
            0,
        )

    def test_idle_service_does_not_preinflate_concurrency_window(self) -> None:
        window = AdaptiveWindow(self.settings(), "download")
        for _ in range(1000):
            window.update(
                self.snapshot(5),
                recent_errors=0,
                destination_healthy=True,
                demand_present=False,
            )
        self.assertEqual(window.value, 1)

    def test_memory_ceiling_has_no_fixed_256_task_cap(self) -> None:
        self.assertEqual(
            AdaptiveWindow._memory_derived_ceiling(16 * 1024**3),
            512,
        )


class RcloneConfigTests(unittest.TestCase):
    def test_blank_target_writes_directly_to_webdav_root(self) -> None:
        settings = SimpleNamespace(
            rclone_config_path=Path("rclone.conf"),
            cd2_target="",
            cd2_password="password",  # pragma: allowlist secret
        )
        client = RcloneClient(settings)
        self.assertEqual(client.remote(), "cd2:")
        self.assertEqual(client.remote("video.mp4"), "cd2:video.mp4")

    def test_existing_config_is_reconciled_with_new_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config_path = Path(temp) / "rclone.conf"
            config_path.write_text(
                "[cd2]\nurl = http://old.example/dav\nuser = old\n",
                encoding="utf-8",
            )
            settings = SimpleNamespace(
                rclone_config_path=config_path,
                cd2_url="http://new.example/dav",
                cd2_user="new-user",
                cd2_password="new-password",  # pragma: allowlist secret
                cd2_target="115/Telegram",
            )
            client = RcloneClient(settings)
            calls: list[tuple[str, ...]] = []

            async def fake_run(
                *args: str,
                timeout: float | None = None,
                check: bool = True,
            ) -> tuple[int, str, str]:
                calls.append(args)
                return 0, "", ""

            client._run = fake_run  # type: ignore[method-assign]
            asyncio.run(client.ensure_config())

            self.assertTrue(calls)
            flattened = [item for call in calls for item in call]
            self.assertIn("update", flattened)
            self.assertIn("http://new.example/dav", flattened)
            self.assertIn("new-user", flattened)
            self.assertIn("new-password", flattened)

    def test_exists_distinguishes_missing_from_remote_failure(self) -> None:
        settings = SimpleNamespace(
            rclone_config_path=Path("rclone.conf"),
            cd2_target="115/Telegram",
            cd2_password="password",  # pragma: allowlist secret
        )
        client = RcloneClient(settings)

        async def missing_run(*args: str, **_: object) -> tuple[int, str, str]:
            return 3, "", "directory not found"

        client._run = missing_run  # type: ignore[method-assign]
        self.assertFalse(asyncio.run(client.exists("missing.mp4")))

        async def missing_file_run(*args: str, **_: object) -> tuple[int, str, str]:
            return 4, "", "file not found"

        client._run = missing_file_run  # type: ignore[method-assign]
        self.assertFalse(asyncio.run(client.exists("missing-file.mp4")))

        async def failed_run(*args: str, **_: object) -> tuple[int, str, str]:
            return 1, "", "connection refused"

        client._run = failed_run  # type: ignore[method-assign]
        with self.assertRaisesRegex(RcloneError, "connection refused"):
            asyncio.run(client.exists("unknown.mp4"))

    def test_stream_upload_uses_rcat_with_exact_size_and_backpressure(self) -> None:
        settings = SimpleNamespace(
            rclone_config_path=Path("rclone.conf"),
            cd2_target="Telegram",
            cd2_password="password",  # pragma: allowlist secret
        )
        client = RcloneClient(settings)
        client._config_ready = True

        class Stdin:
            def __init__(self) -> None:
                self.data = bytearray()

            def write(self, data: bytes) -> None:
                self.data.extend(data)

            async def drain(self) -> None:
                return None

            def close(self) -> None:
                return None

            async def wait_closed(self) -> None:
                return None

        class Process:
            def __init__(self) -> None:
                self.stdin = Stdin()
                self.stdout = asyncio.StreamReader()
                self.stderr = asyncio.StreamReader()
                self.stdout.feed_eof()
                self.stderr.feed_eof()
                self.returncode = None

            async def wait(self) -> int:
                self.returncode = 0
                return 0

        async def scenario() -> tuple[tuple[object, ...], bytearray]:
            process = Process()
            spawn = AsyncMock(return_value=process)
            with patch(
                "app.rclone_client.asyncio.create_subprocess_exec",
                new=spawn,
            ):
                stream = await client.open_upload_stream("large.bin", 7)
                await stream.write(b"123")
                await stream.write(b"4567")
                await stream.finish()
            return spawn.await_args.args, process.stdin.data

        arguments, data = asyncio.run(scenario())
        self.assertIn("rcat", arguments)
        self.assertIn("cd2:Telegram/large.bin", arguments)
        size_index = arguments.index("--size")
        self.assertEqual(arguments[size_index + 1], "7")
        self.assertEqual(data, b"1234567")


class DestinationVerificationTests(unittest.TestCase):
    class FakeRclone:
        def __init__(self, *, wrong_size: bool = False):
            self.files: dict[str, bytes] = {}
            self.prepared = False
            self.wrong_size = wrong_size
            self.removed: list[str] = []

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

    def test_real_destination_verification_writes_moves_checks_and_cleans(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            settings = SimpleNamespace(data_dir=Path(temp))
            client = self.FakeRclone()
            remote_path = asyncio.run(
                verify_destination(settings, client)  # type: ignore[arg-type]
            )
            self.assertTrue(client.prepared)
            self.assertTrue(remote_path.endswith(".ok"))
            self.assertEqual(client.files, {})
            self.assertEqual(client.removed, [remote_path])
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_real_destination_verification_fails_closed_and_cleans(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            settings = SimpleNamespace(data_dir=Path(temp))
            client = self.FakeRclone(wrong_size=True)
            with self.assertRaisesRegex(RuntimeError, "大小错误"):
                asyncio.run(
                    verify_destination(settings, client)  # type: ignore[arg-type]
                )
            self.assertEqual(client.files, {})
            self.assertEqual(list(Path(temp).iterdir()), [])


class FlatDestinationTests(unittest.TestCase):
    class FakeRclone:
        def __init__(self, existing: set[str]):
            self.existing = existing

        async def exists(self, remote_path: str) -> bool:
            return remote_path in self.existing

    def service(self, existing: set[str]) -> TransferService:
        service = TransferService.__new__(TransferService)
        service.rclone = self.FakeRclone(existing)  # type: ignore[assignment]
        return service

    def test_new_file_is_saved_directly_in_configured_target(self) -> None:
        service = self.service(set())
        remote = asyncio.run(
            service._choose_remote_final("video.mp4", task_id=7)
        )
        self.assertEqual(remote, "video.mp4")

    def test_duplicate_name_gets_task_suffix_without_date_folders(self) -> None:
        service = self.service({"video.mp4"})
        remote = asyncio.run(
            service._choose_remote_final("video.mp4", task_id=7)
        )
        self.assertEqual(remote, "video (task-7).mp4")

    def test_repeated_duplicate_gets_incremented_suffix(self) -> None:
        service = self.service({"video.mp4", "video (task-7).mp4"})
        remote = asyncio.run(
            service._choose_remote_final("video.mp4", task_id=7)
        )
        self.assertEqual(remote, "video (task-7-2).mp4")


class UploadRecoveryTests(unittest.TestCase):
    class FakeDB:
        def __init__(self, task: dict[str, object]):
            self.task = task

        def get(self, _: int) -> dict[str, object]:
            return dict(self.task)

        def update(self, _: int, **fields: object) -> None:
            self.task.update(fields)

    @staticmethod
    def service(task: dict[str, object]) -> TransferService:
        service = TransferService.__new__(TransferService)
        service.settings = SimpleNamespace(max_retries=1)
        service.db = UploadRecoveryTests.FakeDB(task)  # type: ignore[assignment]
        service._finalize_lock = asyncio.Lock()
        service.recent_upload_errors = deque(maxlen=50)
        service.log = Mock()

        async def notify(_: str) -> None:
            return None

        service._notify = notify  # type: ignore[method-assign]
        return service

    def test_truncated_file_is_removed_and_automatically_requeued(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            local = Path(temp) / "1-video.mp4"
            local.write_bytes(b"123")
            task: dict[str, object] = {
                "id": 1,
                "file_name": "video.mp4",
                "file_size": 4,
                "local_path": str(local),
                "remote_path": None,
                "upload_retries": 0,
            }
            service = self.service(task)
            asyncio.run(service._upload_one(1))
            self.assertFalse(local.exists())
            self.assertEqual(service.db.task["state"], "queued")
            self.assertIn("自动重新排队", str(service.db.task["wait_reason"]))

    def test_completed_remote_move_is_resumed_without_reupload(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            local = Path(temp) / "1-video.mp4"
            local.write_bytes(b"1234")
            task: dict[str, object] = {
                "id": 1,
                "file_name": "video.mp4",
                "file_size": 4,
                "local_path": str(local),
                "remote_path": "video.mp4",
                "upload_retries": 0,
            }
            service = self.service(task)

            class FakeRclone:
                async def exists(self, _: str) -> bool:
                    return True

                async def remote_size(self, _: str) -> int:
                    return 4

                async def upload(self, *_: object) -> None:
                    raise AssertionError("不应重新上传")

                async def remove(self, _: str) -> None:
                    return None

            service.rclone = FakeRclone()  # type: ignore[assignment]
            asyncio.run(service._upload_one(1))
            self.assertFalse(local.exists())
            self.assertEqual(service.db.task["state"], "completed")
            self.assertEqual(service.db.task["remote_path"], "video.mp4")

    def test_restart_after_local_delete_finishes_cleanup_without_reupload(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            missing_local = Path(temp) / "already-deleted.mp4"
            task: dict[str, object] = {
                "id": 1,
                "state": "cleanup_pending",
                "file_name": "video.mp4",
                "file_size": 4,
                "local_path": str(missing_local),
                "remote_path": "video.mp4",
                "upload_retries": 0,
            }
            service = self.service(task)

            class FakeRclone:
                async def exists(self, _: str) -> bool:
                    return True

                async def remote_size(self, _: str) -> int:
                    return 4

                async def upload(self, *_: object) -> None:
                    raise AssertionError("不应重新上传")

            service.rclone = FakeRclone()  # type: ignore[assignment]
            asyncio.run(service._upload_one(1))
            self.assertEqual(service.db.task["state"], "completed")
            self.assertIsNone(service.db.task["local_path"])

    def test_local_cleanup_failure_is_persisted_and_automatically_retryable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            local = Path(temp) / "1-video.mp4"
            local.write_bytes(b"1234")
            task: dict[str, object] = {
                "id": 1,
                "state": "finalizing",
                "file_name": "video.mp4",
                "file_size": 4,
                "local_path": str(local),
                "remote_path": "video.mp4",
                "upload_retries": 0,
            }
            service = self.service(task)
            with patch.object(Path, "unlink", side_effect=PermissionError("locked")):
                asyncio.run(
                    service._complete_cloud_receive(
                        1, task, local, "video.mp4"
                    )
                )
            self.assertEqual(service.db.task["state"], "cleanup_pending")
            self.assertIn("本地文件暂时无法清理", str(service.db.task["error"]))
            self.assertGreater(float(service.db.task["next_retry_at"]), 0)

    def test_failed_upload_removes_remote_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            local = Path(temp) / "1-video.mp4"
            local.write_bytes(b"1234")
            task: dict[str, object] = {
                "id": 1,
                "file_name": "video.mp4",
                "file_size": 4,
                "local_path": str(local),
                "remote_path": None,
                "upload_retries": 0,
            }
            service = self.service(task)

            class FakeRclone:
                def __init__(self) -> None:
                    self.removed: list[str] = []

                async def upload(self, *_: object) -> None:
                    raise RuntimeError("simulated upload failure")

                async def remove(self, remote: str) -> None:
                    self.removed.append(remote)

            fake = FakeRclone()
            service.rclone = fake  # type: ignore[assignment]
            asyncio.run(service._upload_one(1))
            self.assertEqual(service.db.task["state"], "upload_failed_retained")
            self.assertEqual(fake.removed, [".uploading-1-video.mp4"])
            self.assertTrue(local.exists())


class PayloadTests(unittest.TestCase):
    def test_windows_and_server_versions_match(self) -> None:
        self.assertEqual(APP_VERSION, "1.6.0")
        self.assertEqual(__version__, APP_VERSION)

    def test_required_payload_files_exist(self) -> None:
        for relative in (
            "Dockerfile",
            ".dockerignore",
            "docker-compose.yml",
            "remote_install.sh",
            "repair_clouddrive_network.sh",
            "manage.sh",
            "app/main.py",
            "app/db.py",
            "app/naming.py",
            "app/resources.py",
            "app/rclone_client.py",
            "app/verify_destination.py",
        ):
            self.assertTrue((PAYLOAD / relative).is_file(), relative)

    def test_real_verification_command_is_wired_into_installer(self) -> None:
        installer = (SOURCE / "installer.py").read_text(encoding="utf-8")
        self.assertIn("python -m app.verify_destination", installer)
        self.assertIn("TG115_DESTINATION=OK", installer)

    def test_container_images_are_pinned_and_redeploy_does_not_pull_latest(self) -> None:
        compose = (PAYLOAD / "docker-compose.yml").read_text(encoding="utf-8")
        dockerfile = (PAYLOAD / "Dockerfile").read_text(encoding="utf-8")
        remote_install = (PAYLOAD / "remote_install.sh").read_text(encoding="utf-8")
        manage = (PAYLOAD / "manage.sh").read_text(encoding="utf-8")
        self.assertRegex(
            compose,
            r"cloudnas/clouddrive2@sha256:[0-9a-f]{64}",
        )
        self.assertNotIn("cloudnas/clouddrive2:latest", compose)
        self.assertRegex(
            dockerfile.splitlines()[0],
            r"^FROM python:3\.12-slim-trixie@sha256:[0-9a-f]{64}$",
        )
        self.assertNotIn("build --pull", remote_install)
        self.assertNotIn("build --pull", manage)
        self.assertNotIn("get.docker.com", remote_install)
        self.assertIn("download.docker.com/linux/${ID}", remote_install)

    def test_existing_clouddrive_is_migrated_and_hard_checked(self) -> None:
        installer = (SOURCE / "installer.py").read_text(encoding="utf-8")
        remote_install = (PAYLOAD / "remote_install.sh").read_text(encoding="utf-8")
        repair = (PAYLOAD / "repair_clouddrive_network.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("config_file.write_bytes", installer)
        self.assertIn("sed -i 's/\\r$//'", remote_install)
        self.assertIn("{{.Names}}|{{.Image}}|{{.Ports}}", remote_install)
        self.assertIn("tolower($2) ~ /cloudnas\\/clouddrive2", remote_install)
        self.assertIn("拒绝自动修改", remote_install)
        self.assertNotIn(
            "tolower($0) ~ /clouddrive2/ || $0 ~ /:19798->/",
            remote_install,
        )
        self.assertIn("repair_clouddrive_network.sh", remote_install)
        self.assertLess(
            repair.index("{{.Names}}|{{.Image}}|{{.Ports}}"),
            repair.index("当前配置没有启用由 VPS 管理 CloudDrive2"),
        )
        self.assertIn('DEPLOY_CLOUDDRIVE2="${DEPLOY_CLOUDDRIVE2%$\'\\r\'}"', repair)
        self.assertIn('CD2_NETWORK_MODE="$(', repair)
        self.assertIn("{{.HostConfig.NetworkMode}}", repair)
        self.assertIn('network_mode: " network_mode', repair)
        self.assertIn("http://127.0.0.1:19798/dav", repair)
        self.assertIn('CD2_ENDPOINT_HOST="127.0.0.1"', repair)
        self.assertIn("recover_webdav_credentials", repair)
        self.assertIn("/opt/tg115-backups", repair)
        self.assertIn("保留当前根目录设置", repair)
        self.assertIn("docker network connect --alias clouddrive2 tg115", repair)
        self.assertIn("socket.create_connection((h,19798),5)", repair)
        self.assertIn("python -m app.verify_destination", repair)
        self.assertIn("TG115_REPAIR=SUCCESS", repair)
        self.assertIn('rm -rf -- "$INSTALL_DIR/app"', remote_install)

    def test_bot_container_is_non_root_and_restricted(self) -> None:
        compose = (PAYLOAD / "docker-compose.yml").read_text(encoding="utf-8")
        dockerfile = (PAYLOAD / "Dockerfile").read_text(encoding="utf-8")
        remote_install = (PAYLOAD / "remote_install.sh").read_text(encoding="utf-8")
        self.assertIn('user: "10001:10001"', compose)
        self.assertIn("cap_drop:", compose)
        self.assertIn("no-new-privileges:true", compose)
        self.assertIn("read_only: true", compose)
        self.assertIn("USER 10001:10001", dockerfile)
        self.assertIn("chown -R 10001:10001", remote_install)
        self.assertIn("umask 077", remote_install)
        self.assertIn("install -d -m 700 /opt/tg115-backups", remote_install)

    def test_docker_build_context_excludes_secrets_and_runtime_data(self) -> None:
        dockerignore = (PAYLOAD / ".dockerignore").read_text(encoding="utf-8")
        lines = dockerignore.splitlines()
        # 白名单必须原样、按顺序位于开头：先全部排除，再只放回构建所需文件。
        self.assertEqual(
            lines[:5],
            ["*", "!Dockerfile", "!requirements.txt", "!app/", "!app/**"],
        )
        # 之后只允许追加字节码缓存排除规则（后写的规则优先，才能把它们从 app/ 中剔除），
        # 不能再放回任何别的文件。
        self.assertLessEqual(
            set(lines[5:]), {"**/__pycache__", "**/*.pyc", "**/*.pyo"}
        )
        self.assertNotIn("!.env", dockerignore)
        self.assertNotIn("!downloads", dockerignore)
        self.assertNotIn("!clouddrive", dockerignore)

    def test_remote_installer_defends_install_path_even_without_gui(self) -> None:
        remote_install = (PAYLOAD / "remote_install.sh").read_text(encoding="utf-8")
        self.assertIn(
            "^/opt/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$",
            remote_install,
        )
        self.assertIn("安装目录不能包含 . 或 .. 路径段", remote_install)


if __name__ == "__main__":
    unittest.main()
