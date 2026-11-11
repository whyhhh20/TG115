"""只读 VPS 运行状态检查（“查看 VPS 状态”按钮）的回归测试。

覆盖：Bot 运行 / 停止 / 容器不存在 / 安装未完成 / 状态未知 / SSH 失败，
以及双目标（115、Google Drive）下的只读语义与 busy 防重复。
"""
from __future__ import annotations

import tkinter as tk
import unittest
from unittest.mock import Mock, patch

import paramiko

from installer import InstallerApp

RUNNING_OUTPUT = """TG115_PS_BEGIN
NAME                IMAGE     COMMAND   SERVICE     CREATED        STATUS        PORTS
tg115-bot-1         tg115     "python"  tg115-bot   2 hours ago    Up 2 hours
TG115_PS_END
TG115_INSPECT_BEGIN
running|2026-11-11T01:00:00.000000000Z|0|0
TG115_INSPECT_END
TG115_LOGS_BEGIN
2026-11-11 01:00:01 INFO bot started
2026-11-11 01:00:02 INFO waiting for messages
TG115_LOGS_END
"""

STOPPED_OUTPUT = """TG115_PS_BEGIN
NAME          SERVICE     STATUS
tg115-bot-1   tg115-bot   Exited (1) 3 minutes ago
TG115_PS_END
TG115_INSPECT_BEGIN
exited|2026-11-11T01:00:00.000000000Z|2|1
TG115_INSPECT_END
TG115_LOGS_BEGIN
2026-11-11 01:05:00 ERROR rclone not reachable
TG115_LOGS_END
"""

MISSING_OUTPUT = """TG115_PS_BEGIN
TG115_PS_END
TG115_INSPECT_BEGIN
Error: No such object: tg115-bot
TG115_INSPECT_END
TG115_LOGS_BEGIN
no such service: tg115-bot
TG115_LOGS_END
"""

NOT_INSTALLED_OUTPUT = "TG115_CD_FAILED\n"

DOCKER_MISSING_OUTPUT = """TG115_PS_BEGIN
Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?
TG115_PS_END
TG115_INSPECT_BEGIN
Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?
TG115_INSPECT_END
TG115_LOGS_BEGIN
TG115_LOGS_END
"""

PS_ONLY_OUTPUT = """TG115_PS_BEGIN
NAME          SERVICE     STATUS
tg115-bot-1   tg115-bot   Up 2 hours
TG115_PS_END
TG115_INSPECT_BEGIN
TG115_INSPECT_END
TG115_LOGS_BEGIN
TG115_LOGS_END
"""


def status_app(destination: str = "clouddrive2") -> InstallerApp:
    """不启动 Tk 的 InstallerApp，只注入 check_status 用到的依赖。"""
    app = InstallerApp.__new__(InstallerApp)
    app.busy = False
    app._secrets = []
    app._snapshot = Mock(
        return_value={
            "destination_type": destination,
            "install_dir": "/opt/tg115",
            "vps_host": "203.0.113.10",
            "vps_port": "22",
            "vps_user": "root",
            "auth_method": "密码",
            "vps_password": "local-test-password",
        }
    )
    app._validate_connection = Mock()
    app._run_worker = lambda action: action()
    app._log = Mock()
    app._show_info = Mock()
    app._show_error = Mock()
    app._redact = lambda text: str(text)
    return app


class StatusClassificationTests(unittest.TestCase):
    def test_running_requires_the_container_state_status(self) -> None:
        info = InstallerApp._classify_status(RUNNING_OUTPUT)
        self.assertEqual(info["state"], "running")
        self.assertEqual(info["status"], "running")
        self.assertEqual(info["restarts"], "0")
        self.assertIn("bot started", info["logs"])

    def test_docker_ps_alone_never_counts_as_running(self) -> None:
        info = InstallerApp._classify_status(PS_ONLY_OUTPUT)
        self.assertNotEqual(info["state"], "running")
        self.assertEqual(info["state"], "unknown")
        message = InstallerApp._status_message("clouddrive2", info)
        self.assertNotIn("正在运行", message)
        self.assertIn("状态未知", message)

    def test_exited_container_reports_stopped_with_exit_code(self) -> None:
        info = InstallerApp._classify_status(STOPPED_OUTPUT)
        self.assertEqual(info["state"], "stopped")
        self.assertEqual(info["exit_code"], "1")
        self.assertEqual(info["restarts"], "2")
        message = InstallerApp._status_message("clouddrive2", info)
        self.assertIn("已停止", message)
        self.assertIn("退出码 1", message)

    def test_absent_container_is_missing_not_running(self) -> None:
        info = InstallerApp._classify_status(MISSING_OUTPUT)
        self.assertEqual(info["state"], "missing")
        self.assertIn("不存在", InstallerApp._status_message("clouddrive2", info))

    def test_missing_install_directory_is_not_installed(self) -> None:
        info = InstallerApp._classify_status(NOT_INSTALLED_OUTPUT)
        self.assertEqual(info["state"], "not_installed")
        self.assertIn(
            "安装仍未完成", InstallerApp._status_message("clouddrive2", info)
        )

    def test_docker_unavailable_does_not_claim_installation_is_missing(self) -> None:
        info = InstallerApp._classify_status(DOCKER_MISSING_OUTPUT)
        self.assertEqual(info["state"], "unknown")
        message = InstallerApp._status_message("gdrive", info)
        self.assertIn("Docker", message)
        self.assertNotIn("安装仍未完成", message)

    def test_unparsable_output_is_unknown(self) -> None:
        info = InstallerApp._classify_status("")
        self.assertEqual(info["state"], "unknown")
        self.assertIn("状态未知", InstallerApp._status_message("gdrive", info))


