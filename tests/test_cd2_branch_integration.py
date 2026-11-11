"""Contract tests: 115 and Google Drive are two targets behind the same CD2."""
from __future__ import annotations

import asyncio
import base64
import configparser
import os
import sys
import tempfile
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "payload"))
from app.config import Settings
from app.db import TaskDB
from app.main import TransferService
from app.rclone_client import RcloneClient

from installer import InstallerApp


def encoded(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def form_values(destination="clouddrive2", managed="true"):
    return {
        "vps_host": "203.0.113.10", "vps_port": "22", "vps_user": "root",
        "auth_method": "密码", "vps_password": "local-fixture-only",
        "ssh_key_path": "", "ssh_key_passphrase": "", "sudo_password": "",
        "bot_token": "123456:" + "a" * 30, "telegram_api_id": "12345",
        "telegram_api_hash": "a" * 32, "allowed_user_id": "987654321",
        "destination_type": destination, "cd2_url": "http://clouddrive2:19798/dav",
        "cd2_username": "local-webdav-user", "cd2_password": "local-webdav-fixture",
        "cd2_target": "115挂载/Telegram", "gdrive_target": "Google挂载/Telegram",
        "deploy_clouddrive2": managed, "local_budget_gb": "20",
        "min_free_disk_gb": "20", "install_dir": "/opt/tg115", "timezone": "Asia/Shanghai",
    }


def app_for(values):
    class Var:
        def __init__(self, value):
            self.value = value
        def get(self):
            return self.value
        def set(self, value):
            self.value = value
    app = InstallerApp.__new__(InstallerApp)
    app.values = {key: Var(value) for key, value in values.items()}
    return app


def config_for(values):
    app = app_for(values)
    snapshot = app._snapshot()
    app._validate(snapshot)
    return dict(line.split("=", 1) for line in app._build_config(snapshot).splitlines() if "=" in line)


def env_for(root, destination="gdrive", target=None):
    return {
        "DESTINATION_TYPE": destination,
        "TELEGRAM_API_ID": "12345", "TELEGRAM_API_HASH_B64": encoded("a" * 32),
        "BOT_TOKEN_B64": encoded("123456:" + "a" * 30), "ALLOWED_USER_ID": "987654321",
        "CD2_WEBDAV_URL_B64": encoded("http://clouddrive2:19798/dav"),
        "CD2_WEBDAV_USERNAME_B64": encoded("local-webdav-user"),
        "CD2_WEBDAV_PASSWORD_B64": encoded("local-webdav-fixture"),
        "CD2_TARGET_PATH_B64": encoded(target if target is not None else ("Google挂载/Telegram" if destination == "gdrive" else "115挂载/Telegram")),
        "DATA_DIR": str(root / "data"), "DOWNLOAD_DIR": str(root / "downloads"),
        "LOG_DIR": str(root / "logs"), "RCLONE_CONFIG_PATH": str(root / "config/rclone.conf"),
    }


def load_settings(env):
    with patch.dict(os.environ, env, clear=True):
        return Settings.from_env()


def service_for(settings, db):
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
    service.snapshot = None
    service._notify = AsyncMock()
    return service


class Cd2TwoBranchIntegrationTests(unittest.TestCase):
    def test_google_keeps_cd2_and_needs_no_oauth(self):
        config = config_for(form_values("gdrive"))
        self.assertEqual(config["DEPLOY_CLOUDDRIVE2"], "true")
        self.assertEqual(base64.b64decode(config["CD2_WEBDAV_USERNAME_B64"]).decode(), "local-webdav-user")
        self.assertEqual(base64.b64decode(config["CD2_WEBDAV_PASSWORD_B64"]).decode(), "local-webdav-fixture")
        self.assertFalse(any(key.startswith("GDRIVE_") for key in config))

    def test_both_choices_roundtrip_through_cd2_with_their_own_path(self):
        with tempfile.TemporaryDirectory() as temp:
            for kind, path in (("clouddrive2", "115挂载/Telegram"), ("gdrive", "Google挂载/Telegram")):
                with self.subTest(destination=kind):
                    env = env_for(Path(temp) / kind, kind)
                    env.update(config_for(form_values(kind)))
                    settings = load_settings(env)
                    self.assertEqual(settings.cd2_target, path)
                    self.assertEqual(settings.destination_target, path)
                    self.assertEqual(RcloneClient(settings).remote("video.mp4"), f"cd2:{path}/video.mp4")
                    self.assertEqual(settings.destination_label, "115" if kind == "clouddrive2" else "Google Drive")

    def test_external_cd2_setting_is_respected_for_both_choices(self):
        for kind in ("clouddrive2", "gdrive"):
            with self.subTest(destination=kind):
                config = config_for(form_values(kind, "false"))
                self.assertEqual(config["DEPLOY_CLOUDDRIVE2"], "false")
                self.assertEqual(base64.b64decode(config["CD2_WEBDAV_URL_B64"]).decode(), "http://clouddrive2:19798/dav")

    def test_both_choices_validate_cd2_credentials(self):
        for kind in ("clouddrive2", "gdrive"):
            for field in ("cd2_url", "cd2_username", "cd2_password"):
                with self.subTest(destination=kind, field=field):
                    values = form_values(kind)
                    values[field] = ""
                    app = app_for(values)
                    with self.assertRaises(ValueError):
                        app._validate(app._snapshot())

    def test_google_oauth_cannot_replace_cd2_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            env = env_for(Path(temp))
            env.pop("CD2_WEBDAV_USERNAME_B64")
            env["GDRIVE_TOKEN_B64"] = encoded('{"refresh_token":"obsolete-direct-fixture"}')
            with self.assertRaises(RuntimeError) as caught:
                load_settings(env)
            self.assertIn("CD2", str(caught.exception))

    def test_old_direct_google_environment_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            env = env_for(Path(temp))
            for key in tuple(env):
                if key.startswith("CD2_"):
                    env.pop(key)
            env["GDRIVE_TOKEN_B64"] = encoded('{"refresh_token":"obsolete-direct-fixture"}')
            with self.assertRaises(RuntimeError) as caught:
                load_settings(env)
            self.assertIn("CD2", str(caught.exception))

    def test_same_cd2_endpoint_does_not_merge_two_cloud_queues(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = load_settings(env_for(root, "clouddrive2", "same-relative-path"))
            second = load_settings(env_for(root, "gdrive", "same-relative-path"))
            db = TaskDB(root / "tasks.db")
            try:
                first_service = service_for(first, db)
                second_service = service_for(second, db)
                task, _ = db.create_task(chat_id=1, message_id=1, sender_id=1, file_name="file.bin", file_size=1, destination=first_service._binding())
                second_service.rclone = SimpleNamespace(upload=AsyncMock(), remove=AsyncMock())
                db.update(task["id"], state="waiting_upload")
                asyncio.run(second_service._upload_one(task["id"]))
                second_service.rclone.upload.assert_not_awaited()
                self.assertEqual(db.pending_mismatch_count(second_service._binding()), 1)
                self.assertEqual(db.get(task["id"])["state"], "waiting_upload")
                self.assertIn("目标", db.get(task["id"])["wait_reason"])
            finally:
                db.close()

    def test_each_choice_creates_a_cd2_webdav_remote(self):
        with tempfile.TemporaryDirectory() as temp:
            for kind in ("clouddrive2", "gdrive"):
                with self.subTest(destination=kind):
                    settings = load_settings(env_for(Path(temp) / kind, kind))
                    client = RcloneClient(settings)
                    calls = []
                    async def config_cli(*args, _calls=calls, _path=settings.rclone_config_path, **kwargs):
                        _calls.append(args)
                        parser = configparser.ConfigParser(interpolation=None)
                        parser.read(_path, encoding="utf-8")
                        action, name, *rest = args[1:]
                        if not parser.has_section(name):
                            parser.add_section(name)
                        if action == "create":
                            parser.set(name, "type", rest.pop(0))
                        index = 0
                        while index < len(rest):
                            if rest[index].startswith("--"):
                                index += 1
                            else:
                                parser.set(name, rest[index], rest[index + 1])
                                index += 2
                        with _path.open("w", encoding="utf-8") as handle:
                            parser.write(handle)
                        return 0, "", ""
                    client._run = config_cli
                    asyncio.run(client.ensure_config())
                    parser = configparser.ConfigParser(interpolation=None)
                    parser.read(settings.rclone_config_path, encoding="utf-8")
                    self.assertEqual(parser.get("cd2", "type"), "webdav")
                    self.assertEqual(parser.get("cd2", "url"), settings.cd2_url)
                    self.assertFalse(parser.has_section("gdrive"))
                    self.assertTrue(all(call[2] == "cd2" for call in calls))


if __name__ == "__main__":
    unittest.main()
