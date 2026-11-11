from __future__ import annotations

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
    DESTINATION_CLOUDDRIVE2,
    DESTINATION_GDRIVE,
    FAILURE_AUTH,
    FAILURE_CONTAINER,
    FAILURE_NETWORK,
    FAILURE_PATH,
    FAILURE_PROTOCOL,
    FAILURE_TAIL_LINES,
    FAILURE_TAIL_WIDTH,
    FAILURE_TIMEOUT,
    FAILURE_UNKNOWN,
    InstallerApp,
    bounded_output_tail,
    classify_failure,
    deploy_success_message,
    describe_failure,
    format_diagnostic_tail,
    verify_success_message,
)

REPAIR_SUCCESS_OUTPUT = "[TG115-REPAIR] Bot 已解析并连通 CloudDrive2：172.18.0.5:19798\nTG115_REPAIR=SUCCESS\n"

AUTH_FAILURE_OUTPUT = (
    "[TG115-REPAIR] 目标网盘：115（经 CloudDrive2 WebDAV 传输）\n"
    "Traceback (most recent call last):\n"
    "RuntimeError: WebDAV 401 Unauthorized: Invalid credentials\n"
    "TG115_REPAIR=FAILED\n"
)


def valid_values(destination: str = DESTINATION_CLOUDDRIVE2) -> dict[str, str]:
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
        "cd2_url": installer.MANAGED_CD2_WEBDAV_URL,
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


class FakeSftp:
    def __init__(self) -> None:
        self.puts: list[tuple[str, str]] = []
        self.chmods: list[tuple[str, int]] = []
        self.closed = False

    def put(self, local: str, remote: str) -> None:
        self.puts.append((local, remote))

    def chmod(self, remote: str, mode: int) -> None:
        self.chmods.append((remote, mode))

    def close(self) -> None:
        self.closed = True


class FakeSession:
    """只回放固定输出的 SSH 会话替身，不发任何真实请求。"""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.commands: list[str] = []
        self.sftp_client = FakeSftp()
        self.closed = False

    def run(self, command, *, sudo=False, stream=None, timeout=None):
        self.commands.append(command)
        code, output = self.handler(command)
        if stream is not None:
            for line in output.splitlines():
                stream(line)
        return code, output

    def sftp(self) -> FakeSftp:
        return self.sftp_client

    def close(self) -> None:
        self.closed = True


def default_handler(command: str) -> tuple[int, str]:
    stripped = command.strip()
    if stripped.startswith("mkdir"):
        return 0, ""
    if stripped == "id -u":
        return 0, "0\n"
    if stripped.startswith("rm -rf"):
        return 0, ""
    return 0, ""


def make_session(
    *,
    repair_output: str = REPAIR_SUCCESS_OUTPUT,
    verify_output: str = "",
    verify_code: int | None = None,
):
    """按命令内容区分修复脚本与验收命令，返回 SSH 会话替身。"""

    def handler(command: str) -> tuple[int, str]:
        if "--network-only" in command:
            return (0 if "TG115_REPAIR=SUCCESS" in repair_output else 1), repair_output
        if "verify_destination" in command:
            code = (
                (0 if "TG115_DESTINATION=OK" in verify_output else 1)
                if verify_code is None
                else verify_code
            )
            return code, verify_output
        return default_handler(command)

    return FakeSession(handler)


class WithdrawnRootTestCase(unittest.TestCase):
    """所有 UI 测试都使用 withdraw() 的 root，绝不显示窗口、不抢焦点。"""

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

    def pump_until_idle(self, timeout: float = 5.0) -> None:
        """等后台操作结束且主线程把排队的 UI 回调全部执行完。

        busy 在主线程上、排在失败弹窗/状态栏回调之后才复位，再加上队列为空，
        断言就不依赖线程调度的先后。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump()
            if not self.app.busy and self.app._ui_queue.empty():
                self.pump()
                return
            time.sleep(0.01)
        self.fail("后台操作在超时前没有结束")

    def pump_until(self, predicate, timeout: float = 5.0) -> bool:
        """等主线程把后台线程排队的回调执行完（弹窗/状态都在回调里）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump()
            if predicate():
                return True
            time.sleep(0.01)
        return bool(predicate())

    def load_values(self, destination: str = DESTINATION_CLOUDDRIVE2) -> None:
        for name, value in valid_values(destination).items():
            if name in self.app.values:
                self.app.values[name].set(value)
        self.app.values["destination_type"].set(destination)
        self.pump()