class CheckStatusWorkerTests(unittest.TestCase):
    def test_command_reads_only_state_fields_and_never_config_env(self) -> None:
        app = status_app()
        session = Mock()
        session.run.return_value = (0, RUNNING_OUTPUT)
        app._new_session = Mock(return_value=session)

        app.check_status()

        command = session.run.call_args.args[0]
        for field in (
            ".State.Status",
            ".State.StartedAt",
            ".RestartCount",
            ".State.ExitCode",
        ):
            self.assertIn(field, command)
        self.assertIn("docker inspect", command)
        self.assertNotIn(".State.RestartCount", command)
        self.assertIn("docker compose ps", command)
        self.assertIn("logs --tail=", command)
        self.assertNotIn(".Config", command)
        self.assertNotIn(".Env", command)
        self.assertNotIn("State.Health", command)
        self.assertNotIn("exec -T", command)
        self.assertNotIn("verify_destination", command)
        self.assertNotIn("compose up", command)
        self.assertNotIn("compose down", command)
        self.assertNotIn("restart", command)
        session.close.assert_called_once()

    def test_status_is_read_only_and_does_not_claim_webdav_ingest(self) -> None:
        app = status_app()
        session = Mock()
        session.run.return_value = (0, RUNNING_OUTPUT)
        app._new_session = Mock(return_value=session)

        app.check_status()

        message = app._show_info.call_args.args[1]
        self.assertIn("正在运行", message)
        self.assertIn("115", message)
        self.assertIn("只读", message)
        self.assertIn("不代表", message)
        self.assertIn("WebDAV", message)
        logged = " ".join(str(call.args[0]) for call in app._log.call_args_list)
        self.assertIn("不代表 WebDAV", logged)

    def test_both_destinations_report_their_own_target_label(self) -> None:
        for destination, label in (
            ("clouddrive2", "115"),
            ("gdrive", "Google Drive"),
        ):
            with self.subTest(destination=destination):
                app = status_app(destination)
                session = Mock()
                session.run.return_value = (0, STOPPED_OUTPUT)
                app._new_session = Mock(return_value=session)

                app.check_status()

                message = app._show_info.call_args.args[1]
                self.assertIn(label, message)
                self.assertIn("已停止", message)
                self.assertIn("不代表", message)
                self.assertNotIn("验收通过", message)
                session.close.assert_called_once()

    def test_ssh_failure_is_classified_and_never_reported_as_running(self) -> None:
        app = status_app()
        app._new_session = Mock(side_effect=paramiko.SSHException("auth failed"))

        with self.assertRaises(RuntimeError) as caught:
            app.check_status()

        self.assertIn("SSH 连接失败", str(caught.exception))
        logged = " ".join(str(call.args[0]) for call in app._log.call_args_list)
        self.assertIn("SSH 连接失败", logged)
        self.assertNotIn("正在运行", logged)
        app._show_info.assert_not_called()

    def test_remote_command_failure_still_closes_the_session(self) -> None:
        app = status_app()
        session = Mock()
        session.run.side_effect = RuntimeError("channel closed")
        app._new_session = Mock(return_value=session)

        with self.assertRaises(RuntimeError):
            app.check_status()

        session.close.assert_called_once()
        app._show_info.assert_not_called()

    def test_busy_state_prevents_a_second_status_check(self) -> None:
        app = InstallerApp.__new__(InstallerApp)
        app.busy = True
        app._snapshot = Mock(return_value={"destination_type": "clouddrive2"})
        app._new_session = Mock()

        app.check_status()

        app._new_session.assert_not_called()


class StatusButtonTests(unittest.TestCase):
    """UI 测试一律 withdraw()，不显示窗口、不抢焦点。"""

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

    def test_status_button_is_low_weight_and_bound_to_check_status(self) -> None:
        button = self.app.status_button
        self.assertEqual(button.cget("text"), "查看 VPS 状态")
        self.assertIn("Quiet", str(button.cget("style")))
        self.assertIn("check_status", str(button.cget("command")))

    def test_status_button_shares_the_existing_busy_state(self) -> None:
        self.app._set_busy(True)
        self.pump()
        for button in (
            self.app.deploy_button,
            self.app.test_button,
            self.app.status_button,
            self.app.verify_button,
            self.app.cloud_button,
            self.app.repair_button,
            self.app.clear_button,
        ):
            self.assertEqual(str(button.cget("state")), "disabled")
        self.app._set_busy(False)
        self.pump()
        for button in (
            self.app.deploy_button,
            self.app.status_button,
            self.app.verify_button,
        ):
            self.assertEqual(str(button.cget("state")), "normal")

    def test_status_button_does_not_run_webdav_verification(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            with (
                patch.object(InstallerApp, "check_status") as check_status,
                patch.object(InstallerApp, "verify") as verify,
            ):
                app = InstallerApp(root)
                app.status_button.invoke()
                root.update()
            check_status.assert_called_once()
            verify.assert_not_called()
        finally:
            app.busy = False
            app._stop_ui_drain()
            root.destroy()


if __name__ == "__main__":
    unittest.main()
