"""新手引导 UI 回归：① 连接服务器 → ② Telegram 信息 → ③ 保存位置 → ④ 部署并检查，
不常用的设置和排查按钮收进默认折叠的「高级选项」，出错时告诉用户去哪一步改什么。

所有窗口都用 withdraw() 创建；需要真实几何的布局用例把窗口映射到屏幕外并禁用激活，
不会弹出可见窗口，也不会抢用户焦点。
"""

from __future__ import annotations

import ctypes
import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))

import installer
from installer import (
    ADVANCED_FIELDS,
    ADVANCED_TOGGLE_CLOSED,
    ADVANCED_TOGGLE_OPEN,
    BEGINNER_STEPS,
    BEGINNER_STEPS_NOTE,
    DEPLOY_SUBSTEPS,
    DESTINATION_CLOUDDRIVE2,
    DESTINATION_GDRIVE,
    DESTINATION_LABELS,
    FIELD_HINTS,
    FIELD_LOCATIONS,
    MANAGED_CD2_WEBDAV_URL,
    STATUS_BUSY,
    STATUS_IDLE,
    STEP_NUMBERS,
    InstallerApp,
)

SW_SHOWNOACTIVATE = 4
OFFSCREEN = "+6000+6000"


def buttons_under(widget: tk.Misc) -> list[tk.Widget]:
    found: list[tk.Widget] = []
    for child in widget.winfo_children():
        if isinstance(child, installer.ttk.Button):
            found.append(child)
        found.extend(buttons_under(child))
    return found


def labels_under(widget: tk.Misc) -> list[str]:
    found: list[str] = []
    for child in widget.winfo_children():
        if isinstance(child, tk.Label):
            found.append(str(child.cget("text")))
        found.extend(labels_under(child))
    return found


def is_inside(widget: tk.Misc, ancestor: tk.Misc) -> bool:
    node: tk.Misc | None = widget
    while node is not None:
        if str(node) == str(ancestor):
            return True
        node = node.master
    return False


def map_offscreen(root: tk.Tk, width: int, height: int) -> bool:
    """把窗口映射到屏幕外并禁止激活；失败时返回 False，由用例跳过。"""
    root.geometry(f"{width}x{height}{OFFSCREEN}")
    try:
        root.attributes("-alpha", 0.0)
    except tk.TclError:
        pass
    root.deiconify()
    root.update_idletasks()
    root.update()
    try:
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        ctypes.windll.user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
    except Exception:  # noqa: BLE001, S110 - 非 Windows 或受限环境时保持离屏即可
        pass
    root.update_idletasks()
    root.update()
    return root.winfo_width() > 100 and root.winfo_height() > 100