class FailureClassificationTests(unittest.TestCase):
    def test_auth_failure_is_classified_as_auth_not_network(self) -> None:
        output = "RuntimeError: WebDAV 401 Unauthorized: Invalid credentials"
        self.assertEqual(classify_failure(output), FAILURE_AUTH)

    def test_forbidden_is_auth(self) -> None:
        self.assertEqual(classify_failure("HTTP 403 Forbidden"), FAILURE_AUTH)

    def test_missing_path_is_classified_as_path(self) -> None:
        output = "rclone: 404 Not Found: directory not found: /115/Telegram"
        self.assertEqual(classify_failure(output), FAILURE_PATH)

    def test_dns_and_port_failures_are_network(self) -> None:
        for output in (
            "socket.gaierror: [Errno -2] Name or service not known (lookup clouddrive2)",
            "Connection refused: clouddrive2:19798",
            "Bot 仍无法通过 clouddrive2:19798 访问 CloudDrive2",
        ):
            with self.subTest(output=output):
                self.assertEqual(classify_failure(output), FAILURE_NETWORK)

    def test_https_against_http_port_is_protocol(self) -> None:
        output = "rclone: server gave HTTP response to HTTPS client"
        self.assertEqual(classify_failure(output), FAILURE_PROTOCOL)

    def test_stopped_container_is_container(self) -> None:
        for output in (
            "CloudDrive2 容器没有运行：tg115-clouddrive2",
            "Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
            "Error response from daemon: No such container: tg115-bot",
        ):
            with self.subTest(output=output):
                self.assertEqual(classify_failure(output), FAILURE_CONTAINER)

    def test_timeout_is_timeout(self) -> None:
        self.assertEqual(classify_failure("curl: (28) Operation timed out"), FAILURE_TIMEOUT)

    def test_unrecognized_output_is_unknown(self) -> None:
        self.assertEqual(classify_failure("一切正常但没有通过标记"), FAILURE_UNKNOWN)

    def test_empty_output_is_unknown(self) -> None:
        self.assertEqual(classify_failure(""), FAILURE_UNKNOWN)

    def test_tail_is_bounded_in_lines_and_width(self) -> None:
        output = "\n".join(f"line-{index}" for index in range(200))
        tail = bounded_output_tail(output)
        self.assertEqual(len(tail), FAILURE_TAIL_LINES)
        self.assertEqual(tail[-1], "line-199")
        long_line = "x" * (FAILURE_TAIL_WIDTH * 3)
        self.assertLessEqual(len(bounded_output_tail(long_line)[0]), FAILURE_TAIL_WIDTH)

    def test_tail_keeps_only_the_end_of_the_output(self) -> None:
        output = "\n".join(f"line-{index}" for index in range(30))
        tail = bounded_output_tail(output)
        self.assertNotIn("line-0", tail)
        self.assertIn("line-29", tail)

    def test_empty_output_has_no_diagnostic_tail(self) -> None:
        self.assertEqual(format_diagnostic_tail(""), "")

    def test_describe_failure_keeps_specific_cause_next_step_and_raw_tail(self) -> None:
        message = describe_failure("verify", AUTH_FAILURE_OUTPUT, DESTINATION_CLOUDDRIVE2)
        self.assertIn("认证失败", message)
        self.assertIn("下一步：", message)
        self.assertIn("401 Unauthorized", message)
        self.assertIn("原始诊断要点", message)
        self.assertIn("115", message)
        self.assertNotIn("网络修复", message.split("下一步：")[0])

    def test_repair_and_verify_leads_are_different(self) -> None:
        repair = describe_failure("repair", "Connection refused", DESTINATION_CLOUDDRIVE2)
        verify = describe_failure("verify", "Connection refused", DESTINATION_CLOUDDRIVE2)
        self.assertIn("网络连接没有修复成功", repair)
        self.assertIn("WebDAV 写入验收没有通过", verify)
        self.assertNotEqual(repair, verify)

    def test_google_drive_label_is_used_in_diagnostics(self) -> None:
        message = describe_failure("verify", AUTH_FAILURE_OUTPUT, DESTINATION_GDRIVE)
        self.assertIn("Google Drive", message)


