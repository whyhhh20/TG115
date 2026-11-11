from __future__ import annotations

import base64
import sys
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))

import installer
from installer import (
    APP_TITLE,
    APP_VERSION,
    DESTINATION_CLOUDDRIVE2,
    DESTINATION_GDRIVE,
    MANAGED_CD2_WEBDAV_URL,
    VPS_PROFILE_LABELS,
    VPS_PROFILE_MINIMUM,
    VPS_PROFILE_PRESETS,
    VPS_PROFILE_STANDARD,
    InstallerApp,
    ScrollableFrame,
    build_deploy_config,
    deploy_success_message,
    destination_target,
    destination_type,
    verify_success_message,
    vps_memory_limits,
    vps_profile_name,
)


def valid_values(destination: str = DESTINATION_CLOUDDRIVE2) -> dict[str, str]:
    """两个分支使用完全相同的 CD2 连接信息，只有相对路径字段不同。"""
    return {
        "destination_type": destination,
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
        "cd2_url": MANAGED_CD2_WEBDAV_URL,
        "cd2_username": "user",
        "cd2_password": "password",  # pragma: allowlist secret
        "cd2_target": "",
        "gdrive_target": "",
        "local_budget_gb": "20",
        "min_free_disk_gb": "20",
        "install_dir": "/opt/tg115",
        "timezone": "Asia/Shanghai",
        "deploy_clouddrive2": "true",
    }


