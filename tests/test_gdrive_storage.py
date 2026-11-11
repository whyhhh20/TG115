"""Storage-layer contract: both destinations are CloudDrive2 WebDAV targets.

115 and Google Drive are two mount points inside CloudDrive2. The storage layer
must therefore build the same ``cd2:`` remote for both, require the same WebDAV
credentials and only differ in the relative target path. Nothing here talks to a
real CloudDrive2 instance.
"""
from __future__ import annotations

import asyncio
import base64
import configparser
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

SOURCE = Path(__file__).resolve().parents[1]
PAYLOAD = SOURCE / "payload"
sys.path.insert(0, str(SOURCE))
sys.path.insert(0, str(PAYLOAD))

from app.config import Settings
from app.rclone_client import RcloneClient, RcloneError

CD2_URL = "http://clouddrive2:19798/dav"


def encoded(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def base_env(root: Path) -> dict[str, str]:
    return {
        "TELEGRAM_API_ID": "12345",
        "TELEGRAM_API_HASH_B64": encoded("a" * 32),
        "BOT_TOKEN_B64": encoded("12345:abcdefghijklmnopqrstuvwxyz"),
        "ALLOWED_USER_ID": "987654321",
        "DATA_DIR": str(root / "data"),
        "DOWNLOAD_DIR": str(root / "downloads"),
        "LOG_DIR": str(root / "logs"),
        "RCLONE_CONFIG_PATH": str(root / "config" / "rclone.conf"),
    }


def cd2_env(root: Path, **overrides: str) -> dict[str, str]:
    env = base_env(root)
    env.update(
        {
            "CD2_WEBDAV_URL_B64": encoded(CD2_URL),
            "CD2_WEBDAV_USERNAME_B64": encoded("cd2-user"),
            "CD2_WEBDAV_PASSWORD_B64": encoded("cd2-secret"),
            "CD2_TARGET_PATH_B64": encoded("115/Telegram"),
        }
    )
    env.update(overrides)
    return env


def gdrive_env(root: Path, **overrides: str) -> dict[str, str]:
    """Google Drive selection: same CD2 credentials, Google relative path."""
    env = cd2_env(root)
    env["DESTINATION_TYPE"] = "gdrive"
    env["CD2_TARGET_PATH_B64"] = encoded("GoogleDrive/Telegram")
    env.update(overrides)
    return env


class SettingsDestinationTests(unittest.TestCase):
    def test_default_destination_is_clouddrive2_and_reads_cd2_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, cd2_env(Path(temp)), clear=True):
                settings = Settings.from_env()
            self.assertEqual(settings.destination_type, "clouddrive2")
            self.assertEqual(settings.destination_label, "115")
            self.assertEqual(settings.destination_target, "115/Telegram")
            self.assertEqual(settings.cd2_target, "115/Telegram")
            self.assertEqual(settings.cd2_url, CD2_URL)
            self.assertEqual(settings.cd2_user, "cd2-user")
            self.assertEqual(
                settings.cd2_password, "cd2-secret"  # pragma: allowlist secret
            )
            # The direct-Google settings are gone, not merely empty.
            self.assertFalse(hasattr(settings, "gdrive_token"))
            self.assertFalse(hasattr(settings, "gdrive_root_folder_id"))
            self.assertTrue(settings.rclone_config_path.parent.is_dir())

    def test_clouddrive2_mode_requires_webdav_credentials(self) -> None:
        for variable in (
            "CD2_WEBDAV_URL_B64",
            "CD2_WEBDAV_USERNAME_B64",
            "CD2_WEBDAV_PASSWORD_B64",
        ):
            with (
                self.subTest(variable=variable),
                tempfile.TemporaryDirectory() as temp,
            ):
                env = cd2_env(Path(temp))
                env.pop(variable)
                with (
                    patch.dict(os.environ, env, clear=True),
                    self.assertRaisesRegex(RuntimeError, variable),
                ):
                    Settings.from_env()

    def test_google_drive_mode_also_requires_webdav_credentials(self) -> None:
        for variable in (
            "CD2_WEBDAV_URL_B64",
            "CD2_WEBDAV_USERNAME_B64",
            "CD2_WEBDAV_PASSWORD_B64",
        ):
            with (
                self.subTest(variable=variable),
                tempfile.TemporaryDirectory() as temp,
            ):
                env = gdrive_env(Path(temp))
                env.pop(variable)
                with (
                    patch.dict(os.environ, env, clear=True),
                    self.assertRaisesRegex(RuntimeError, variable),
                ):
                    Settings.from_env()

    def test_google_drive_mode_uses_the_same_cd2_login(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, gdrive_env(Path(temp)), clear=True):
                settings = Settings.from_env()
            self.assertEqual(settings.destination_type, "gdrive")
            self.assertEqual(settings.destination_label, "Google Drive")
            self.assertEqual(settings.destination_target, "GoogleDrive/Telegram")
            self.assertEqual(settings.cd2_target, "GoogleDrive/Telegram")
            self.assertEqual(settings.cd2_url, CD2_URL)
            self.assertEqual(settings.cd2_user, "cd2-user")
            self.assertEqual(
                settings.cd2_password, "cd2-secret"  # pragma: allowlist secret
            )

    def test_destination_aliases_are_accepted(self) -> None:
        cases = {
            "115": ("clouddrive2", "115"),
            " CloudDrive2 ": ("clouddrive2", "115"),
            "GOOGLE_DRIVE": ("gdrive", "Google Drive"),
            "gdrive": ("gdrive", "Google Drive"),
        }
        for raw, (expected_type, expected_label) in cases.items():
            with (
                self.subTest(raw=raw),
                tempfile.TemporaryDirectory() as temp,
            ):
                env = cd2_env(Path(temp), DESTINATION_TYPE=raw)
                with patch.dict(os.environ, env, clear=True):
                    settings = Settings.from_env()
                self.assertEqual(settings.destination_type, expected_type)
                self.assertEqual(settings.destination_label, expected_label)

    def test_unknown_destination_type_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            env = cd2_env(Path(temp), DESTINATION_TYPE="onedrive")
            with (
                patch.dict(os.environ, env, clear=True),
                self.assertRaisesRegex(RuntimeError, "DESTINATION_TYPE"),
            ):
                Settings.from_env()

    def test_legacy_google_only_config_demands_clouddrive2(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            env = base_env(Path(temp))
            env["DESTINATION_TYPE"] = "gdrive"
            env["GDRIVE_TOKEN_B64"] = encoded('{"refresh_token":"1//legacy"}')
            env["GDRIVE_ROOT_FOLDER_ID_B64"] = encoded("folder_A")
            with (
                patch.dict(os.environ, env, clear=True),
                self.assertRaisesRegex(RuntimeError, "CloudDrive2"),
            ):
                Settings.from_env()

    def test_legacy_google_only_config_is_rejected_without_destination_type(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            env = base_env(Path(temp))
            env["GDRIVE_TOKEN_B64"] = encoded('{"refresh_token":"1//legacy"}')
            with (
                patch.dict(os.environ, env, clear=True),
                self.assertRaisesRegex(RuntimeError, "直连方式已移除"),
            ):
                Settings.from_env()

    def test_google_drive_target_is_normalised_to_relative_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            env = gdrive_env(
                Path(temp), CD2_TARGET_PATH_B64=encoded("  Telegram//Video/  ")
            )
            with patch.dict(os.environ, env, clear=True):
                settings = Settings.from_env()
            self.assertEqual(settings.destination_target, "Telegram/Video")

    def test_blank_target_means_the_webdav_root_itself(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            env = gdrive_env(Path(temp), CD2_TARGET_PATH_B64="")
            with patch.dict(os.environ, env, clear=True):
                settings = Settings.from_env()
            self.assertEqual(settings.destination_target, "")
            self.assertEqual(settings.cd2_target, "")

    def test_unsafe_target_paths_are_rejected_for_both_destinations(self) -> None:
        unsafe = {
            "../escape": r"\.\. 路径片段",
            "Telegram/../../escape": r"\.\. 路径片段",
            "./relative": r"\.\. 路径片段",
            "/absolute/path": "相对路径",
            "Telegram\\Video": "路径分隔符",
            "Telegram/\x07bell": "控制字符",
        }
        for destination in ("clouddrive2", "gdrive"):
            for value, message in unsafe.items():
                with (
                    self.subTest(destination=destination, value=value),
                    tempfile.TemporaryDirectory() as temp,
                ):
                    env = cd2_env(
                        Path(temp),
                        DESTINATION_TYPE=destination,
                        CD2_TARGET_PATH_B64=encoded(value),
                    )
                    with (
                        patch.dict(os.environ, env, clear=True),
                        self.assertRaisesRegex(RuntimeError, message),
                    ):
                        Settings.from_env()

    def test_settings_dataclass_still_accepts_legacy_construction(self) -> None:
        settings = Settings(
            1,
            "hash",
            "token",
            2,
            CD2_URL,
            "user",
            "password",
            "115",
            Path("data"),
            Path("downloads"),
            Path("logs"),
            Path("rclone.conf"),
            1,
            1,
            3.0,
            60.0,
            80.0,
            90.0,
            1024,
            512,
            1,
            0.5,
            3,
            30.0,
        )
        self.assertEqual(settings.destination_type, "clouddrive2")
        self.assertEqual(settings.destination_target, "115")
        self.assertEqual(settings.destination_label, "115")


def cd2_settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "rclone_config_path": Path("rclone.conf"),
        "destination_type": "clouddrive2",
        "destination_target": "115/Telegram",
        "destination_label": "115",
        "cd2_url": CD2_URL,
        "cd2_user": "cd2-user",
        "cd2_password": "cd2-secret",  # pragma: allowlist secret
        "cd2_target": "115/Telegram",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def gdrive_settings(**overrides: object) -> SimpleNamespace:
    values = {
        "destination_type": "gdrive",
        "destination_target": "GoogleDrive/Telegram",
        "destination_label": "Google Drive",
        "cd2_target": "GoogleDrive/Telegram",
    }
    values.update(overrides)
    return cd2_settings(**values)


def apply_rclone_config(path: Path, args: tuple[str, ...]) -> None:
    """Emulate ``rclone config create``/``update`` for the ``cd2`` remote.

    rclone merges the key/value pairs it is given into the section and leaves
    every omitted key untouched. Reproducing that merge keeps these tests real:
    a fake that ignored the arguments could not catch a wrong URL or a stale
    password.
    """
    parser = configparser.ConfigParser(interpolation=None, delimiters=("=",))
    if path.exists():
        parser.read(path, encoding="utf-8")
    section = args[2]
    if not parser.has_section(section):
        parser.add_section(section)
    if args[1] == "create":
        parser.set(section, "type", args[3])
        pairs = args[4:]
    else:
        pairs = args[3:]
    items = [item for item in pairs if not item.startswith("--")]
    for key, value in zip(items[0::2], items[1::2]):
        parser.set(section, key, value)
    with path.open("w", encoding="utf-8") as handle:
        parser.write(handle)


def read_section(path: Path, section: str) -> dict[str, str]:
    parser = configparser.ConfigParser(interpolation=None, delimiters=("=",))
    parser.read(path, encoding="utf-8")
    if not parser.has_section(section):
        return {}
    return dict(parser.items(section))


class SharedCd2RemotePathTests(unittest.TestCase):
    def test_both_destinations_use_the_same_cd2_remote(self) -> None:
        for settings in (cd2_settings(), gdrive_settings()):
            with self.subTest(destination=settings.destination_type):
                client = RcloneClient(settings)
                self.assertEqual(
                    client.remote(), f"cd2:{settings.destination_target}"
                )
                self.assertEqual(
                    client.remote("movie.mp4"),
                    f"cd2:{settings.destination_target}/movie.mp4",
                )
                self.assertEqual(
                    client.remote("/movie.mp4"),
                    f"cd2:{settings.destination_target}/movie.mp4",
                )

    def test_google_drive_never_builds_a_drive_remote(self) -> None:
        client = RcloneClient(gdrive_settings())
        self.assertNotIn("gdrive:", client.remote("movie.mp4"))
        self.assertTrue(client.remote("movie.mp4").startswith("cd2:"))

    def test_blank_target_writes_to_webdav_root_for_both_destinations(self) -> None:
        for settings in (
            cd2_settings(destination_target="", cd2_target=""),
            gdrive_settings(destination_target="", cd2_target=""),
        ):
            with self.subTest(destination=settings.destination_type):
                client = RcloneClient(settings)
                self.assertEqual(client.remote(), "cd2:")
                self.assertEqual(client.remote("movie.mp4"), "cd2:movie.mp4")

    def test_legacy_simple_namespace_without_destination_type_stays_cd2(self) -> None:
        settings = SimpleNamespace(
            rclone_config_path=Path("rclone.conf"),
            cd2_target="115/Telegram",
            cd2_password="password",  # pragma: allowlist secret
        )
        client = RcloneClient(settings)
        self.assertEqual(client.remote("movie.mp4"), "cd2:115/Telegram/movie.mp4")

    def test_unknown_destination_type_fails_closed(self) -> None:
        client = RcloneClient(gdrive_settings(destination_type="dropbox"))
        with self.assertRaisesRegex(RcloneError, "未知的网盘目标类型"):
            client.remote("movie.mp4")

    def test_destination_label_prefers_settings_property(self) -> None:
        self.assertEqual(
            RcloneClient(gdrive_settings()).destination_label(), "Google Drive"
        )
        self.assertEqual(RcloneClient(cd2_settings()).destination_label(), "115")
        legacy = RcloneClient(
            SimpleNamespace(
                rclone_config_path=Path("rclone.conf"),
                cd2_target="",
                cd2_password="password",  # pragma: allowlist secret
            )
        )
        self.assertEqual(legacy.destination_label(), "115")


class SharedCd2ConfigTests(unittest.TestCase):
    def make_client(self, temp: str, **overrides: object) -> tuple[RcloneClient, Path]:
        config_path = Path(temp) / "rclone.conf"
        settings = cd2_settings(rclone_config_path=config_path, **overrides)
        return RcloneClient(settings), config_path

    def test_google_drive_creates_the_cd2_webdav_remote(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            client, config_path = self.make_client(
                temp,
                destination_type="gdrive",
                destination_target="GoogleDrive/Telegram",
                destination_label="Google Drive",
                cd2_target="GoogleDrive/Telegram",
            )
            calls: list[tuple[str, ...]] = []

            async def fake_run(
                *args: str,
                timeout: float | None = None,
                check: bool = True,
            ) -> tuple[int, str, str]:
                calls.append(args)
                apply_rclone_config(config_path, args)
                return 0, "", ""

            client._run = fake_run  # type: ignore[method-assign]
            asyncio.run(client.ensure_config())

            flattened = list(calls[-1])
            self.assertEqual(flattened[0], "config")
            self.assertEqual(flattened[1], "create")
            self.assertEqual(flattened[2], "cd2")
            self.assertEqual(flattened[3], "webdav")
            self.assertEqual(flattened[flattened.index("url") + 1], CD2_URL)
            self.assertEqual(flattened[flattened.index("user") + 1], "cd2-user")
            self.assertEqual(
                flattened[flattened.index("pass") + 1],
                "cd2-secret",  # pragma: allowlist secret
            )
            self.assertIn("--obscure", flattened)
            # No direct-Google remote and no Drive-only flags.
            self.assertNotIn("drive", flattened)
            self.assertNotIn("token", flattened)
            self.assertNotIn("--drive-chunk-size", flattened)

            values = read_section(config_path, "cd2")
            self.assertEqual(values["type"], "webdav")
            self.assertEqual(values["url"], CD2_URL)
            self.assertEqual(values["user"], "cd2-user")
            self.assertEqual(
                values["pass"], "cd2-secret"  # pragma: allowlist secret
            )
            self.assertFalse(read_section(config_path, "gdrive"))
            self.assertTrue(client._config_ready)

    def test_115_creates_the_same_cd2_webdav_remote(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            client, config_path = self.make_client(temp)

            async def fake_run(
                *args: str,
                timeout: float | None = None,
                check: bool = True,
            ) -> tuple[int, str, str]:
                apply_rclone_config(config_path, args)
                return 0, "", ""

            client._run = fake_run  # type: ignore[method-assign]
            asyncio.run(client.ensure_config())

            values = read_section(config_path, "cd2")
            self.assertEqual(values["type"], "webdav")
            self.assertEqual(values["url"], CD2_URL)
            self.assertEqual(values["vendor"], "other")

    def test_existing_config_is_reconciled_before_creating(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            client, config_path = self.make_client(
                temp,
                cd2_url="http://new.example/dav",
                cd2_user="new-user",
                cd2_password="new-password",  # pragma: allowlist secret
            )
            config_path.write_text(
                "[cd2]\ntype = webdav\nurl = http://old.example/dav\nuser = old\n",
                encoding="utf-8",
            )
            calls: list[tuple[str, ...]] = []

            async def fake_run(
                *args: str,
                timeout: float | None = None,
                check: bool = True,
            ) -> tuple[int, str, str]:
                calls.append(args)
                apply_rclone_config(config_path, args)
                return 0, "", ""

            client._run = fake_run  # type: ignore[method-assign]
            asyncio.run(client.ensure_config())

            self.assertEqual(calls[0][1], "update")
            values = read_section(config_path, "cd2")
            self.assertEqual(values["url"], "http://new.example/dav")
            self.assertEqual(values["user"], "new-user")
            self.assertEqual(
                values["pass"], "new-password"  # pragma: allowlist secret
            )

    def test_failed_update_falls_back_to_create(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            client, config_path = self.make_client(temp)
            config_path.write_text("[other]\ntype = local\n", encoding="utf-8")
            calls: list[tuple[str, ...]] = []

            async def fake_run(
                *args: str,
                timeout: float | None = None,
                check: bool = True,
            ) -> tuple[int, str, str]:
                calls.append(args)
                if args[1] == "update":
                    return 1, "", "section not found"
                apply_rclone_config(config_path, args)
                return 0, "", ""

            client._run = fake_run  # type: ignore[method-assign]
            asyncio.run(client.ensure_config())

            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[1][1], "create")
            self.assertEqual(calls[1][2], "cd2")
            self.assertEqual(calls[1][3], "webdav")

    def test_config_is_only_written_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            client, config_path = self.make_client(temp)

            async def fake_run(
                *args: str,
                timeout: float | None = None,
                check: bool = True,
            ) -> tuple[int, str, str]:
                apply_rclone_config(config_path, args)
                return 0, "", ""

            client._run = fake_run  # type: ignore[method-assign]
            asyncio.run(client.ensure_config())
            run = AsyncMock(return_value=(0, "", ""))
            client._run = run  # type: ignore[method-assign]
            asyncio.run(client.ensure_config())
            run.assert_not_awaited()

    def test_webdav_password_is_never_logged(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            client, config_path = self.make_client(
                temp, cd2_password="super-secret-value"  # pragma: allowlist secret
            )

            async def fake_run(
                *args: str,
                timeout: float | None = None,
                check: bool = True,
            ) -> tuple[int, str, str]:
                apply_rclone_config(config_path, args)
                return 0, "", ""

            client._run = fake_run  # type: ignore[method-assign]
            logger = logging.getLogger("tg115.rclone")
            records: list[str] = []

            class Collector(logging.Handler):
                def emit(self, record: logging.LogRecord) -> None:
                    records.append(record.getMessage())

            handler = Collector()
            previous_level = logger.level
            logger.addHandler(handler)
            logger.setLevel(logging.DEBUG)
            try:
                asyncio.run(client.ensure_config())
                client.log.debug(
                    "运行 rclone：%s",
                    " ".join(
                        client._redact(
                            [
                                "rclone",
                                "config",
                                "update",
                                "cd2",
                                "pass",
                                "super-secret-value",
                            ]
                        )
                    ),
                )
            finally:
                logger.removeHandler(handler)
                logger.setLevel(previous_level)

            joined = "\n".join(records)
            self.assertNotIn("super-secret-value", joined)
            self.assertIn("***", joined)


class SharedCd2TransferTests(unittest.TestCase):
    def test_prepare_destination_creates_target_folder_then_probes(self) -> None:
        for settings in (cd2_settings(), gdrive_settings()):
            with self.subTest(destination=settings.destination_type):
                client = RcloneClient(settings)
                run = AsyncMock(return_value=(0, "", ""))
                client._run = run  # type: ignore[method-assign]
                client._config_ready = True
                asyncio.run(client.prepare_destination())
                calls = [list(call.args) for call in run.await_args_list]
                remote = f"cd2:{settings.destination_target}"
                self.assertEqual(calls[0][:2], ["mkdir", remote])
                self.assertEqual(calls[1][:2], ["lsd", remote])
                self.assertIn("--max-depth", calls[1])

    def test_prepare_destination_does_not_mkdir_a_bare_remote_root(self) -> None:
        client = RcloneClient(gdrive_settings(destination_target="", cd2_target=""))
        run = AsyncMock(return_value=(0, "", ""))
        client._run = run  # type: ignore[method-assign]
        client._config_ready = True
        asyncio.run(client.prepare_destination())
        calls = [list(call.args) for call in run.await_args_list]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ["lsd", "cd2:"])

    def test_upload_uses_copyto_without_drive_flags_for_both_destinations(
        self,
    ) -> None:
        for settings in (cd2_settings(), gdrive_settings()):
            with self.subTest(destination=settings.destination_type):
                client = RcloneClient(settings)
                run = AsyncMock(return_value=(0, "", ""))
                client._run = run  # type: ignore[method-assign]
                local = Path("movie.mp4")
                asyncio.run(client.upload(local, "movie.mp4.uploading"))
                flattened = list(run.await_args.args)
                self.assertEqual(flattened[0], "copyto")
                self.assertEqual(flattened[1], str(local))
                self.assertEqual(
                    flattened[2],
                    f"cd2:{settings.destination_target}/movie.mp4.uploading",
                )
                self.assertNotIn("--drive-chunk-size", flattened)
                self.assertNotIn("--drive-upload-cutoff", flattened)

    def test_stream_upload_uses_rcat_on_cd2_for_both_destinations(self) -> None:
        for settings in (cd2_settings(), gdrive_settings()):
            with self.subTest(destination=settings.destination_type):
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

                async def scenario(client=client) -> tuple[tuple[object, ...], bytearray]:
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
                self.assertIn(
                    f"cd2:{settings.destination_target}/large.bin", arguments
                )
                self.assertEqual(arguments[arguments.index("--size") + 1], "7")
                self.assertNotIn("--drive-chunk-size", arguments)
                self.assertEqual(data, b"1234567")

    def test_size_exists_move_and_delete_target_cd2_paths(self) -> None:
        for settings in (cd2_settings(), gdrive_settings()):
            with self.subTest(destination=settings.destination_type):
                client = RcloneClient(settings)
                calls: list[tuple[str, ...]] = []

                async def fake_run(
                    *args: str,
                    timeout: float | None = None,
                    check: bool = True,
                    _calls=calls,
                ) -> tuple[int, str, str]:
                    _calls.append(args)
                    if args[0] == "size":
                        return 0, '{"count":1,"bytes":42}', ""
                    return 0, "", ""

                client._run = fake_run  # type: ignore[method-assign]
                self.assertEqual(asyncio.run(client.remote_size("a.bin")), 42)
                self.assertTrue(asyncio.run(client.exists("a.bin")))
                asyncio.run(client.move("a.bin.uploading", "a.bin"))
                asyncio.run(client.remove("a.bin"))

                root = f"cd2:{settings.destination_target}"
                self.assertEqual(calls[0][:2], ("size", f"{root}/a.bin"))
                self.assertEqual(calls[1][:2], ("lsjson", f"{root}/a.bin"))
                self.assertEqual(
                    calls[2][:3],
                    ("moveto", f"{root}/a.bin.uploading", f"{root}/a.bin"),
                )
                self.assertEqual(calls[3][:2], ("deletefile", f"{root}/a.bin"))

    def test_exists_distinguishes_missing_from_remote_failure(self) -> None:
        client = RcloneClient(gdrive_settings())

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

    def test_healthy_reports_destination_label_on_failure(self) -> None:
        for settings, label in (
            (cd2_settings(), "115"),
            (gdrive_settings(), "Google Drive"),
        ):
            with self.subTest(destination=settings.destination_type):
                client = RcloneClient(settings)
                client._config_ready = True

                async def failing_run(
                    *args: str,
                    timeout: float | None = None,
                    check: bool = True,
                ) -> tuple[int, str, str]:
                    raise RcloneError("rclone 退出码 1：401 unauthorized")

                client._run = failing_run  # type: ignore[method-assign]
                with self.assertLogs("tg115.rclone", level="WARNING") as captured:
                    self.assertFalse(asyncio.run(client.healthy()))
                self.assertIn(label, "\n".join(captured.output))

    def test_stream_failure_surfaces_rclone_stderr(self) -> None:
        client = RcloneClient(gdrive_settings())
        client._config_ready = True

        class Stdin:
            def write(self, data: bytes) -> None:
                return None

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
                self.stderr.feed_data(b"401 Unauthorized")
                self.stderr.feed_eof()
                self.returncode = None

            async def wait(self) -> int:
                self.returncode = 1
                return 1

        async def scenario() -> None:
            process = Process()
            spawn = AsyncMock(return_value=process)
            with patch(
                "app.rclone_client.asyncio.create_subprocess_exec", new=spawn
            ):
                stream = await client.open_upload_stream("large.bin", 3)
                await stream.write(b"abc")
                await stream.finish()

        with self.assertRaisesRegex(RcloneError, "401 Unauthorized"):
            asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