class SuccessMessageTests(unittest.TestCase):
    def test_deploy_message_says_installed_only_and_needs_mounting(self) -> None:
        for destination, label in (
            (DESTINATION_CLOUDDRIVE2, "115"),
            (DESTINATION_GDRIVE, "Google Drive"),
        ):
            with self.subTest(destination=destination):
                message = deploy_success_message(destination)
                self.assertIn("已经安装", message)
                self.assertIn("尚未检查", message)
                self.assertNotIn("还不能收发", message)
                self.assertIn("挂载", message)
                self.assertIn(label, message)
                self.assertIn("登录并挂载网盘", message)
                self.assertIn("检查是否可用", message)
                # 安装成功不能宣称网盘已经可用。
                self.assertNotIn("已通过", message)
                self.assertNotIn("可以收发文件", message)

    def test_verify_message_claims_only_cd2_receipt(self) -> None:
        for destination, label in (
            (DESTINATION_CLOUDDRIVE2, "115"),
            (DESTINATION_GDRIVE, "Google Drive"),
        ):
            with self.subTest(destination=destination):
                message = verify_success_message(destination)
                self.assertIn("CloudDrive2 已接收", message)
                self.assertIn("不代表", message)
                self.assertIn("官方端", message)
                self.assertIn(label, message)


class RepairCloudDriveTests(WithdrawnRootTestCase):
    def test_repair_runs_the_script_in_network_only_mode(self) -> None:
        for destination in (DESTINATION_CLOUDDRIVE2, DESTINATION_GDRIVE):
            with self.subTest(destination=destination):
                self.load_values(destination)
                session = make_session()
                self.app._run_worker = lambda function: function()
                with (
                    patch.object(self.app, "_new_session", return_value=session),
                    patch.object(installer.messagebox, "showinfo"),
                ):
                    self.app.repair_clouddrive()
                repair_commands = [
                    command for command in session.commands if "repair_clouddrive_network.sh" in command
                ]
                self.assertEqual(len(repair_commands), 1)
                self.assertIn("--network-only", repair_commands[0])
                # 修复阶段不得自己再跑一次网盘写入校验。
                self.assertNotIn("verify_destination", " ".join(session.commands))
                self.assertTrue(session.closed)

    def test_repair_success_does_not_claim_webdav_is_ready(self) -> None:
        self.load_values()
        session = make_session(repair_output=REPAIR_SUCCESS_OUTPUT)
        self.app._run_worker = lambda function: function()
        with (
            patch.object(self.app, "_new_session", return_value=session),
            patch.object(installer.messagebox, "showinfo") as showinfo,
        ):
            self.app.repair_clouddrive()
        title, message = showinfo.call_args.args
        self.assertEqual(title, "网络连接已修复")
        self.assertIn("只说明连接通了", message)
        self.assertIn("挂载", message)
        self.assertIn("检查是否可用", message)
        # 不能把“网络修好”说成“WebDAV 通过”。
        self.assertNotIn("写入、校验、改名和清理", message)
        self.assertNotIn("WebDAV 真实验收", self.log_text())
        self.assertIn("未做网盘写入校验", self.log_text())

    def test_repair_failure_reports_the_real_cause(self) -> None:
        self.load_values()
        session = make_session(repair_output=AUTH_FAILURE_OUTPUT)
        self.app._run_worker = lambda function: function()
        with (
            patch.object(self.app, "_new_session", return_value=session),
            patch.object(installer.messagebox, "showinfo"),
            self.assertRaises(RuntimeError) as caught,
        ):
            self.app.repair_clouddrive()
        message = str(caught.exception)
        self.assertIn("认证失败", message)
        self.assertIn("下一步：", message)
        self.assertIn("401 Unauthorized", message)
        self.assertNotIn("网络修复或 WebDAV 验收没有通过", message)

    def test_repair_network_failure_points_at_network(self) -> None:
        self.load_values()
        session = make_session(
            repair_output="[TG115-REPAIR][ERROR] Bot 仍无法通过 clouddrive2:19798 访问 CloudDrive2\nTG115_REPAIR=FAILED\n"
        )
        self.app._run_worker = lambda function: function()
        with (
            patch.object(self.app, "_new_session", return_value=session),
            patch.object(installer.messagebox, "showinfo"),
            self.assertRaises(RuntimeError) as caught,
        ):
            self.app.repair_clouddrive()
        message = str(caught.exception)
        self.assertIn("网络不通", message)
        self.assertIn("19798", message)
        self.assertIn("TG115_REPAIR=FAILED", message)