def parse_config(text: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def decode(config: dict[str, str], key: str) -> str:
    return base64.b64decode(config[key]).decode()


def app_for_validation() -> InstallerApp:
    return InstallerApp.__new__(InstallerApp)


class DestinationSelectionTests(unittest.TestCase):
    def test_missing_destination_defaults_to_clouddrive2(self) -> None:
        values = valid_values()
        values.pop("destination_type")
        self.assertEqual(destination_type(values), DESTINATION_CLOUDDRIVE2)
        app = app_for_validation()
        app._validate(values)
        config = parse_config(build_deploy_config(values))
        self.assertEqual(config["DESTINATION_TYPE"], DESTINATION_CLOUDDRIVE2)

    def test_blank_destination_defaults_to_clouddrive2(self) -> None:
        values = valid_values()
        values["destination_type"] = "   "
        self.assertEqual(destination_type(values), DESTINATION_CLOUDDRIVE2)

    def test_unknown_destination_is_rejected(self) -> None:
        values = valid_values()
        values["destination_type"] = "dropbox"
        with self.assertRaisesRegex(ValueError, "clouddrive2 或 gdrive"):
            destination_type(values)
        with self.assertRaisesRegex(ValueError, "clouddrive2 或 gdrive"):
            app_for_validation()._validate(values)

    def test_destination_value_is_case_insensitive(self) -> None:
        values = valid_values()
        values["destination_type"] = "GDrive"
        self.assertEqual(destination_type(values), DESTINATION_GDRIVE)

    def test_legacy_aliases_map_to_the_two_branches(self) -> None:
        for raw, expected in (
            ("115", DESTINATION_CLOUDDRIVE2),
            ("Google_Drive", DESTINATION_GDRIVE),
            ("google_drive", DESTINATION_GDRIVE),
        ):
            values = valid_values()
            values["destination_type"] = raw
            self.assertEqual(destination_type(values), expected)
            config = parse_config(build_deploy_config(values))
            self.assertEqual(config["DESTINATION_TYPE"], expected)

    def test_ui_labels_are_only_115_and_google_drive(self) -> None:
        self.assertEqual(installer.DESTINATION_LABELS[DESTINATION_CLOUDDRIVE2], "115")
        self.assertEqual(
            installer.DESTINATION_LABELS[DESTINATION_GDRIVE], "Google Drive"
        )
        self.assertNotIn("CloudDrive2", installer.DESTINATION_LABELS[DESTINATION_CLOUDDRIVE2])

    def test_both_branches_use_the_cd2_remote(self) -> None:
        self.assertEqual(installer.DESTINATION_REMOTES[DESTINATION_CLOUDDRIVE2], "cd2:")
        self.assertEqual(installer.DESTINATION_REMOTES[DESTINATION_GDRIVE], "cd2:")


class DeployConfigTests(unittest.TestCase):
    def test_115_config_uses_managed_url_and_its_own_path(self) -> None:
        values = valid_values()
        values["cd2_url"] = "https://203.0.113.10:19798/dav"
        values["cd2_target"] = "/115/Telegram/"
        values["gdrive_target"] = "/should/not/leak"
        config = parse_config(build_deploy_config(values))
        self.assertEqual(config["DESTINATION_TYPE"], DESTINATION_CLOUDDRIVE2)
        self.assertEqual(config["DEPLOY_CLOUDDRIVE2"], "true")
        self.assertEqual(decode(config, "CD2_WEBDAV_URL_B64"), MANAGED_CD2_WEBDAV_URL)
        self.assertEqual(decode(config, "CD2_WEBDAV_USERNAME_B64"), "user")
        self.assertEqual(decode(config, "CD2_WEBDAV_PASSWORD_B64"), "password")
        self.assertEqual(decode(config, "CD2_TARGET_PATH_B64"), "115/Telegram")
        self.assertNotIn("should/not/leak", build_deploy_config(values))

    def test_gdrive_config_uses_the_same_cd2_credentials_and_its_own_path(self) -> None:
        values = valid_values(DESTINATION_GDRIVE)
        values["cd2_target"] = "/should/not/leak"
        values["gdrive_target"] = "/Telegram/视频/"
        config = parse_config(build_deploy_config(values))
        self.assertEqual(config["DESTINATION_TYPE"], DESTINATION_GDRIVE)
        self.assertEqual(decode(config, "CD2_WEBDAV_URL_B64"), MANAGED_CD2_WEBDAV_URL)
        self.assertEqual(decode(config, "CD2_WEBDAV_USERNAME_B64"), "user")
        self.assertEqual(decode(config, "CD2_WEBDAV_PASSWORD_B64"), "password")
        self.assertEqual(decode(config, "CD2_TARGET_PATH_B64"), "Telegram/视频")
        self.assertNotIn("should/not/leak", build_deploy_config(values))

    def test_no_gdrive_variables_are_emitted_for_either_branch(self) -> None:
        for destination in (DESTINATION_CLOUDDRIVE2, DESTINATION_GDRIVE):
            text = build_deploy_config(valid_values(destination))
            config = parse_config(text)
            self.assertFalse([key for key in config if key.startswith("GDRIVE_")])
            self.assertNotIn("GDRIVE", text)

    def test_google_branch_does_not_force_clouddrive2_off(self) -> None:
        values = valid_values(DESTINATION_GDRIVE)
        values["deploy_clouddrive2"] = "true"
        config = parse_config(build_deploy_config(values))
        self.assertEqual(config["DEPLOY_CLOUDDRIVE2"], "true")

    def test_container_choice_is_honoured_for_both_branches(self) -> None:
        for destination in (DESTINATION_CLOUDDRIVE2, DESTINATION_GDRIVE):
            values = valid_values(destination)
            values["deploy_clouddrive2"] = "false"
            values["cd2_url"] = "http://192.168.1.10:19798/dav"
            config = parse_config(build_deploy_config(values))
            self.assertEqual(config["DEPLOY_CLOUDDRIVE2"], "false")
            self.assertEqual(
                decode(config, "CD2_WEBDAV_URL_B64"),
                "http://192.168.1.10:19798/dav",
            )

    def test_empty_target_path_means_webdav_root(self) -> None:
        for destination in (DESTINATION_CLOUDDRIVE2, DESTINATION_GDRIVE):
            config = parse_config(build_deploy_config(valid_values(destination)))
            self.assertEqual(decode(config, "CD2_TARGET_PATH_B64"), "")

    def test_config_uses_lf_line_endings_for_bash_sourcing(self) -> None:
        for destination in (DESTINATION_CLOUDDRIVE2, DESTINATION_GDRIVE):
            text = build_deploy_config(valid_values(destination))
            self.assertNotIn("\r", text)
            self.assertTrue(text.endswith("\n"))

    def test_destination_target_reads_the_selected_field(self) -> None:
        values = valid_values()
        values["cd2_target"] = "/115/"
        values["gdrive_target"] = "/gd/"
        self.assertEqual(destination_target(values), "115")
        values["destination_type"] = DESTINATION_GDRIVE
        self.assertEqual(destination_target(values), "gd")


class VpsProfileConfigTests(unittest.TestCase):
    """预设填入推荐的预算和磁盘保留，内存保护阈值也跟随预设。"""

    def test_minimum_profile_emits_2gb_budget_6gb_reserve_and_256_128(self) -> None:
        values = valid_values()
        values["vps_profile"] = VPS_PROFILE_MINIMUM
        values["local_budget_gb"] = "2"
        values["min_free_disk_gb"] = "6"
        config = parse_config(build_deploy_config(values))
        self.assertEqual(config["LOCAL_TEMP_BUDGET_GB"], "2")
        self.assertEqual(config["MIN_FREE_DISK_GB"], "6")
        self.assertEqual(config["MEMORY_SOFT_MIN_MB"], "256")
        self.assertEqual(config["MEMORY_HARD_MIN_MB"], "128")

    def test_minimum_profile_floor_fits_a_921mb_vps(self) -> None:
        """真实 921MB VPS 在 Docker + CD2 常驻后约有 340MB 可用，硬阈值必须低于它。"""
        values = valid_values()
        values["vps_profile"] = VPS_PROFILE_MINIMUM
        soft, hard = (int(x) for x in vps_memory_limits(values))
        self.assertLess(hard, 340)
        self.assertLess(soft, 340)
        self.assertLess(hard, soft)

    def test_standard_profile_emits_20gb_budget_20gb_reserve_and_1024_512(self) -> None:
        values = valid_values()
        values["vps_profile"] = VPS_PROFILE_STANDARD
        values["local_budget_gb"] = "20"
        values["min_free_disk_gb"] = "20"
        config = parse_config(build_deploy_config(values))
        self.assertEqual(config["LOCAL_TEMP_BUDGET_GB"], "20")
        self.assertEqual(config["MIN_FREE_DISK_GB"], "20")
        self.assertEqual(config["MEMORY_SOFT_MIN_MB"], "1024")
        self.assertEqual(config["MEMORY_HARD_MIN_MB"], "512")

    def test_manual_budget_and_reserve_win_over_the_preset(self) -> None:
        """用户在输入框里改过的数字不能被预设覆盖，内存阈值仍跟随预设。"""
        values = valid_values()
        values["vps_profile"] = VPS_PROFILE_MINIMUM
        values["local_budget_gb"] = "5"
        values["min_free_disk_gb"] = "9"
        config = parse_config(build_deploy_config(values))
        self.assertEqual(config["LOCAL_TEMP_BUDGET_GB"], "5")
        self.assertEqual(config["MIN_FREE_DISK_GB"], "9")
        self.assertEqual(config["MEMORY_SOFT_MIN_MB"], "256")
        self.assertEqual(config["MEMORY_HARD_MIN_MB"], "128")
        values["vps_profile"] = VPS_PROFILE_STANDARD
        config = parse_config(build_deploy_config(values))
        self.assertEqual(config["LOCAL_TEMP_BUDGET_GB"], "5")
        self.assertEqual(config["MIN_FREE_DISK_GB"], "9")
        self.assertEqual(config["MEMORY_SOFT_MIN_MB"], "1024")
        self.assertEqual(config["MEMORY_HARD_MIN_MB"], "512")

    def test_missing_profile_keeps_legacy_memory_limits(self) -> None:
        values = valid_values()
        values.pop("vps_profile", None)
        self.assertIsNone(vps_profile_name(values))
        self.assertEqual(vps_memory_limits(values), ("1024", "512"))
        config = parse_config(build_deploy_config(values))
        self.assertEqual(config["MEMORY_SOFT_MIN_MB"], "1024")
        self.assertEqual(config["MEMORY_HARD_MIN_MB"], "512")
        # 预算/保留仍取输入框里的旧值。
        self.assertEqual(config["LOCAL_TEMP_BUDGET_GB"], "20")
        self.assertEqual(config["MIN_FREE_DISK_GB"], "20")
        app_for_validation()._validate(values)

    def test_blank_profile_keeps_legacy_memory_limits(self) -> None:
        values = valid_values()
        values["vps_profile"] = "   "
        self.assertIsNone(vps_profile_name(values))
        config = parse_config(build_deploy_config(values))
        self.assertEqual(config["MEMORY_SOFT_MIN_MB"], "1024")
        self.assertEqual(config["MEMORY_HARD_MIN_MB"], "512")

    def test_unknown_profile_is_rejected(self) -> None:
        values = valid_values()
        values["vps_profile"] = "tiny"
        with self.assertRaisesRegex(ValueError, "minimum 或 standard"):
            vps_profile_name(values)
        with self.assertRaisesRegex(ValueError, "minimum 或 standard"):
            build_deploy_config(values)
        with self.assertRaisesRegex(ValueError, "minimum 或 standard"):
            app_for_validation()._validate(values)

    def test_profile_names_are_case_insensitive(self) -> None:
        values = valid_values()
        values["vps_profile"] = " Minimum "
        self.assertEqual(vps_profile_name(values), VPS_PROFILE_MINIMUM)
        values["vps_profile"] = "STANDARD"
        self.assertEqual(vps_profile_name(values), VPS_PROFILE_STANDARD)

    def test_both_destinations_share_the_same_profile_limits(self) -> None:
        for destination in (DESTINATION_CLOUDDRIVE2, DESTINATION_GDRIVE):
            values = valid_values(destination)
            values["vps_profile"] = VPS_PROFILE_MINIMUM
            values["local_budget_gb"] = "2"
            values["min_free_disk_gb"] = "6"
            config = parse_config(build_deploy_config(values))
            self.assertEqual(config["MEMORY_SOFT_MIN_MB"], "256")
            self.assertEqual(config["MEMORY_HARD_MIN_MB"], "128")

    def test_presets_match_the_agreed_numbers(self) -> None:
        self.assertEqual(
            VPS_PROFILE_PRESETS[VPS_PROFILE_MINIMUM],
            {
                "local_budget_gb": "2",
                "min_free_disk_gb": "6",
            },
        )
        self.assertEqual(
            VPS_PROFILE_PRESETS[VPS_PROFILE_STANDARD],
            {
                "local_budget_gb": "20",
                "min_free_disk_gb": "20",
            },
        )
        self.assertIn("推荐", VPS_PROFILE_LABELS[VPS_PROFILE_MINIMUM])


class ValidationTests(unittest.TestCase):
    def test_both_branches_require_the_same_cd2_credentials(self) -> None:
        for destination in (DESTINATION_CLOUDDRIVE2, DESTINATION_GDRIVE):
            for name, label in (
                ("cd2_url", "WebDAV 地址"),
                ("cd2_username", "WebDAV 用户名"),
                ("cd2_password", "WebDAV 密码"),
            ):
                values = valid_values(destination)
                values[name] = ""
                with self.assertRaisesRegex(ValueError, label):
                    app_for_validation()._validate(values)

    def test_both_branches_reject_public_http(self) -> None:
        for destination in (DESTINATION_CLOUDDRIVE2, DESTINATION_GDRIVE):
            values = valid_values(destination)
            values["deploy_clouddrive2"] = "false"
            values["cd2_url"] = "http://webdav.example.com/dav"
            with self.assertRaisesRegex(ValueError, "必须使用 HTTPS"):
                app_for_validation()._validate(values)

    def test_both_branches_reject_credentials_inside_the_url(self) -> None:
        for destination in (DESTINATION_CLOUDDRIVE2, DESTINATION_GDRIVE):
            values = valid_values(destination)
            values["deploy_clouddrive2"] = "false"
            values["cd2_url"] = "https://user:pass@webdav.example.com/dav"
            with self.assertRaisesRegex(ValueError, "专用输入框"):
                app_for_validation()._validate(values)

    def test_parent_path_segments_are_rejected_for_the_selected_target(self) -> None:
        values = valid_values(DESTINATION_GDRIVE)
        values["gdrive_target"] = "Telegram/../../etc"
        with self.assertRaisesRegex(ValueError, "Google Drive 目标路径不能包含 \\.\\."):
            app_for_validation()._validate(values)
        values = valid_values()
        values["cd2_target"] = "Telegram/../../etc"
        with self.assertRaisesRegex(ValueError, "115 目标路径不能包含 \\.\\."):
            app_for_validation()._validate(values)

    def test_backslash_target_is_rejected_for_the_selected_target(self) -> None:
        for destination, name in (
            (DESTINATION_CLOUDDRIVE2, "cd2_target"),
            (DESTINATION_GDRIVE, "gdrive_target"),
        ):
            values = valid_values(destination)
            values[name] = "Telegram\\video"
            with self.assertRaisesRegex(ValueError, "反斜杠"):
                app_for_validation()._validate(values)

    def test_unselected_target_path_is_not_validated(self) -> None:
        values = valid_values()
        values["gdrive_target"] = "bad/../path"
        app_for_validation()._validate(values)
        values["destination_type"] = DESTINATION_GDRIVE
        values["cd2_target"] = "bad/../path"
        values["gdrive_target"] = ""
        app_for_validation()._validate(values)

    def test_legacy_values_without_gdrive_keys_still_validate(self) -> None:
        values = valid_values()
        for name in ("destination_type", "gdrive_target", "gdrive_token"):
            values.pop(name, None)
        app_for_validation()._validate(values)
        config = parse_config(build_deploy_config(values))
        self.assertEqual(config["DESTINATION_TYPE"], DESTINATION_CLOUDDRIVE2)

    def test_install_dir_guard_applies_to_both_destinations(self) -> None:
        for destination in (DESTINATION_CLOUDDRIVE2, DESTINATION_GDRIVE):
            values = valid_values(destination)
            values["install_dir"] = "/opt/tg115;touch /tmp/pwned"
            with self.assertRaisesRegex(ValueError, "安装目录"):
                app_for_validation()._validate(values)


class MessageTests(unittest.TestCase):
    def test_deploy_message_points_at_clouddrive2_for_both_branches(self) -> None:
        for destination, label in (
            (DESTINATION_CLOUDDRIVE2, "115"),
            (DESTINATION_GDRIVE, "Google Drive"),
        ):
            message = deploy_success_message(destination)
            self.assertIn("CloudDrive2", message)
            self.assertIn(label, message)
            self.assertIn("登录并挂载网盘", message)
            self.assertNotIn("rclone 直传", message)

    def test_verify_message_does_not_claim_official_confirmation(self) -> None:
        for destination, label in (
            (DESTINATION_CLOUDDRIVE2, "115"),
            (DESTINATION_GDRIVE, "Google Drive"),
        ):
            message = verify_success_message(destination)
            self.assertIn("CloudDrive2", message)
            self.assertIn(label, message)
            self.assertIn("不代表", message)
            self.assertIn("官方端", message)
            self.assertNotIn("drive.google.com 查看", message)

    def test_app_title_names_the_shared_clouddrive2_layer(self) -> None:
        self.assertIn("CloudDrive2", APP_TITLE)
        self.assertEqual(APP_VERSION, "1.5.0")


def frame_visible(frame: tk.Frame) -> bool:
    """grid_remove() 之后 grid_info() 为空；withdrawn 窗口下不能用 winfo_ismapped。"""
    return bool(frame.grid_info())


class WithdrawnRootTestCase(unittest.TestCase):
    """所有 UI 测试都用 withdraw() 的 root，绝不显示窗口。"""

    def setUp(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = InstallerApp(self.root)

    def tearDown(self) -> None:
        self.app.busy = False
        try:
            self.app.progress.stop()
        except tk.TclError:
            pass
        self.app._stop_ui_drain()
        self.root.destroy()

    def pump(self) -> None:
        self.root.update()

    def log_text(self) -> str:
        return self.app.log.get("1.0", tk.END)

    def pump_until(self, needle: str, timeout: float = 5.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.root.update()
            text = self.log_text()
            if needle in text:
                return text
            time.sleep(0.01)
        return self.log_text()


class LogRedactionTests(WithdrawnRootTestCase):
    def test_log_redacts_known_secrets(self) -> None:
        self.app._secrets = ["cd2-secret-value"]
        self.app._log("password is cd2-secret-value here")
        text = self.log_text()
        self.assertNotIn("cd2-secret-value", text)
        self.assertIn("***", text)

    def test_snapshot_caches_cd2_password_for_background_threads(self) -> None:
        self.app.values["cd2_password"].set("cd2-password-value")
        self.app.values["bot_token"].set("123456:" + "b" * 30)
        self.app._snapshot()
        self.assertTrue(self.app._secrets)
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                self.app._log("leaking cd2-password-value")
            except BaseException as exc:  # noqa: BLE001 - test probe
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(5)
        self.assertFalse(errors)
        text = self.pump_until("leaking")
        self.assertIn("leaking", text)
        self.assertNotIn("cd2-password-value", text)

    def test_short_sudo_password_is_masked_when_echoed(self) -> None:
        self.app.values["sudo_password"].set("ab1")
        self.app._snapshot()
        self.app._log("ab1")
        self.app._log("[sudo] ab1 echoed")
        self.app._log("tab1e stays readable")
        text = self.log_text()
        self.assertNotIn("\nab1\n", "\n" + text)
        self.assertNotIn(" ab1 ", text)
        self.assertIn("tab1e stays readable", text)
        self.assertEqual(text.count("***"), 2)

    def test_long_sudo_password_is_masked_inside_text(self) -> None:
        self.app.values["sudo_password"].set("sudo-pass-1")
        self.app._snapshot()
        self.app._log("prefixsudo-pass-1suffix")
        self.assertNotIn("sudo-pass-1", self.log_text())

    def test_show_error_redacts_and_uses_main_thread(self) -> None:
        self.app._secrets = ["cd2-secret-value"]
        with patch.object(installer.messagebox, "showerror") as showerror:
            self.app._show_error("失败", RuntimeError("bad cd2-secret-value"))
            self.pump()
        self.assertNotIn("cd2-secret-value", self.log_text())
        showerror.assert_called_once()
        self.assertNotIn("cd2-secret-value", showerror.call_args.args[1])


class UiBuildTests(WithdrawnRootTestCase):
    def test_form_has_only_the_shared_cd2_and_two_target_paths(self) -> None:
        values = self.app.values
        self.assertEqual(values["destination_type"].get(), DESTINATION_CLOUDDRIVE2)
        for name in ("cd2_url", "cd2_username", "cd2_password", "cd2_target"):
            self.assertIn(name, values)
        self.assertIn("gdrive_target", values)
        # 两个网盘默认都存到各自的 telegram转存 文件夹，而不是网盘根目录。
        self.assertEqual(values["gdrive_target"].get(), "GoogleDrive/telegram转存")
        self.assertEqual(values["cd2_target"].get(), "115/telegram转存")
        for removed in (
            "gdrive_token",
            "gdrive_client_id",
            "gdrive_client_secret",
            "gdrive_root_folder_id",
        ):
            self.assertNotIn(removed, values)

    def test_oauth_helpers_are_gone(self) -> None:
        for name in (
            "gdrive_token_json",
            "read_gdrive_token_file",
            "GdriveTokenError",
            "MAX_GDRIVE_TOKEN_BYTES",
        ):
            self.assertFalse(hasattr(installer, name), name)
        self.assertFalse(hasattr(installer.InstallerApp, "import_gdrive_token"))
        self.assertFalse(hasattr(installer.InstallerApp, "check_gdrive_token"))

    def test_form_is_scrollable(self) -> None:
        self.assertIsInstance(self.app.form, ScrollableFrame)
        self.assertTrue(self.app.form.canvas.winfo_exists())
        self.assertTrue(self.app.form.scrollbar.winfo_exists())

    def test_default_state_shows_115_path_and_shared_cd2_controls(self) -> None:
        self.assertTrue(
            frame_visible(self.app._target_frames[DESTINATION_CLOUDDRIVE2])
        )
        self.assertFalse(frame_visible(self.app._target_frames[DESTINATION_GDRIVE]))
        self.assertEqual(self.app.deploy_button.cget("text"), "一键部署（115）")
        self.assertEqual(self.app.verify_button.cget("text"), "检查是否可用")
        self.assertEqual(str(self.app.cloud_button.cget("state")), "normal")
        self.assertEqual(str(self.app.repair_button.cget("state")), "normal")
        self.assertEqual(self.app.values["deploy_clouddrive2"].get(), "true")
        self.assertEqual(str(self.app.cd2_checkbox.cget("state")), "normal")

    def test_switching_to_google_keeps_the_shared_cd2_layer_enabled(self) -> None:
        self.app.values["destination_type"].set(DESTINATION_GDRIVE)
        self.pump()
        self.assertTrue(frame_visible(self.app._target_frames[DESTINATION_GDRIVE]))
        self.assertFalse(
            frame_visible(self.app._target_frames[DESTINATION_CLOUDDRIVE2])
        )
        self.assertEqual(self.app.deploy_button.cget("text"), "一键部署（Google Drive）")
        self.assertEqual(self.app.verify_button.cget("text"), "检查是否可用")
        self.assertEqual(str(self.app.cloud_button.cget("state")), "normal")
        self.assertEqual(str(self.app.repair_button.cget("state")), "normal")
        self.assertEqual(self.app.values["deploy_clouddrive2"].get(), "true")
        self.assertEqual(str(self.app.cd2_checkbox.cget("state")), "normal")
        self.assertEqual(self.app.badge.cget("text"), "当前目标：Google Drive")

    def test_switching_back_restores_the_115_path_and_cd2_controls(self) -> None:
        self.app.values["destination_type"].set(DESTINATION_GDRIVE)
        self.pump()
        self.app.values["destination_type"].set(DESTINATION_CLOUDDRIVE2)
        self.pump()
        self.assertTrue(
            frame_visible(self.app._target_frames[DESTINATION_CLOUDDRIVE2])
        )
        self.assertEqual(str(self.app.cloud_button.cget("state")), "normal")
        self.assertEqual(self.app.values["deploy_clouddrive2"].get(), "true")
        self.assertEqual(str(self.app.cd2_checkbox.cget("state")), "normal")

    def test_each_destination_remembers_its_own_relative_path(self) -> None:
        self.app.values["cd2_target"].set("/115/Telegram/")
        self.app.values["destination_type"].set(DESTINATION_GDRIVE)
        self.pump()
        self.app.values["gdrive_target"].set("/gd/Telegram/")
        self.app.values["destination_type"].set(DESTINATION_CLOUDDRIVE2)
        self.pump()
        self.assertEqual(self.app.values["cd2_target"].get(), "/115/Telegram/")
        self.assertEqual(self.app.values["gdrive_target"].get(), "/gd/Telegram/")

    def test_user_disabled_container_choice_survives_a_destination_round_trip(
        self,
    ) -> None:
        self.app.values["deploy_clouddrive2"].set("false")
        self.app.values["destination_type"].set(DESTINATION_GDRIVE)
        self.pump()
        self.assertEqual(self.app.values["deploy_clouddrive2"].get(), "false")
        self.assertEqual(str(self.app.cd2_checkbox.cget("state")), "normal")
        self.app.values["destination_type"].set(DESTINATION_CLOUDDRIVE2)
        self.pump()
        self.assertEqual(self.app.values["deploy_clouddrive2"].get(), "false")

    def test_snapshot_builds_cd2_config_from_the_form_for_google(self) -> None:
        for name, value in valid_values(DESTINATION_GDRIVE).items():
            if name in self.app.values:
                self.app.values[name].set(value)
        self.app.values["destination_type"].set(DESTINATION_GDRIVE)
        self.app.values["gdrive_target"].set("/Telegram/")
        self.app.values["cd2_target"].set("/115/")
        self.pump()
        snapshot = self.app._snapshot()
        config = parse_config(self.app._build_config(snapshot))
        self.assertEqual(config["DESTINATION_TYPE"], DESTINATION_GDRIVE)
        self.assertEqual(config["DEPLOY_CLOUDDRIVE2"], "true")
        self.assertEqual(decode(config, "CD2_TARGET_PATH_B64"), "Telegram")
        self.assertEqual(decode(config, "CD2_WEBDAV_PASSWORD_B64"), "password")
        self.assertFalse([key for key in config if key.startswith("GDRIVE_")])

    def test_busy_state_disables_buttons_and_keeps_cd2_actions(self) -> None:
        self.app._set_busy(True)
        self.pump()
        for button in (
            self.app.deploy_button,
            self.app.verify_button,
            self.app.cloud_button,
            self.app.repair_button,
        ):
            self.assertEqual(str(button.cget("state")), "disabled")
        self.app._set_busy(False)
        self.pump()
        self.assertEqual(str(self.app.deploy_button.cget("state")), "normal")
        self.app.values["destination_type"].set(DESTINATION_GDRIVE)
        self.pump()
        self.assertEqual(str(self.app.deploy_button.cget("state")), "normal")
        self.assertEqual(str(self.app.cloud_button.cget("state")), "normal")

    def test_worker_thread_log_is_marshalled_to_the_main_thread(self) -> None:
        def worker() -> None:
            self.app._log("后台线程消息")

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(5)
        self.assertIn("后台线程消息", self.pump_until("后台线程消息"))

    def test_worker_failure_is_reported_without_touching_tk_directly(self) -> None:
        def boom() -> None:
            raise RuntimeError("worker-boom")

        with patch.object(installer.messagebox, "showerror") as showerror:
            self.app._run_worker(boom)
            text = self.pump_until("worker-boom")
            self.pump()
        self.assertIn("worker-boom", text)
        showerror.assert_called_once()
        self.pump()
        self.assertEqual(str(self.app.deploy_button.cget("state")), "normal")

    def test_clouddrive2_actions_are_not_blocked_on_the_google_branch(self) -> None:
        """两个分支都能进入 CD2 管理页/修复流程：用探针替换会话，不发真实请求。"""
        for name, value in valid_values(DESTINATION_GDRIVE).items():
            if name in self.app.values:
                self.app.values[name].set(value)
        self.app.values["destination_type"].set(DESTINATION_GDRIVE)
        self.pump()

        def probe(_values: dict[str, str]) -> object:
            raise RuntimeError("session-probe")

        with (
            patch.object(self.app, "_new_session", side_effect=probe),
            patch.object(installer.messagebox, "showerror"),
        ):
            self.app.open_clouddrive()
            self.pump_until("session-probe")
            deadline = time.monotonic() + 5.0
            while self.app.busy and time.monotonic() < deadline:
                self.pump()
                time.sleep(0.01)
            self.app.repair_clouddrive()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                self.pump()
                if not self.app.busy and self.log_text().count("session-probe") >= 2:
                    break
                time.sleep(0.01)
            text = self.log_text()
        self.assertEqual(text.count("session-probe"), 2)
        self.assertNotIn("不适用", text)


class VpsProfileUiTests(WithdrawnRootTestCase):
    """预设下拉框：默认最小配置，切换写入预算/保留值，之后可手工改。"""

    def test_default_profile_is_the_minimum_one(self) -> None:
        values = self.app.values
        self.assertEqual(values["vps_profile"].get(), VPS_PROFILE_MINIMUM)
        self.assertEqual(
            self.app.vps_profile_combo.get(), VPS_PROFILE_LABELS[VPS_PROFILE_MINIMUM]
        )
        self.assertEqual(str(self.app.vps_profile_combo.cget("state")), "readonly")
        self.assertEqual(values["local_budget_gb"].get(), "2")
        self.assertEqual(values["min_free_disk_gb"].get(), "6")

    def test_switching_profile_writes_budget_and_reserve(self) -> None:
        self.app.vps_profile_combo.set(VPS_PROFILE_LABELS[VPS_PROFILE_STANDARD])
        self.pump()
        self.assertEqual(self.app.values["vps_profile"].get(), VPS_PROFILE_STANDARD)
        self.assertEqual(self.app.values["local_budget_gb"].get(), "20")
        self.assertEqual(self.app.values["min_free_disk_gb"].get(), "20")
        self.app.vps_profile_combo.set(VPS_PROFILE_LABELS[VPS_PROFILE_MINIMUM])
        self.pump()
        self.assertEqual(self.app.values["vps_profile"].get(), VPS_PROFILE_MINIMUM)
        self.assertEqual(self.app.values["local_budget_gb"].get(), "2")
        self.assertEqual(self.app.values["min_free_disk_gb"].get(), "6")

    def test_manual_budget_edit_reaches_the_generated_config(self) -> None:
        for name, value in valid_values().items():
            if name in self.app.values:
                self.app.values[name].set(value)
        self.app.values["local_budget_gb"].set("4")
        self.app.values["min_free_disk_gb"].set("7")
        self.pump()
        config = parse_config(self.app._build_config(self.app._snapshot()))
        self.assertEqual(config["LOCAL_TEMP_BUDGET_GB"], "4")
        self.assertEqual(config["MIN_FREE_DISK_GB"], "7")
        # 界面默认是最小配置：内存阈值跟随预设，手改预算不影响它。
        self.assertEqual(config["MEMORY_SOFT_MIN_MB"], "256")
        self.assertEqual(config["MEMORY_HARD_MIN_MB"], "128")

    def test_destination_switch_keeps_the_selected_profile_numbers(self) -> None:
        self.app.vps_profile_combo.set(VPS_PROFILE_LABELS[VPS_PROFILE_STANDARD])
        self.pump()
        self.app.values["destination_type"].set(DESTINATION_GDRIVE)
        self.pump()
        self.assertEqual(self.app.values["vps_profile"].get(), VPS_PROFILE_STANDARD)
        self.assertEqual(self.app.values["local_budget_gb"].get(), "20")
        self.assertEqual(self.app.values["min_free_disk_gb"].get(), "20")
        self.app.values["destination_type"].set(DESTINATION_CLOUDDRIVE2)
        self.pump()
        self.assertEqual(self.app.values["vps_profile"].get(), VPS_PROFILE_STANDARD)
        self.assertEqual(self.app.values["local_budget_gb"].get(), "20")

    def test_ssh_key_fields_appear_beside_the_login_method(self) -> None:
        self.assertFalse(self.app._key_panel.grid_info())
        self.assertTrue(self.app._field_rows["vps_password"][0].grid_info())
        self.app.values["auth_method"].set("SSH 密钥")
        self.pump()
        self.assertTrue(self.app._key_panel.grid_info())
        self.assertFalse(self.app._field_rows["vps_password"][0].grid_info())
        self.assertIn("ssh_key_path", self.app.values)
        self.app.values["auth_method"].set("密码")
        self.pump()
        self.assertFalse(self.app._key_panel.grid_info())
        self.assertTrue(self.app._field_rows["vps_password"][0].grid_info())


if __name__ == "__main__":
    unittest.main()
