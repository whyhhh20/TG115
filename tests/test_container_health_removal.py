from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import installer

ROOT = Path(__file__).resolve().parents[1]


def compose_command() -> list[str] | None:
    """从 PATH 查找 Compose v2：优先 docker compose 插件，其次独立的 docker-compose。"""
    docker = shutil.which("docker")
    if docker:
        probe = subprocess.run(
            [docker, "compose", "version"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, check=False,
        )
        if probe.returncode == 0:
            return [docker, "compose"]
    standalone = shutil.which("docker-compose")
    return [standalone] if standalone else None


class ContainerHealthRemovalTests(unittest.TestCase):
    def run_install_tail(self, status: str) -> subprocess.CompletedProcess[str]:
        text = (ROOT / "payload/remote_install.sh").read_text(encoding="utf-8")
        tail = text[text.index('log "构建 Telegram') :]
        prelude = '''set -euo pipefail
log() { :; }
fail() { printf '%s\\n' "$*" >&2; exit 1; }
sleep() { :; }
docker() {
  if [[ "$1" == "inspect" ]]; then
    if [[ "$*" == *'.State.Health'* ]]; then
      printf 'unhealthy\\n'
    else
      printf '%s\\n' "$TEST_CONTAINER_STATUS"
    fi
  fi
}
DESTINATION_LABEL=115
DESTINATION_TYPE=clouddrive2
DEPLOY_CLOUDDRIVE2=false
INSTALL_DIR=/opt/tg115
'''
        return subprocess.run(
            [shutil.which("bash") or "bash", "-c", prelude + tail],
            env={**os.environ, "TEST_CONTAINER_STATUS": status},
            capture_output=True, text=True, encoding="utf-8", timeout=15,
            check=False,
        )

    def test_running_container_with_unhealthy_probe_does_not_block_install(self) -> None:
        result = self.run_install_tail("running")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TG115_RESULT=SUCCESS", result.stdout)
        self.assertNotIn("TG115_BOT_HEALTH", result.stdout)

    def test_exited_container_is_reported_as_start_failure(self) -> None:
        result = self.run_install_tail("exited")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("TG115_RESULT=SUCCESS", result.stdout)
        self.assertIn("未启动", result.stderr)

    def test_webdav_verification_ignores_container_health_status(self) -> None:
        for destination in ("clouddrive2", "gdrive"):
            with self.subTest(destination=destination):
                app = installer.InstallerApp.__new__(installer.InstallerApp)
                app._snapshot = Mock(return_value={
                    "destination_type": destination, "install_dir": "/opt/tg115",
                })
                app._validate_connection = Mock()
                app._run_worker = lambda action: action()
                app._log = Mock()
                app._show_info = Mock()
                session = Mock()
                session.run.side_effect = [
                    (0, "0\n"),
                    (0, "BOT_HEALTH=unhealthy\nTG115_DESTINATION=OK\n"),
                ]
                app._new_session = Mock(return_value=session)
                app.verify()
                app._show_info.assert_called_once()
                command = session.run.call_args_list[-1].args[0]
                self.assertNotIn(".State.Health", command)
                self.assertIn("python -m app.verify_destination", command)
                self.assertNotIn("容器健康", app._show_info.call_args.args[1])
                session.close.assert_called_once()

    def test_failed_webdav_verification_still_fails(self) -> None:
        app = installer.InstallerApp.__new__(installer.InstallerApp)
        app._snapshot = Mock(return_value={"install_dir": "/opt/tg115"})
        app._validate_connection = Mock()
        app._run_worker = lambda action: action()
        app._log = Mock()
        app._show_info = Mock()
        session = Mock()
        session.run.side_effect = [(0, "0\n"), (1, "WebDAV write failed")]
        app._new_session = Mock(return_value=session)
        with self.assertRaises(RuntimeError):
            app.verify()
        app._show_info.assert_not_called()
        session.close.assert_called_once()

    def test_compose_disables_bot_healthcheck(self) -> None:
        compose = compose_command()
        if compose is None:
            self.skipTest("PATH 中没有 docker compose 或 docker-compose")
        # compose 要求 env_file 存在；在临时目录里放一份空 .env，不依赖工作区里的真实配置。
        with tempfile.TemporaryDirectory(prefix="tg115-compose-") as temp:
            workdir = Path(temp)
            shutil.copyfile(ROOT / "payload/docker-compose.yml", workdir / "docker-compose.yml")
            (workdir / ".env").write_text("", encoding="utf-8")
            result = subprocess.run(
                [*compose, "-f", str(workdir / "docker-compose.yml"),
                 "config", "--format", "json", "--no-env-resolution"],
                capture_output=True, text=True, encoding="utf-8", timeout=15,
                check=True,
            )
        bot = json.loads(result.stdout)["services"]["tg115-bot"]
        self.assertEqual(bot["healthcheck"], {"disable": True})


if __name__ == "__main__":
    unittest.main()
