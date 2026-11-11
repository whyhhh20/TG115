"""保存文件夹：默认存到 <网盘>/telegram转存，留空或只填网盘根目录时也不会存到根目录；
WebDAV 地址与“CloudDrive2 装在这台 VPS 上”一起放在 ③ 保存位置。"""

from __future__ import annotations

import asyncio
import sys
import tkinter as tk
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_rclone_local_smoke as smoke
from test_gdrive_installer import decode, parse_config, valid_values

from installer import (
    CD2_URL_HINT_CUSTOM,
    CD2_URL_HINT_MANAGED,
    DEFAULT_TARGETS,
    DESTINATION_CLOUDDRIVE2,
    DESTINATION_GDRIVE,
    MANAGED_CD2_WEBDAV_URL,
    InstallerApp,
    destination_target,
    normalize_drive_folder,
)


class NormalizeTests(unittest.TestCase):
    def test_defaults_are_telegram_folders_under_each_drive(self) -> None:
        self.assertEqual(DEFAULT_TARGETS[DESTINATION_CLOUDDRIVE2], "115/telegram转存")
        self.assertEqual(DEFAULT_TARGETS[DESTINATION_GDRIVE], "GoogleDrive/telegram转存")

    def test_blank_uses_the_default_folder(self) -> None:
        for raw in ("", "  ", "/", "//"):
            self.assertEqual(normalize_drive_folder(DESTINATION_GDRIVE, raw), "GoogleDrive/telegram转存")
            self.assertEqual(normalize_drive_folder(DESTINATION_CLOUDDRIVE2, raw), "115/telegram转存")

    def test_drive_root_alone_gets_telegram_folder_appended(self) -> None:
        cases = {
            (DESTINATION_GDRIVE, "GoogleDrive"): "GoogleDrive/telegram转存",
            (DESTINATION_GDRIVE, "/GoogleDrive/"): "GoogleDrive/telegram转存",
            (DESTINATION_GDRIVE, "googledrive"): "googledrive/telegram转存",
            (DESTINATION_CLOUDDRIVE2, "115"): "115/telegram转存",
            (DESTINATION_CLOUDDRIVE2, "115open"): "115open/telegram转存",
        }
        for (destination, raw), expected in cases.items():
            self.assertEqual(normalize_drive_folder(destination, raw), expected, raw)

    def test_explicit_paths_are_kept(self) -> None:
        for raw in ("115/Telegram", "GoogleDrive/电影/2026", "Telegram", "telegram转存", "115open/Telegram"):
            self.assertEqual(normalize_drive_folder(DESTINATION_CLOUDDRIVE2, raw), raw)
        self.assertEqual(normalize_drive_folder(DESTINATION_GDRIVE, "/GoogleDrive/x/"), "GoogleDrive/x")

    def test_config_builder_itself_is_unchanged_for_explicit_blank(self) -> None:
        # 手写的配置字典仍按原样写入（受保护测试要求空值表示 WebDAV 根目录）；
        # “不存根目录”只在部署器界面读取表单时生效。
        values = valid_values(DESTINATION_GDRIVE)
        values["gdrive_target"] = ""
        self.assertEqual(destination_target(values), "")


class GuiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = InstallerApp(self.root)
        self.app._stop_ui_drain()

    def tearDown(self) -> None:
        self.app.busy = False
        self.app._stop_ui_drain()
        self.root.destroy()

    def fill(self, destination: str) -> None:
        for name, value in valid_values(destination).items():
            if name in self.app.values and name not in {"cd2_target", "gdrive_target"}:
                self.app.values[name].set(value)
        self.app.values["destination_type"].set(destination)
        self.root.update()

    def test_snapshot_never_sends_the_drive_root(self) -> None:
        for destination, field, raw, expected in (
            (DESTINATION_GDRIVE, "gdrive_target", "", "GoogleDrive/telegram转存"),
            (DESTINATION_GDRIVE, "gdrive_target", "GoogleDrive", "GoogleDrive/telegram转存"),
            (DESTINATION_CLOUDDRIVE2, "cd2_target", "", "115/telegram转存"),
            (DESTINATION_CLOUDDRIVE2, "cd2_target", "/115/", "115/telegram转存"),
            (DESTINATION_CLOUDDRIVE2, "cd2_target", "115/Telegram", "115/Telegram"),
        ):
            with self.subTest(destination=destination, raw=raw):
                self.fill(destination)
                self.app.values[field].set(raw)
                config = parse_config(self.app._build_config(self.app._snapshot()))
                self.assertEqual(decode(config, "CD2_TARGET_PATH_B64"), expected)
                # 输入框也显示实际使用的文件夹。
                self.assertEqual(self.app.values[field].get(), expected)

    def test_leaving_the_folder_field_shows_the_real_folder(self) -> None:
        self.app.values["gdrive_target"].set("")
        self.app._normalize_target_fields("gdrive_target")
        self.assertEqual(self.app.values["gdrive_target"].get(), "GoogleDrive/telegram转存")

    def test_managed_checkbox_locks_the_internal_address(self) -> None:
        entry = self.app.cd2_url_entry
        self.assertEqual(self.app.values["cd2_url"].get(), MANAGED_CD2_WEBDAV_URL)
        self.assertEqual(str(entry.cget("state")), "readonly")
        self.assertEqual(self.app._cd2_url_hint.cget("text"), CD2_URL_HINT_MANAGED)
        self.app.values["deploy_clouddrive2"].set("false")
        self.assertEqual(str(entry.cget("state")), "normal")
        self.assertEqual(self.app.values["cd2_url"].get(), "")
        self.assertEqual(self.app._cd2_url_hint.cget("text"), CD2_URL_HINT_CUSTOM)
        self.assertIn("http://IP:19798/dav", CD2_URL_HINT_CUSTOM)
        self.app.values["cd2_url"].set("http://192.168.1.10:19798/dav")
        self.app.values["deploy_clouddrive2"].set("true")
        self.assertEqual(self.app.values["cd2_url"].get(), MANAGED_CD2_WEBDAV_URL)
        # 再次取消勾选时恢复用户自己填的地址。
        self.app.values["deploy_clouddrive2"].set("false")
        self.assertEqual(self.app.values["cd2_url"].get(), "http://192.168.1.10:19798/dav")

    def test_custom_address_reaches_the_config(self) -> None:
        self.fill(DESTINATION_GDRIVE)
        self.app.values["deploy_clouddrive2"].set("false")
        self.app.values["cd2_url"].set("http://192.168.1.10:19798/dav")
        snapshot = self.app._snapshot()
        self.app._validate(snapshot)
        config = parse_config(self.app._build_config(snapshot))
        self.assertEqual(decode(config, "CD2_WEBDAV_URL_B64"), "http://192.168.1.10:19798/dav")
        self.assertEqual(config["DEPLOY_CLOUDDRIVE2"], "false")


@unittest.skipUnless(smoke.RCLONE, "set TG115_RCLONE_EXE for real local rclone tests")
class FolderIsCreatedOverWebdavTests(smoke.RealRcloneWebdavTests):
    """真实 rclone + 本地 WebDAV：不存在的 <网盘>/telegram转存 会被自动创建。"""

    test_webdav_remote_is_created_and_used_for_both_destinations = None  # type: ignore[assignment]

    def test_missing_telegram_folder_is_created_before_the_first_upload(self) -> None:
        for destination, target in DEFAULT_TARGETS.items():
            with self.subTest(destination=destination):
                mount = self.storage / target.split("/")[0]
                mount.mkdir(exist_ok=True)
                self.assertFalse((self.storage / target).exists())
                client = self.client(destination, target)

                async def scenario(client=client, destination=destination) -> None:
                    await client.ensure_config()
                    # Bot 每次健康检查（启动后和每 30 秒）都会先执行 prepare_destination。
                    self.assertTrue(await client.healthy())
                    source = self.root / f"first-{destination}.bin"
                    source.write_bytes(b"first-file")
                    await client.upload(source, "first.bin")

                asyncio.run(scenario())
                self.assertTrue((self.storage / target).is_dir())
                self.assertEqual((self.storage / target / "first.bin").read_bytes(), b"first-file")

    def test_destination_check_also_creates_the_folder(self) -> None:
        target = DEFAULT_TARGETS[DESTINATION_GDRIVE]
        client = self.client(DESTINATION_GDRIVE, target)
        settings = client.settings
        asyncio.run(smoke.verify_destination(settings, client))
        self.assertTrue((self.storage / target).is_dir())
        self.assertEqual(list((self.storage / target).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