class VerifyTests(WithdrawnRootTestCase):
    def test_auth_failure_is_reported_as_auth_with_next_step(self) -> None:
        for destination, label in (
            (DESTINATION_CLOUDDRIVE2, "115"),
            (DESTINATION_GDRIVE, "Google Drive"),
        ):
            with self.subTest(destination=destination):
                self.load_values(destination)
                session = make_session(verify_output=AUTH_FAILURE_OUTPUT)
                self.app._run_worker = lambda function: function()
                with (
                    patch.object(self.app, "_new_session", return_value=session),
                    patch.object(installer.messagebox, "showinfo"),
                    self.assertRaises(RuntimeError) as caught,
                ):
                    self.app.verify()
                message = str(caught.exception)
                self.assertIn("认证失败", message)
                self.assertIn(label, message)
                self.assertIn("下一步：", message)
                self.assertIn("401 Unauthorized", message)

    def test_missing_pass_marker_keeps_a_bounded_tail(self) -> None:
        self.load_values()
        session = make_session(verify_output="docker compose ps\n容器状态未知\n", verify_code=0)
        self.app._run_worker = lambda function: function()
        with (
            patch.object(self.app, "_new_session", return_value=session),
            patch.object(installer.messagebox, "showinfo"),
            self.assertRaises(RuntimeError) as caught,
        ):
            self.app.verify()
        message = str(caught.exception)
        self.assertIn("没有返回通过标记", message)
        self.assertIn("原始诊断要点", message)
        self.assertIn("容器状态未知", message)

    def test_verify_success_shows_receipt_message_and_result_status(self) -> None:
        self.load_values()
        session = make_session(
            verify_output="TG115_DESTINATION=OK\n测试文件已写入、校验、改名并清理\n"
        )
        with (
            patch.object(self.app, "_new_session", return_value=session),
            patch.object(installer.messagebox, "showinfo") as showinfo,
            patch.object(installer.messagebox, "showerror") as showerror,
        ):
            self.app.verify()
            self.assertTrue(self.pump_until(lambda: showinfo.called))
            self.pump_until_idle()
            self.pump()
        showerror.assert_not_called()
        title, message = showinfo.call_args.args
        self.assertEqual(title, "验收通过")
        self.assertIn("CloudDrive2 已接收", message)
        # 状态栏保留本次结果，而不是笼统的“已完成”。
        self.assertEqual(self.app._status_text.get(), "验收通过")
        self.assertEqual(str(self.app.verify_button.cget("state")), "normal")


class WorkerStateTests(WithdrawnRootTestCase):
    def test_failure_marks_status_failed_and_restores_buttons(self) -> None:
        def boom() -> None:
            raise RuntimeError("worker-boom")

        with patch.object(installer.messagebox, "showerror") as showerror:
            self.app._run_worker(boom)
            self.pump_until_idle()
            self.pump()
        showerror.assert_called_once()
        self.assertIn("worker-boom", self.log_text())
        self.assertEqual(self.app._status_text.get(), "操作失败")
        self.assertNotEqual(self.app._status_text.get(), "已完成")
        self.assertEqual(str(self.app.deploy_button.cget("state")), "normal")
        self.assertEqual(str(self.app.repair_button.cget("state")), "normal")
        self.assertFalse(self.app.busy)

    def test_success_keeps_the_operation_result_in_the_status_bar(self) -> None:
        def fine() -> None:
            self.app._show_info("网络连接已修复", "ok")

        with patch.object(installer.messagebox, "showinfo"):
            self.app._run_worker(fine)
            self.pump_until_idle()
            self.pump()
        self.assertEqual(self.app._status_text.get(), "网络连接已修复")
        self.assertEqual(str(self.app.verify_button.cget("state")), "normal")

    def test_busy_guard_ignores_a_second_worker(self) -> None:
        started = threading.Event()

        def slow() -> None:
            started.set()
            time.sleep(0.2)

        self.app._run_worker(slow)
        started.wait(5)
        self.app._run_worker(slow)
        self.pump_until_idle()
        self.assertFalse(self.app.busy)


class RedactionTests(WithdrawnRootTestCase):
    def test_failure_output_never_leaks_a_secret(self) -> None:
        self.load_values()
        self.app.values["cd2_password"].set("cd2-secret-value")
        self.app._secrets = ["cd2-secret-value"]
        session = make_session(
            verify_output="RuntimeError: 401 Unauthorized for user:cd2-secret-value\n"
        )
        with (
            patch.object(self.app, "_new_session", return_value=session),
            patch.object(installer.messagebox, "showerror") as showerror,
        ):
            self.app.verify()
            self.assertTrue(self.pump_until(lambda: showerror.called))
            self.pump_until_idle()
            self.pump()
        popup = showerror.call_args.args[1]
        self.assertNotIn("cd2-secret-value", popup)
        self.assertNotIn("cd2-secret-value", self.log_text())
        self.assertIn("认证失败", popup)


if __name__ == "__main__":
    unittest.main()