class PackagedSelfTestRegression(unittest.TestCase):
    def test_self_test_accepts_the_step_layout(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tg115-self-test-") as folder:
            result = Path(folder) / "result.txt"
            self.assertEqual(installer.packaged_self_test(result), 0)
            report = result.read_text(encoding="utf-8")
            self.assertIn("ui_self_test=OK", report)
            self.assertIn("result=OK", report)


class WithdrawnAppTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = InstallerApp(self.root)
        self.app._stop_ui_drain()

    def tearDown(self) -> None:
        self.app.busy = False
        try:
            self.app.progress.stop()
        except tk.TclError:
            pass
        try:
            self.app._stop_ui_drain()
            self.root.destroy()
        except tk.TclError:
            pass

    def pump(self) -> None:
        self.root.update()

    def card(self, index: int) -> tk.Frame:
        return self.app._step_cards[index]

    def field_row(self, name: str) -> tk.Frame:
        return self.app._field_rows[name][0]


class StepOrderTests(WithdrawnAppTestCase):
    def test_four_numbered_steps_in_beginner_order(self) -> None:
        titles = [title for title, _purpose in BEGINNER_STEPS]
        self.assertEqual(titles, ["连接服务器", "Telegram 信息", "保存位置", "部署并检查"])
        self.assertEqual(STEP_NUMBERS, ("①", "②", "③", "④"))
        for _title, purpose in BEGINNER_STEPS:
            self.assertTrue(purpose.strip())

    def test_step_cards_render_with_number_title_and_purpose(self) -> None:
        self.assertEqual(len(self.app._step_cards), 4)
        for index, card in enumerate(self.app._step_cards):
            texts = labels_under(card)
            self.assertEqual(texts[0], STEP_NUMBERS[index])
            self.assertEqual(texts[1], BEGINNER_STEPS[index][0])
            self.assertEqual(card.purpose_label.cget("text"), BEGINNER_STEPS[index][1])

    def test_steps_one_to_three_scroll_in_order_and_step_four_stays_visible(self) -> None:
        body = self.app.form.body
        rows = [int(self.card(index).grid_info()["row"]) for index in range(3)]
        self.assertEqual(rows, sorted(rows))
        for index in range(3):
            self.assertTrue(is_inside(self.card(index), body))
        # 高级选项在 ③ 之后；④ 在滚动区外面，任何时候都看得到主按钮。
        advanced_row = int(self.app._advanced_card.grid_info()["row"])
        self.assertGreater(advanced_row, rows[-1])
        self.assertFalse(is_inside(self.card(3), body))

    def test_top_note_tells_returning_users_to_skip_installation(self) -> None:
        note = self.app._steps_note.cget("text")
        self.assertEqual(note, BEGINNER_STEPS_NOTE)
        self.assertIn("① → ④", note)
        self.assertIn("不用重新安装", note)


class StepContentTests(WithdrawnAppTestCase):
    STEP_FIELDS: ClassVar[dict[int, tuple[str, ...]]] = {
        0: ("vps_host", "vps_user", "vps_port", "vps_password", "ssh_key_path", "ssh_key_passphrase"),
        1: ("bot_token", "telegram_api_id", "telegram_api_hash", "allowed_user_id"),
        2: ("cd2_target", "gdrive_target", "cd2_url", "cd2_username", "cd2_password"),
    }

    def test_each_step_holds_only_its_own_fields(self) -> None:
        for index, names in self.STEP_FIELDS.items():
            for name in names:
                self.assertTrue(is_inside(self.field_row(name), self.card(index)), name)
                for other in range(4):
                    if other != index:
                        self.assertFalse(is_inside(self.field_row(name), self.card(other)), name)

    def test_rarely_needed_settings_live_in_the_advanced_section(self) -> None:
        body = self.app._advanced_body
        for name in ("sudo_password", "install_dir", "timezone", "local_budget_gb", "min_free_disk_gb"):
            self.assertTrue(is_inside(self.field_row(name), body), name)
        self.assertEqual(
            ADVANCED_FIELDS,
            {"sudo_password", "install_dir", "timezone", "local_budget_gb", "min_free_disk_gb", "vps_profile"},
        )
        for widget in (self.app.vps_profile_combo, *self.app.transfer_mode_buttons.values()):
            self.assertTrue(is_inside(widget, body), str(widget))

    def test_webdav_address_and_managed_checkbox_sit_in_step_three(self) -> None:
        # 地址每个人都要确认；“装在这台 VPS 上”决定地址怎么填，所以两者放在一起。
        self.assertTrue(is_inside(self.field_row("cd2_url"), self.card(2)))
        self.assertTrue(is_inside(self.app.cd2_checkbox, self.card(2)))
        self.assertFalse(is_inside(self.app.cd2_checkbox, self.app._advanced_body))

    def test_beginner_fields_explain_where_to_find_the_value(self) -> None:
        for names in self.STEP_FIELDS.values():
            for name in names:
                if name in {"ssh_key_passphrase", "cd2_url"}:
                    continue
                self.assertIn(FIELD_HINTS[name], labels_under(self.field_row(name)), name)
        self.assertIn("@BotFather", FIELD_HINTS["bot_token"])
        self.assertIn("my.telegram.org", FIELD_HINTS["telegram_api_id"])
        self.assertIn("@userinfobot", FIELD_HINTS["allowed_user_id"])

    def test_destination_choice_sits_in_step_three(self) -> None:
        for chip in self.app.destination_buttons.values():
            self.assertTrue(is_inside(chip, self.card(2)))
        self.assertTrue(is_inside(self.app.destination_help, self.card(2)))

    def test_defaults_and_saved_value_names_are_unchanged(self) -> None:
        values = {name: variable.get() for name, variable in self.app.values.items()}
        expected = {
            "vps_port": "22",
            "vps_user": "root",
            "auth_method": "密码",
            "destination_type": DESTINATION_CLOUDDRIVE2,
            "deploy_clouddrive2": "true",
            "cd2_url": MANAGED_CD2_WEBDAV_URL,
            "vps_profile": "minimum",
            "local_budget_gb": "2",
            "min_free_disk_gb": "6",
            "transfer_mode": "auto",
            "install_dir": "/opt/tg115",
            "timezone": "Asia/Shanghai",
            "cd2_target": "115/telegram转存",
            "gdrive_target": "GoogleDrive/telegram转存",
        }
        for name, value in expected.items():
            self.assertEqual(values[name], value, name)
        self.assertLessEqual(
            {
                "vps_host", "vps_port", "vps_user", "auth_method", "vps_password", "ssh_key_path",
                "ssh_key_passphrase", "sudo_password", "bot_token", "telegram_api_id",
                "telegram_api_hash", "allowed_user_id", "cd2_url", "cd2_username", "cd2_password",
                "cd2_target", "gdrive_target", "destination_type", "deploy_clouddrive2",
                "vps_profile", "local_budget_gb", "min_free_disk_gb", "transfer_mode",
                "install_dir", "timezone",
            },
            set(values),
        )


class ActionTests(WithdrawnAppTestCase):
    def test_step_four_has_one_primary_button_then_two_follow_ups(self) -> None:
        buttons = buttons_under(self.card(3))
        self.assertEqual(buttons, [self.app.deploy_button, self.app.cloud_button, self.app.verify_button])
        self.assertEqual(str(self.app.deploy_button.cget("style")), "Primary.TButton")
        for button in (self.app.cloud_button, self.app.verify_button):
            self.assertNotEqual(str(button.cget("style")), "Primary.TButton")
        primary = [
            button for button in buttons_under(self.root)
            if str(button.cget("style")) == "Primary.TButton"
        ]
        self.assertEqual(primary, [self.app.deploy_button])

    def test_follow_up_hints_say_when_to_click(self) -> None:
        texts = labels_under(self.card(3))
        for title, hint in DEPLOY_SUBSTEPS:
            self.assertIn(f"{title}：{hint}", texts)
        self.assertIn("部署成功后", DEPLOY_SUBSTEPS[1][1])
        self.assertIn("挂载好后", DEPLOY_SUBSTEPS[2][1])

    def test_button_texts_and_commands_are_kept(self) -> None:
        self.assertEqual(self.app.deploy_button.cget("text"), "一键部署（115）")
        self.assertEqual(self.app.cloud_button.cget("text"), "登录并挂载网盘")
        self.assertEqual(self.app.verify_button.cget("text"), "检查是否可用")
        for button, name in (
            (self.app.deploy_button, "deploy"),
            (self.app.cloud_button, "open_clouddrive"),
            (self.app.verify_button, "verify"),
            (self.app.test_button, "test_connection"),
            (self.app.repair_button, "repair_clouddrive"),
            (self.app.status_button, "check_status"),
            (self.app.clear_button, "_clear_log"),
        ):
            self.assertIn(name, str(button.cget("command")), name)

    def test_connection_test_sits_in_step_one(self) -> None:
        self.assertTrue(is_inside(self.app.test_button, self.card(0)))
        self.assertEqual(self.app.test_button.cget("text"), "测试连接")

    def test_destination_switch_updates_primary_text_and_target_field(self) -> None:
        self.app.values["destination_type"].set(DESTINATION_GDRIVE)
        self.pump()
        self.assertEqual(self.app.deploy_button.cget("text"), "一键部署（Google Drive）")
        self.assertTrue(self.app._target_frames[DESTINATION_GDRIVE].grid_info())
        self.assertFalse(self.app._target_frames[DESTINATION_CLOUDDRIVE2].grid_info())
        self.app.values["destination_type"].set(DESTINATION_CLOUDDRIVE2)
        self.pump()
        self.assertEqual(self.app.deploy_button.cget("text"), f"一键部署（{DESTINATION_LABELS[DESTINATION_CLOUDDRIVE2]}）")


class AdvancedSectionTests(WithdrawnAppTestCase):
    def test_collapsed_by_default(self) -> None:
        self.assertFalse(self.app._advanced_open.get())
        self.assertFalse(self.app._advanced_body.winfo_manager())
        self.assertEqual(self.app._advanced_toggle.cget("text"), ADVANCED_TOGGLE_CLOSED)

    def test_toggle_shows_and_hides(self) -> None:
        self.app._toggle_advanced()
        self.pump()
        self.assertTrue(self.app._advanced_open.get())
        self.assertEqual(self.app._advanced_body.winfo_manager(), "pack")
        self.assertEqual(self.app._advanced_toggle.cget("text"), ADVANCED_TOGGLE_OPEN)
        self.app._toggle_advanced()
        self.pump()
        self.assertFalse(self.app._advanced_body.winfo_manager())
        self.assertEqual(self.app._advanced_toggle.cget("text"), ADVANCED_TOGGLE_CLOSED)

    def test_troubleshooting_tools_live_in_the_advanced_section(self) -> None:
        for button in (self.app.repair_button, self.app.status_button, self.app.clear_button):
            self.assertEqual(button.master, self.app._more_actions_row)
            self.assertTrue(is_inside(button, self.app._advanced_body))
        self.assertTrue(any("一键部署已包含 SSH 连接测试" in text for text in labels_under(self.app._advanced_body)))

    def test_advanced_values_still_reach_the_generated_config(self) -> None:
        self.app.transfer_mode_buttons["stream"].invoke()
        self.app.values["timezone"].set("Europe/Berlin")
        config = self.app._build_config(self.app._snapshot())
        self.assertIn("TRANSFER_MODE=stream", config.splitlines())
        self.assertIn("TZ=Europe/Berlin", config.splitlines())


class ErrorGuidanceTests(WithdrawnAppTestCase):
    def test_every_field_error_names_the_step_and_field(self) -> None:
        cases = {
            "请填写：VPS IP 或域名": "① 连接服务器",
            "SSH 端口必须是数字": "① 连接服务器",
            "密码登录方式必须填写 VPS 登录密码": "① 连接服务器",
            "请填写：Bot Token": "② Telegram 信息",
            "Telegram API Hash 应为 32 位十六进制字符": "② Telegram 信息",
            "请填写：Telegram 数字 ID": "② Telegram 信息",
            "请填写：WebDAV 用户名": "③ 保存位置",
            "公网 WebDAV 地址必须使用 HTTPS，避免密码明文传输": "③ 保存位置",
            "Google Drive 目标路径不能包含 ..": "③ 保存位置",
            "安装目录只能是 /opt/ 下不含空格的安全路径": "高级选项",
            "sudo 密码不能包含换行符": "高级选项",
        }
        for message, step in cases.items():
            field, hint = InstallerApp._next_step_for_error(message)
            self.assertTrue(field, message)
            self.assertTrue(hint.startswith("下一步："), message)
            self.assertIn(step, hint, message)
            self.assertIn(FIELD_LOCATIONS[field][1], hint, message)

    def test_connection_failures_point_back_to_step_one(self) -> None:
        for message in ("Authentication failed.", "timed out", "[Errno 111] Connection refused"):
            field, hint = InstallerApp._next_step_for_error(message)
            self.assertEqual(field, "")
            self.assertIn("① 连接服务器", hint)
            self.assertIn("测试连接", hint)

    def test_unknown_failures_point_to_the_log(self) -> None:
        field, hint = InstallerApp._next_step_for_error("docker build failed")
        self.assertEqual(field, "")
        self.assertIn("部署记录", hint)

    def test_show_error_adds_the_next_step_and_marks_the_field(self) -> None:
        with patch.object(installer.messagebox, "showerror") as showerror:
            self.app._show_error("操作失败", ValueError("请填写：Bot Token"))
            self.pump()
        shown = showerror.call_args.args[1]
        self.assertIn("请填写：Bot Token", shown)
        self.assertIn("下一步：到「② Telegram 信息」", shown)
        message_label = self.app._field_rows["bot_token"][1]
        self.assertEqual(message_label.cget("text"), "请填写：Bot Token")
        self.assertTrue(message_label.grid_info())
        self.assertEqual(self.app._status_text.get(), "操作失败")
        self.assertIn("下一步", self.app.log.get("1.0", tk.END))

    def test_error_in_an_advanced_field_opens_the_advanced_section(self) -> None:
        with patch.object(installer.messagebox, "showerror"):
            self.app._show_error("操作失败", ValueError("时区格式不正确"))
            self.pump()
        self.assertTrue(self.app._advanced_open.get())
        self.assertTrue(self.app._field_rows["timezone"][1].grid_info())

    def test_message_that_already_has_a_next_step_is_not_doubled(self) -> None:
        with patch.object(installer.messagebox, "showerror") as showerror:
            self.app._show_error("失败", RuntimeError("挂载缺失。下一步：去 CloudDrive2 挂载"))
            self.pump()
        self.assertEqual(showerror.call_args.args[1].count("下一步"), 1)

    def test_inline_error_takes_no_space_until_shown(self) -> None:
        label = self.app._field_rows["vps_host"][1]
        self.assertFalse(label.grid_info())
        self.app._mark_field_error("请填写：VPS IP 或域名")
        self.assertTrue(label.grid_info())
        self.app._clear_field_error("vps_host")
        self.assertFalse(label.grid_info())


class BusyAndStatusTests(WithdrawnAppTestCase):
    def all_buttons(self) -> tuple[installer.ttk.Button, ...]:
        return (
            self.app.deploy_button,
            self.app.cloud_button,
            self.app.verify_button,
            self.app.test_button,
            self.app.repair_button,
            self.app.clear_button,
            self.app.status_button,
        )

    def test_busy_disables_every_action_including_the_advanced_section(self) -> None:
        self.app._set_busy(True)
        self.pump()
        for button in self.all_buttons():
            self.assertEqual(str(button.cget("state")), "disabled", button.cget("text"))
        self.assertEqual(self.app._status_text.get(), STATUS_BUSY)
        self.app._set_busy(False)
        self.pump()
        for button in self.all_buttons():
            self.assertEqual(str(button.cget("state")), "normal", button.cget("text"))
        self.assertEqual(self.app._status_text.get(), STATUS_IDLE)

    def test_update_buttons_never_overwrites_a_precise_status(self) -> None:
        self.app._set_busy(True)
        self.pump()
        self.app._status_text.set("正在校验 WebDAV 写入")
        self.app._update_buttons()
        self.assertEqual(self.app._status_text.get(), "正在校验 WebDAV 写入")
        self.app._set_busy(False)
        self.pump()
        self.assertEqual(self.app._status_text.get(), "正在校验 WebDAV 写入")


class StatusButtonAdoptionTests(WithdrawnAppTestCase):
    def test_status_button_is_packed_in_the_tools_row(self) -> None:
        self.assertEqual(self.app.status_button.winfo_manager(), "pack")
        self.assertIn(self.app.status_button, buttons_under(self.app._more_actions_row))
        self.assertNotIn(self.app.status_button, buttons_under(self.card(3)))

    def test_status_button_parented_elsewhere_is_left_alone(self) -> None:
        elsewhere = tk.Frame(self.root)
        button = installer.ttk.Button(elsewhere, text="查看 VPS 状态")
        self.app.status_button = button
        self.app._adopt_status_button()
        self.assertEqual(button.winfo_manager(), "")

    def test_absent_status_button_keeps_the_ui_working(self) -> None:
        self.app.status_button.destroy()
        del self.app.status_button
        self.app._update_buttons()
        self.app._adopt_status_button()


class LayoutTests(WithdrawnAppTestCase):
    """真实映射到屏幕外，检查不同窗口尺寸下不裁切、不重叠。"""

    def measure(self, width: int, height: int, *, advanced: bool = False) -> None:
        if advanced:
            self.app._toggle_advanced(True)
        if not map_offscreen(self.root, width, height):
            self.skipTest("当前环境无法映射窗口，跳过真实布局检查")
        try:
            self.check_layout()
        finally:
            self.root.withdraw()
            self.root.update()

    def check_layout(self) -> None:
        win_h = self.root.winfo_height()
        win_w = self.root.winfo_width()
        form = self.app.form
        deploy = self.card(3)
        self.assertLess(form.winfo_y(), deploy.winfo_y())
        self.assertGreaterEqual(form.winfo_height(), 120)
        self.assertLessEqual(self.app.log.winfo_rooty() - self.root.winfo_rooty(), win_h)
        right = 0
        for button in (self.app.deploy_button, self.app.cloud_button, self.app.verify_button):
            self.assertGreaterEqual(button.winfo_width(), button.winfo_reqwidth(), str(button))
            self.assertGreaterEqual(button.winfo_height(), button.winfo_reqheight(), str(button))
            left = button.winfo_rootx() - self.root.winfo_rootx()
            self.assertGreaterEqual(left, right)
            right = left + button.winfo_width()
            bottom = button.winfo_rooty() - self.root.winfo_rooty() + button.winfo_height()
            self.assertLessEqual(bottom, win_h)
        self.assertLessEqual(right, win_w)

    def test_minimum_window_size_layout(self) -> None:
        self.measure(980, 680)

    def test_default_window_size_layout(self) -> None:
        self.measure(1180, 860)

    def test_wide_window_layout(self) -> None:
        self.measure(1440, 900)

    def test_expanded_advanced_section_keeps_step_four_visible(self) -> None:
        self.measure(1180, 860, advanced=True)


if __name__ == "__main__":
    unittest.main()
