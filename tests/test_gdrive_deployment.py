from __future__ import annotations

# Deployment-layer tests for the two destination choices.
#
# Both destinations (115 and Google Drive) are transported through CloudDrive2,
# so these tests assert the shared CD2 contract instead of a Google direct path.
# They intentionally do not import the Bot application: they exercise the shell
# scripts, the Compose file and the user-facing documents that this layer owns.
import base64
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
PAYLOAD = SOURCE / "payload"
DOCS = SOURCE / "docs"
INSTALL_SCRIPT = PAYLOAD / "remote_install.sh"
REPAIR_SCRIPT = PAYLOAD / "repair_clouddrive_network.sh"
MANAGE_SCRIPT = PAYLOAD / "manage.sh"
COMPOSE = PAYLOAD / "docker-compose.yml"

BASH = shutil.which("bash") or ""

IF_RE = re.compile(r"^\s*if\s+(.+?);\s*then\s*$")
FI_RE = re.compile(r"^\s*fi\s*$")
SKIP_LINE_RE = re.compile(r"^\s*(#|log\b|printf\b|echo\b)")


def b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def find_tool(name: str, env_var: str) -> str:
    """Locate a bundled local tool (ShellCheck / Compose) without touching the network."""
    candidates = [
        os.environ.get(env_var, ""),
        str(SOURCE / ".local" / "tools" / name),
        str(SOURCE / ".local" / "tools" / f"{name}.exe"),
        shutil.which(name) or "",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return ""


def run_bash(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


def check_config(config_text: str) -> subprocess.CompletedProcess[str]:
    """Run the installer's config-only validation against a temporary .env."""
    with tempfile.TemporaryDirectory(prefix="tg115-cfg-") as temp:
        path = Path(temp) / "config.env"
        path.write_bytes(config_text.encode("utf-8"))
        return run_bash([INSTALL_SCRIPT.as_posix(), "--check-config", path.as_posix()])


def cd2_config(
    destination: str | None = None,
    deploy: str = "true",
    target: str = "115/Telegram",
    extra: dict[str, str] | None = None,
    drop: tuple[str, ...] = (),
) -> str:
    values = {
        "TELEGRAM_API_ID": "12345",
        "CD2_WEBDAV_URL_B64": b64("http://clouddrive2:19798/dav"),
        "CD2_WEBDAV_USERNAME_B64": b64("webdav-user"),
        "CD2_WEBDAV_PASSWORD_B64": b64("webdav-pass"),
        "CD2_TARGET_PATH_B64": b64(target),
        "DEPLOY_CLOUDDRIVE2": deploy,
    }
    if destination is not None:
        values["DESTINATION_TYPE"] = destination
    if extra:
        values.update(extra)
    for key in drop:
        values.pop(key, None)
    return "\n".join(f"{key}={value}" for key, value in values.items()) + "\n"


def guards_by_line(text: str) -> list[tuple[str, ...]]:
    """Return the enclosing `if` conditions for every line of a shell script."""
    stack: list[str] = []
    result: list[tuple[str, ...]] = []
    for line in text.splitlines():
        result.append(tuple(stack))
        match = IF_RE.match(line)
        if match:
            stack.append(match.group(1))
        elif FI_RE.match(line) and stack:
            stack.pop()
    return result


def guarded_by(text: str, needle: str, guard: str) -> list[tuple[int, str]]:
    """Lines containing `needle` that are NOT inside a block guarded by `guard`."""
    lines = text.splitlines()
    guards = guards_by_line(text)
    unguarded: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        if needle not in line or SKIP_LINE_RE.match(line):
            continue
        if not any(guard in condition for condition in guards[index]):
            unguarded.append((index + 1, line.strip()))
    return unguarded


def compose_services(text: str) -> dict[str, list[str]]:
    services: dict[str, list[str]] = {}
    in_services = False
    current: str | None = None
    for line in text.splitlines():
        if re.match(r"^services:\s*$", line):
            in_services = True
            continue
        if in_services and re.match(r"^\S", line):
            in_services = False
            current = None
            continue
        if not in_services:
            continue
        match = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", line)
        if match is not None:
            name = match.group(1)
            current = name
            services[name] = []
            continue
        if current is not None:
            services[current].append(line)
    return services


@unittest.skipUnless(BASH, "需要 bash 才能执行部署脚本校验")
class DestinationConfigTests(unittest.TestCase):
    def test_missing_destination_defaults_to_clouddrive2(self) -> None:
        result = check_config(cd2_config())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TG115_CONFIG=OK", result.stdout)
        self.assertIn("TG115_DESTINATION_TYPE=clouddrive2", result.stdout)
        self.assertIn("TG115_DESTINATION_LABEL=115", result.stdout)
        self.assertIn("TG115_DEPLOY_CLOUDDRIVE2=true", result.stdout)

    def test_empty_destination_value_defaults_to_clouddrive2(self) -> None:
        result = check_config(cd2_config(destination=""))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TG115_DESTINATION_TYPE=clouddrive2", result.stdout)

    def test_115_alias_normalizes_to_clouddrive2(self) -> None:
        result = check_config(cd2_config(destination="115"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TG115_DESTINATION_TYPE=clouddrive2", result.stdout)
        self.assertIn("TG115_DESTINATION_LABEL=115", result.stdout)

    def test_google_drive_alias_normalizes_to_gdrive(self) -> None:
        result = check_config(cd2_config(destination="google_drive"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TG115_DESTINATION_TYPE=gdrive", result.stdout)
        self.assertIn("TG115_DESTINATION_LABEL=Google Drive", result.stdout)

    def test_unknown_destination_is_rejected(self) -> None:
        result = check_config(cd2_config(destination="webdav"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("DESTINATION_TYPE", result.stderr)
        self.assertIn("webdav", result.stderr)
        self.assertIn("TG115_RESULT=FAILED", result.stderr)

    def test_gdrive_keeps_user_clouddrive2_choice(self) -> None:
        result = check_config(cd2_config(destination="gdrive", deploy="true"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TG115_DESTINATION_TYPE=gdrive", result.stdout)
        self.assertIn("TG115_DESTINATION_LABEL=Google Drive", result.stdout)
        self.assertIn("TG115_DEPLOY_CLOUDDRIVE2=true", result.stdout)
        self.assertNotIn("强制", result.stdout)

    def test_gdrive_can_reuse_existing_clouddrive2(self) -> None:
        result = check_config(cd2_config(destination="gdrive", deploy="false"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TG115_DESTINATION_TYPE=gdrive", result.stdout)
        self.assertIn("TG115_DEPLOY_CLOUDDRIVE2=false", result.stdout)

    def test_clouddrive2_can_reuse_existing_clouddrive2(self) -> None:
        result = check_config(cd2_config(destination="clouddrive2", deploy="false"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TG115_DESTINATION_TYPE=clouddrive2", result.stdout)
        self.assertIn("TG115_DEPLOY_CLOUDDRIVE2=false", result.stdout)

    def test_gdrive_requires_cd2_webdav_credentials(self) -> None:
        result = check_config(
            cd2_config(destination="gdrive", drop=("CD2_WEBDAV_PASSWORD_B64",))
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("CD2_WEBDAV_PASSWORD_B64", result.stderr)
        self.assertIn("TG115_RESULT=FAILED", result.stderr)

    def test_clouddrive2_requires_cd2_webdav_credentials(self) -> None:
        result = check_config(cd2_config(drop=("CD2_WEBDAV_USERNAME_B64",)))
        self.assertEqual(result.returncode, 1)
        self.assertIn("CD2_WEBDAV_USERNAME_B64", result.stderr)

    def test_gdrive_requires_cd2_webdav_url(self) -> None:
        result = check_config(cd2_config(destination="gdrive", drop=("CD2_WEBDAV_URL_B64",)))
        self.assertEqual(result.returncode, 1)
        self.assertIn("CD2_WEBDAV_URL_B64", result.stderr)

    def test_gdrive_does_not_require_google_oauth(self) -> None:
        result = check_config(cd2_config(destination="gdrive"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("GDRIVE", result.stderr)
        self.assertNotIn("GDRIVE", result.stdout)

    def test_legacy_google_token_only_config_is_rejected(self) -> None:
        # 旧误实现只写 Google token；现在必须要求 CD2，且不得回退到直连。
        config = (
            "DESTINATION_TYPE=gdrive\n"
            f"GDRIVE_TOKEN_B64={b64('{\"access_token\":\"a\",\"refresh_token\":\"r\"}')}\n"
            "DEPLOY_CLOUDDRIVE2=false\n"
        )
        result = check_config(config)
        self.assertEqual(result.returncode, 1)
        self.assertIn("CD2_WEBDAV_URL_B64", result.stderr)

    def test_invalid_cd2_target_base64_is_rejected(self) -> None:
        result = check_config(
            cd2_config(destination="gdrive", extra={"CD2_TARGET_PATH_B64": "***"})
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("CD2_TARGET_PATH_B64", result.stderr)
        self.assertIn("Base64", result.stderr)

    def test_empty_cd2_target_path_is_allowed(self) -> None:
        result = check_config(cd2_config(destination="gdrive", target=""))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TG115_CONFIG=OK", result.stdout)

    def test_unknown_deploy_flag_is_rejected(self) -> None:
        result = check_config(cd2_config(destination="gdrive", deploy="yes"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("DEPLOY_CLOUDDRIVE2", result.stderr)

    def test_crlf_config_is_accepted(self) -> None:
        result = check_config(cd2_config(destination="gdrive").replace("\n", "\r\n"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("\r", result.stdout)

    def test_missing_config_fails_before_root_check(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tg115-cfg-") as temp:
            missing = (Path(temp) / "absent.env").as_posix()
            result = run_bash([INSTALL_SCRIPT.as_posix(), "--check-config", missing])
        self.assertEqual(result.returncode, 1)
        self.assertIn("找不到配置文件", result.stderr)

    def test_no_arguments_does_not_require_root(self) -> None:
        result = run_bash([INSTALL_SCRIPT.as_posix()])
        self.assertEqual(result.returncode, 1)
        self.assertIn("找不到配置文件", result.stderr)


@unittest.skipUnless(BASH, "需要 bash 才能执行 manage.sh")
class ManageScriptTests(unittest.TestCase):
    def run_manage(self, env_text: str, command: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="tg115-manage-") as temp:
            install_dir = Path(temp)
            (install_dir / ".env").write_text(env_text, encoding="utf-8")
            return subprocess.run(
                [BASH, MANAGE_SCRIPT.as_posix(), command],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                check=False,
                env={
                    **os.environ,
                    "INSTALL_DIR": install_dir.as_posix(),
                    "PYTHONIOENCODING": "utf-8",
                },
            )

    def test_destination_reports_gdrive_with_shared_cd2_path(self) -> None:
        result = self.run_manage(
            "DESTINATION_TYPE=gdrive\n"
            f"CD2_TARGET_PATH_B64={b64('GoogleDrive/Telegram')}\n"
            "DEPLOY_CLOUDDRIVE2=true\n",
            "destination",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DESTINATION_TYPE=gdrive", result.stdout)
        self.assertIn("目标网盘：Google Drive", result.stdout)
        self.assertIn("CloudDrive2 WebDAV 相对路径：GoogleDrive/Telegram", result.stdout)
        self.assertIn("cd2:", result.stdout)
        self.assertIn("DEPLOY_CLOUDDRIVE2=true", result.stdout)
        self.assertNotIn("gdrive:", result.stdout)

    def test_destination_reports_115(self) -> None:
        result = self.run_manage(
            f"CD2_TARGET_PATH_B64={b64('115/Telegram')}\n", "destination"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DESTINATION_TYPE=clouddrive2", result.stdout)
        self.assertIn("目标网盘：115", result.stdout)
        self.assertIn("CloudDrive2 WebDAV 相对路径：115/Telegram", result.stdout)
        self.assertIn("cd2:", result.stdout)

    def test_destination_accepts_115_alias(self) -> None:
        result = self.run_manage("DESTINATION_TYPE=115\n", "destination")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DESTINATION_TYPE=clouddrive2", result.stdout)
        self.assertIn("目标网盘：115", result.stdout)

    def test_destination_without_target_mentions_webdav_root(self) -> None:
        result = self.run_manage("DESTINATION_TYPE=gdrive\n", "destination")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WebDAV 根目录", result.stdout)

    def test_unknown_destination_is_rejected(self) -> None:
        result = self.run_manage("DESTINATION_TYPE=onedrive\n", "destination")
        self.assertEqual(result.returncode, 1)
        self.assertIn("DESTINATION_TYPE", result.stderr)

    def test_usage_mentions_destination(self) -> None:
        result = self.run_manage("DESTINATION_TYPE=clouddrive2\n", "no-such-command")
        self.assertEqual(result.returncode, 2)
        self.assertIn("destination", result.stderr)


class ScriptStructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.install = read(INSTALL_SCRIPT)
        self.repair = read(REPAIR_SCRIPT)
        self.manage = read(MANAGE_SCRIPT)

    def test_fuse_dependency_is_guarded_by_clouddrive2_flag(self) -> None:
        for needle in ("fuse3", "kmod", "modprobe fuse", "/dev/fuse"):
            with self.subTest(needle=needle):
                unguarded = guarded_by(self.install, needle, "DEPLOY_CLOUDDRIVE2")
                self.assertEqual(unguarded, [], f"{needle} 未受 DEPLOY_CLOUDDRIVE2 保护")

    def test_clouddrive2_directories_are_guarded(self) -> None:
        unguarded = guarded_by(self.install, "clouddrive/config", "DEPLOY_CLOUDDRIVE2")
        self.assertEqual(unguarded, [])

    def test_repair_script_invocation_is_guarded(self) -> None:
        # 只要求“执行修复脚本”受保护；复制/删除脚本文件本身与启动 CloudDrive2 无关。
        unguarded = guarded_by(
            self.install,
            'bash "$INSTALL_DIR/repair_clouddrive_network.sh"',
            "DEPLOY_CLOUDDRIVE2",
        )
        self.assertEqual(unguarded, [])

    def test_cd2_webdav_requirements_are_not_destination_specific(self) -> None:
        for field in (
            "require_b64 CD2_WEBDAV_URL_B64",
            "require_b64 CD2_WEBDAV_USERNAME_B64",
            "require_b64 CD2_WEBDAV_PASSWORD_B64",
        ):
            with self.subTest(field=field):
                self.assertIn(field, self.install)
        # 两条路线共用同一段校验，不允许再出现按网盘分支的条件。
        self.assertNotIn('if [[ "$DESTINATION_TYPE"', self.install)
        self.assertNotIn('if [[ "$DESTINATION_TYPE"', self.repair)

    def test_google_oauth_is_gone_from_scripts(self) -> None:
        for name, text in (
            ("remote_install.sh", self.install),
            ("repair_clouddrive_network.sh", self.repair),
            ("manage.sh", self.manage),
        ):
            with self.subTest(script=name):
                self.assertNotIn("GDRIVE_", text)
                self.assertNotIn("rclone authorize", text)

    def test_gdrive_is_not_forced_to_disable_clouddrive2(self) -> None:
        self.assertNotIn("DEPLOY_CLOUDDRIVE2 false", self.install)
        self.assertNotIn("必须使用 DEPLOY_CLOUDDRIVE2=false", self.install)
        self.assertNotIn("不需要 CloudDrive2", self.install)
        self.assertNotIn("不需要 CloudDrive2", self.repair)

    def test_repair_script_handles_both_destinations(self) -> None:
        self.assertIn("DESTINATION_TYPE", self.repair)
        self.assertIn("google_drive", self.repair)
        self.assertIsNone(
            re.search(r"gdrive\)\s*\n\s*fail", self.repair),
            "修复脚本不应再拒绝 Google Drive 目标",
        )

    def test_install_script_writes_destination_and_deploy_flag_to_env(self) -> None:
        self.assertIn('set_env_value DESTINATION_TYPE "$DESTINATION_TYPE"', self.install)
        self.assertIn('set_env_value DEPLOY_CLOUDDRIVE2 "$DEPLOY_CLOUDDRIVE2"', self.install)

    def test_clouddrive2_compose_calls_use_profile(self) -> None:
        offenders = [
            (index + 1, line.strip())
            for index, line in enumerate(self.install.splitlines())
            if "docker compose" in line
            and "clouddrive2" in line
            and "--profile clouddrive2" not in line
        ]
        self.assertEqual(offenders, [], "启动/拉取 CloudDrive2 必须显式启用 profile")

    def test_repair_script_compose_calls_use_profile(self) -> None:
        offenders = [
            (index + 1, line.strip())
            for index, line in enumerate(self.repair.splitlines())
            if "docker compose" in line
            and "clouddrive2" in line
            and "--profile clouddrive2" not in line
        ]
        self.assertEqual(offenders, [])

    def test_manage_script_validates_destination(self) -> None:
        self.assertIn("DESTINATION_TYPE", self.manage)
        self.assertIn("destination)", self.manage)
        self.assertIn("clouddrive2|115", self.manage)
        self.assertIn("gdrive|google_drive", self.manage)

    def test_manage_script_reports_shared_cd2_remote(self) -> None:
        self.assertIn("rclone remote: cd2:", self.manage)
        self.assertNotIn("gdrive:", self.manage)


@unittest.skipUnless(BASH, "需要 bash 才能执行 .env 改写逻辑")
class EnvRewriteTests(unittest.TestCase):
    def test_set_env_value_rewrites_and_appends(self) -> None:
        text = read(INSTALL_SCRIPT)
        start = text.index("set_env_value() {")
        end = text.index("\n}\n", start) + len("\n}\n")
        function = text[start:end]
        self.assertIn("sed -i", function)
        with tempfile.TemporaryDirectory(prefix="tg115-env-") as temp:
            env_file = Path(temp) / ".env"
            env_file.write_text("A=1\nDEPLOY_CLOUDDRIVE2=false\n", encoding="utf-8")
            script = (
                "set -Eeuo pipefail\n"
                + function
                + f'\nset_env_value DESTINATION_TYPE gdrive "{env_file.as_posix()}"\n'
                + f'set_env_value DEPLOY_CLOUDDRIVE2 true "{env_file.as_posix()}"\n'
            )
            result = run_bash(["-c", script])
            self.assertEqual(result.returncode, 0, result.stderr)
            content = env_file.read_text(encoding="utf-8")
        self.assertIn("DESTINATION_TYPE=gdrive", content)
        self.assertIn("DEPLOY_CLOUDDRIVE2=true", content)
        self.assertNotIn("DEPLOY_CLOUDDRIVE2=false", content)
        self.assertIn("A=1", content)


class ComposeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = read(COMPOSE)
        self.services = compose_services(self.text)

    def test_expected_services_exist(self) -> None:
        self.assertIn("clouddrive2", self.services)
        self.assertIn("tg115-bot", self.services)

    def test_clouddrive2_is_behind_profile(self) -> None:
        block = "\n".join(self.services["clouddrive2"])
        self.assertRegex(block, r"profiles:\s*\[?\"?clouddrive2\"?\]?")

    def test_bot_is_not_behind_profile(self) -> None:
        block = "\n".join(self.services["tg115-bot"])
        self.assertNotIn("profiles:", block)

    def test_bot_has_no_fuse_or_privileges(self) -> None:
        block = "\n".join(self.services["tg115-bot"])
        self.assertNotIn("/dev/fuse", block)
        self.assertNotIn("privileged", block)
        self.assertIn("read_only: true", block)
        self.assertIn("cap_drop:", block)

    def test_clouddrive2_keeps_fuse_and_privileges(self) -> None:
        block = "\n".join(self.services["clouddrive2"])
        self.assertIn("/dev/fuse", block)
        self.assertIn("privileged: true", block)

    def test_both_services_cap_container_logs(self) -> None:
        # 20GB 硬盘上的最小配置不能让容器 json 日志无限增长。
        for name in ("clouddrive2", "tg115-bot"):
            with self.subTest(service=name):
                block = "\n".join(self.services[name])
                self.assertRegex(block, r"logging:\s*\n\s*driver:\s*json-file")
                self.assertRegex(block, r"max-size:\s*\"?5m\"?")
                self.assertRegex(block, r"max-file:\s*\"?2\"?")

    def test_no_google_credentials_in_compose(self) -> None:
        self.assertNotIn("GDRIVE", self.text)
        self.assertNotIn("access_token", self.text)
        self.assertNotIn("refresh_token", self.text)

    def test_compose_comments_describe_shared_clouddrive2(self) -> None:
        header = "\n".join(self.text.splitlines()[:4])
        self.assertIn("Google Drive", header)
        self.assertIn("CloudDrive2", header)
        self.assertIn("DEPLOY_CLOUDDRIVE2", header)


@unittest.skipUnless(BASH, "需要 bash 才能做语法检查")
class ShellSyntaxTests(unittest.TestCase):
    def test_scripts_parse(self) -> None:
        for script in (INSTALL_SCRIPT, REPAIR_SCRIPT, MANAGE_SCRIPT):
            with self.subTest(script=script.name):
                result = run_bash(["-n", script.as_posix()])
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_shellcheck_is_clean(self) -> None:
        shellcheck = find_tool("shellcheck", "TG115_SHELLCHECK")
        if not shellcheck:
            self.skipTest("未找到 ShellCheck 可执行文件")
        result = subprocess.run(
            [
                shellcheck,
                "-S",
                "warning",
                *[s.as_posix() for s in (INSTALL_SCRIPT, REPAIR_SCRIPT, MANAGE_SCRIPT)],
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class ComposeValidationTests(unittest.TestCase):
    def test_compose_config_is_valid(self) -> None:
        compose = find_tool("docker-compose", "TG115_DOCKER_COMPOSE")
        if not compose:
            self.skipTest("未找到 docker-compose 可执行文件")
        with tempfile.TemporaryDirectory(prefix="tg115-compose-") as temp:
            workdir = Path(temp)
            shutil.copyfile(COMPOSE, workdir / "docker-compose.yml")
            (workdir / ".env").write_text(
                "TZ=Asia/Shanghai\nTELEGRAM_API_ID=1\n", encoding="utf-8"
            )
            result = subprocess.run(
                [compose, "config", "--quiet"],
                cwd=workdir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class DocumentationTests(unittest.TestCase):
    def assert_contains_all(self, text: str, needles: list[str], label: str) -> None:
        for needle in needles:
            with self.subTest(document=label, needle=needle):
                self.assertIn(needle, text)

    def test_readme_covers_shared_clouddrive2(self) -> None:
        text = read(SOURCE / "README.md")
        self.assert_contains_all(
            text,
            [
                "两种目标网盘",
                "DESTINATION_TYPE",
                "Google Drive",
                "CloudDrive2 WebDAV",
                "CD2_TARGET_PATH_B64",
                "DEPLOY_CLOUDDRIVE2",
                "本地开发版，尚未发布",
            ],
            "README.md",
        )
        for stale in ("rclone authorize", "gdrive:", "Google Drive 直传", "不需要 CloudDrive2"):
            with self.subTest(stale=stale):
                self.assertNotIn(stale, text)

    def test_beginner_guide_mounts_netdisk_in_clouddrive2(self) -> None:
        text = read(DOCS / "README-小白使用说明.md")
        self.assert_contains_all(
            text,
            [
                "在 CloudDrive2 中挂载网盘",
                "Google Drive",
                "WebDAV 相对路径",
                "TG115_DESTINATION=OK",
                "不处理 Google token",
                "打开 CloudDrive2 管理页",
                "已持久化但未完成的任务绑定在原来的目标上",
                "`/dev/fuse`",
            ],
            "docs/README-小白使用说明.md",
        )
        self.assertNotIn("rclone authorize", text)

    def test_checklist_covers_both_destinations(self) -> None:
        text = read(DOCS / "填写信息清单.md")
        self.assert_contains_all(
            text,
            [
                "在 CloudDrive2 管理页登录并挂载本次选择的网盘",
                "WebDAV 用户名",
                "WebDAV 密码",
                "相对路径",
                "不需要 OAuth token",
                "`115`",
                "`Google Drive`",
            ],
            "docs/填写信息清单.md",
        )

    def test_review_report_separates_this_round_from_history(self) -> None:
        text = read(DOCS / "验收与复核报告.md")
        self.assert_contains_all(
            text,
            [
                "不代表本轮修正后的验证",
                "75 项自动化测试",
                "Google Drive（经 CloudDrive2，本地开发版）",
                "仍未验证",
                "TG115_DESTINATION_LABEL=Google Drive",
            ],
            "docs/验收与复核报告.md",
        )
        self.assertNotIn("286 项测试全部通过", text)

    def test_readme_documents_minimum_vps_profile(self) -> None:
        text = read(SOURCE / "README.md")
        self.assert_contains_all(
            text,
            [
                "最小配置（推荐）",
                "1 核 / 1GB / 20GB 硬盘",
                "宽裕配置",
                "6GB",
                "CloudDrive2 自己的缓存不计入 2GB",
                "流式传输",
            ],
            "README.md",
        )

    def test_beginner_guide_recommends_minimum_vps(self) -> None:
        text = read(DOCS / "README-小白使用说明.md")
        self.assert_contains_all(
            text,
            [
                "最小配置（推荐）",
                "1 核 CPU、1GB 内存、20GB 硬盘",
                "2GB",
                "6GB",
                "宽裕配置",
            ],
            "docs/README-小白使用说明.md",
        )
        self.assertNotIn("个人基础使用推荐 2 核 CPU、4GB 内存、50GB SSD", text)

    def test_checklist_defaults_to_minimum_budget_and_reserve(self) -> None:
        text = read(DOCS / "填写信息清单.md")
        self.assert_contains_all(
            text,
            [
                "最小配置（默认推荐）",
                "本地任务预算：`2GB`",
                "磁盘最少保留：`6GB`",
                "宽裕配置为 `20GB`",
            ],
            "docs/填写信息清单.md",
        )

    def test_documents_describe_transfer_log_cleanup(self) -> None:
        expectations = {
            SOURCE / "README.md": ["2MB", "5MB", "8MB"],
            SOURCE / "SECURITY.md": ["2MB", "5MB", "8MB"],
            DOCS / "README-小白使用说明.md": ["2MB", "5MB", "8MB"],
            DOCS / "验收与复核报告.md": ["2MB", "5MB", "8MB"],
        }
        for document, needles in expectations.items():
            text = read(document)
            with self.subTest(document=document.name):
                self.assertIn("transfers/<task_id>.log", text)
                self.assertIn("completed", text)
                self.assert_contains_all(text, needles, document.name)

    def test_documents_do_not_claim_real_vps_acceptance(self) -> None:
        documents = [SOURCE / "README.md", SOURCE / "CHANGELOG.md", SOURCE / "SECURITY.md"]
        documents.extend(sorted(DOCS.glob("*.md")))
        forbidden = (
            "真实 VPS 已验收",
            "已在真实 VPS 上完成端到端验收",
            "真实网盘已验收",
            "最小配置已通过真实 VPS 验证",
        )
        for document in documents:
            text = read(document)
            for phrase in forbidden:
                with self.subTest(document=document.name, phrase=phrase):
                    self.assertNotIn(phrase, text)

    def test_changelog_marks_unreleased_local_build(self) -> None:
        text = read(SOURCE / "CHANGELOG.md")
        self.assert_contains_all(
            text,
            [
                "本地开发版（未发布",
                "DESTINATION_TYPE",
                "Compose profile",
                "两条路线都经过 CloudDrive2",
                "tests/test_gdrive_deployment.py",
            ],
            "CHANGELOG.md",
        )

    def test_third_party_doc_describes_webdav_client(self) -> None:
        text = read(DOCS / "第三方组件说明.md")
        self.assert_contains_all(
            text,
            ["rclone", "CloudDrive2 WebDAV", "不配置 Google `drive` 后端", "不传输 Google OAuth token"],
            "docs/第三方组件说明.md",
        )
        self.assertNotIn("rclone authorize drive", text)

    def test_security_doc_has_no_google_token_handling(self) -> None:
        text = read(SOURCE / "SECURITY.md")
        self.assert_contains_all(
            text,
            ["CloudDrive2 WebDAV 地址、用户名和密码", "本程序不接触 Google OAuth token"],
            "SECURITY.md",
        )

    def test_documents_do_not_contain_real_tokens(self) -> None:
        token_patterns = (
            re.compile(r"ya29\.[0-9A-Za-z_-]{20,}"),
            re.compile(r"1//0[0-9A-Za-z_-]{20,}"),
        )
        documents = [SOURCE / "README.md", SOURCE / "CHANGELOG.md", SOURCE / "SECURITY.md"]
        documents.extend(sorted(DOCS.glob("*.md")))
        for document in documents:
            text = read(document)
            for pattern in token_patterns:
                with self.subTest(document=document.name, pattern=pattern.pattern):
                    self.assertIsNone(pattern.search(text))


if __name__ == "__main__":
    unittest.main()
