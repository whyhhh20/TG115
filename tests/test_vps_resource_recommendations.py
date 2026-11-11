"""安装资源预检回归：低内存/低磁盘不得拦截安装。

这些测试直接执行 payload/remote_install.sh 里真实的预检区域（trap、root/OS/架构/
路径检查、资源探测），只把绝对路径重定向到沙箱、并用命令替身替换 id/uname/df，
因此既不会调用真实 apt-get、不会写 /etc，也不会连接任何 VPS。

断言的是"控制流是否抵达安装阶段"（由测试自己打印的标记证明），
不是"真实部署成功"——真实部署需要 apt/docker/网络，测试里不存在。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "payload" / "remote_install.sh"
BASH = shutil.which("bash") or "bash"

PRECHECK_START = 'trap \'fail "安装在第'
PRECHECK_END = "export DEBIAN_FRONTEND=noninteractive"
LOG_FAIL_START = "log() {"
LOG_FAIL_END = "# 只读取 KEY=VALUE"
INSTALL_TAIL_START = 'log "构建 Telegram'

REACHED = "TG115_TEST_REACHED_INSTALL_STAGE"
KIB_PER_MIB = 1024
GIB_KB = 1024 * 1024


def script_source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def section(text: str, start: str, end: str) -> str:
    begin = text.index(start)
    stop = text.index(end, begin)
    return text[begin:stop]


def log_fail_helpers(text: str) -> str:
    return section(text, LOG_FAIL_START, LOG_FAIL_END)


def sandbox_path_export(bin_dir: Path) -> str:
    """在 bash 内部把命令替身目录放到 PATH 最前面。

    不能在 Python 里用 ":" 拼 PATH：Windows 的 PATH 以 ";" 分隔、且含 "C:" 盘符，
    Git Bash 会把拼出来的字符串拆错，替身失效。交给 bash 自己处理：Git Bash
    下用 cygpath 把 C:/... 转成 /c/...，Linux 下没有 cygpath，原样使用。
    """
    raw = bin_dir.as_posix().replace("'", "'\\''")
    return (
        f"TG115_TEST_BIN='{raw}'\n"
        'if command -v cygpath >/dev/null 2>&1; then\n'
        '  TG115_TEST_BIN="$(cygpath -u "$TG115_TEST_BIN")"\n'
        "fi\n"
        'export PATH="$TG115_TEST_BIN:$PATH"\n'
    )


class Sandbox:
    """一个只存在于临时目录里的"VPS"：假 os-release、假 meminfo、命令替身。"""

    def __init__(
        self,
        root: Path,
        *,
        mem_total_mb: int = 4096,
        disk_total_kb: int = 52_428_800,
        disk_free_kb: int = 30_000_000,
        uid: int = 0,
        os_id: str = "ubuntu",
        codename: str = "jammy",
        arch: str = "x86_64",
        df_fails: bool = False,
        meminfo_mode: str = "normal",
        source_exists: bool = True,
        install_dir: str = "/opt/tg115",
    ) -> None:
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir(parents=True, exist_ok=True)
        self.os_release = root / "os-release"
        self.meminfo = root / "meminfo"
        self.source_dir = root / "source"
        self.config_file = root / "tg115.env"
        self.install_dir = install_dir
        self.env = {
            "TEST_UID": str(uid),
            "TEST_ARCH": arch,
            "TEST_DISK_TOTAL_KB": str(disk_total_kb),
            "TEST_DISK_FREE_KB": str(disk_free_kb),
            "TEST_DF_FAIL": "1" if df_fails else "0",
        }

        lines = [f"ID={os_id}"]
        if codename:
            lines.append(f"VERSION_CODENAME={codename}")
        self.os_release.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

        if meminfo_mode == "normal":
            self.meminfo.write_text(
                f"MemTotal:       {mem_total_mb * KIB_PER_MIB} kB\n"
                "MemFree:        100000 kB\n",
                encoding="utf-8", newline="\n",
            )
        elif meminfo_mode == "garbage":
            # 文件存在但没有 MemTotal 行：awk 输出空值。
            self.meminfo.write_text("MemFree:        100000 kB\n", encoding="utf-8", newline="\n")
        elif meminfo_mode == "missing":
            pass
        else:
            raise AssertionError(f"未知 meminfo 模式：{meminfo_mode}")

        if source_exists:
            self.source_dir.mkdir(parents=True, exist_ok=True)
        self.config_file.write_text("DESTINATION_TYPE=clouddrive2\n", encoding="utf-8", newline="\n")

        self._write_double("id", 'printf \'%s\\n\' "${TEST_UID}"')
        self._write_double("uname", 'printf \'%s\\n\' "${TEST_ARCH}"')
        self._write_double(
            "df",
            'if [[ "${TEST_DF_FAIL}" == "1" ]]; then\n'
            '  printf \'df: cannot read table of mounted file systems\\n\' >&2\n'
            '  exit 1\n'
            "fi\n"
            "printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n'\n"
            'printf \'/dev/vda1 %s 0 %s 1%% /\\n\' "${TEST_DISK_TOTAL_KB}" "${TEST_DISK_FREE_KB}"\n',
        )
        self._write_double("apt-get", 'printf \'APT_GET_DOUBLE %s\\n\' "$*" >> "${TEST_CALL_LOG}"')

    def _write_double(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8", newline="\n")
        path.chmod(0o755)

    def isolate(self, snippet: str) -> str:
        """把脚本里的绝对系统路径重定向到沙箱，其余逻辑保持原样。"""
        return (
            snippet.replace("/etc/os-release", self.os_release.as_posix())
            .replace("/proc/meminfo", self.meminfo.as_posix())
        )


class PrecheckHarness:
    """执行真实预检区域，并在其后打印"已抵达安装阶段"标记。"""

    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox
        self.call_log = sandbox.root / "calls.log"
        self.call_log.write_text("", encoding="utf-8")

    def build(self) -> Path:
        text = script_source()
        precheck = section(text, PRECHECK_START, PRECHECK_END)
        harness = (
            "set -Eeuo pipefail\numask 077\n"
            + sandbox_path_export(self.sandbox.bin)
            + log_fail_helpers(text)
            + f'SOURCE_DIR="{self.sandbox.source_dir.as_posix()}"\n'
            + f'CONFIG_FILE="{self.sandbox.config_file.as_posix()}"\n'
            + f'INSTALL_DIR="{self.sandbox.install_dir}"\n'
            + "DEPLOY_CLOUDDRIVE2=false\n"
            + self.sandbox.isolate(precheck)
            + f"\nprintf '{REACHED}\\n'\n"
        )
        path = self.sandbox.root / "precheck.sh"
        path.write_text(harness, encoding="utf-8", newline="\n")
        return path

    def run(self) -> subprocess.CompletedProcess[str]:
        script = self.build()
        env = dict(os.environ)
        env["TEST_CALL_LOG"] = self.call_log.as_posix()
        env.update(self.sandbox.env)
        return subprocess.run(
            [BASH, str(script)],
            env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60, check=False,
        )


class InstallPrecheckSandboxTests(unittest.TestCase):
    def run_precheck(self, **kwargs) -> tuple[subprocess.CompletedProcess[str], Sandbox]:
        with tempfile.TemporaryDirectory() as temp:
            sandbox = Sandbox(Path(temp), **kwargs)
            result = PrecheckHarness(sandbox).run()
        return result, sandbox

    def assert_reached_install_stage(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, f"stdout={result.stdout}\nstderr={result.stderr}")
        self.assertIn(REACHED, result.stdout)
        self.assertNotIn("TG115_RESULT=FAILED", result.stderr)
        # 预检区域跑完就停：测试从不声称真实部署成功。
        self.assertNotIn("TG115_RESULT=SUCCESS", result.stdout)

    def assert_blocked_before_install(self, result: subprocess.CompletedProcess[str], message: str) -> None:
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(REACHED, result.stdout)
        self.assertIn("TG115_RESULT=FAILED", result.stderr)
        self.assertIn(message, result.stderr)

    # --- 低资源必须放行 -------------------------------------------------
    def test_921mb_memory_from_screenshot_reaches_install_stage(self) -> None:
        result, _ = self.run_precheck(mem_total_mb=921, disk_total_kb=20 * GIB_KB, disk_free_kb=15 * GIB_KB)
        self.assert_reached_install_stage(result)

    def test_512mb_memory_reaches_install_stage(self) -> None:
        result, _ = self.run_precheck(mem_total_mb=512, disk_total_kb=20 * GIB_KB, disk_free_kb=15 * GIB_KB)
        self.assert_reached_install_stage(result)

    def test_ample_memory_and_disk_reaches_install_stage(self) -> None:
        result, _ = self.run_precheck(mem_total_mb=4096, disk_total_kb=80 * GIB_KB, disk_free_kb=60 * GIB_KB)
        self.assert_reached_install_stage(result)

    def test_20gb_disk_with_less_than_old_8gb_free_reaches_install_stage(self) -> None:
        # 20GB 总盘低于旧"推荐 50GB"、3GB 空闲低于旧"硬性 8GB"。
        result, _ = self.run_precheck(mem_total_mb=2048, disk_total_kb=20 * GIB_KB, disk_free_kb=3 * GIB_KB)
        self.assert_reached_install_stage(result)

    # --- 探测异常不得变成安装门槛 ---------------------------------------
    def test_missing_meminfo_does_not_block_install(self) -> None:
        result, _ = self.run_precheck(meminfo_mode="missing")
        self.assert_reached_install_stage(result)

    def test_meminfo_without_memtotal_does_not_block_install(self) -> None:
        result, _ = self.run_precheck(meminfo_mode="garbage")
        self.assert_reached_install_stage(result)

    def test_failing_df_probe_does_not_block_install(self) -> None:
        result, _ = self.run_precheck(df_fails=True)
        self.assert_reached_install_stage(result)

    # --- 不依赖主机真实资源 ---------------------------------------------
    def test_probe_inputs_come_from_sandbox_not_host(self) -> None:
        text = script_source()
        precheck = section(text, PRECHECK_START, PRECHECK_END)
        self.assertIn("/proc/meminfo", precheck)
        self.assertIn("/etc/os-release", precheck)
        with tempfile.TemporaryDirectory() as temp:
            sandbox = Sandbox(Path(temp), mem_total_mb=921)
            isolated = sandbox.isolate(precheck)
            self.assertNotIn("/proc/meminfo", isolated)
            self.assertNotIn("/etc/os-release", isolated)
            self.assertIn(sandbox.meminfo.as_posix(), isolated)
            self.assertIn(sandbox.os_release.as_posix(), isolated)
            self.assertIn(
                f"MemTotal:       {921 * KIB_PER_MIB} kB",
                sandbox.meminfo.read_text(encoding="utf-8"),
            )
            result = PrecheckHarness(sandbox).run()
        self.assert_reached_install_stage(result)

    # --- 必需检查必须保留 -----------------------------------------------
    def test_non_root_is_still_rejected(self) -> None:
        result, _ = self.run_precheck(uid=1000)
        self.assert_blocked_before_install(result, "必须以 root 或 sudo 运行")

    def test_unsupported_os_is_still_rejected(self) -> None:
        result, _ = self.run_precheck(os_id="centos")
        self.assert_blocked_before_install(result, "目前只自动支持 Ubuntu 或 Debian")

    def test_missing_version_codename_is_still_rejected(self) -> None:
        result, _ = self.run_precheck(codename="")
        self.assert_blocked_before_install(result, "VERSION_CODENAME")

    def test_missing_source_dir_is_still_rejected(self) -> None:
        result, _ = self.run_precheck(source_exists=False)
        self.assert_blocked_before_install(result, "找不到部署源目录")

    def test_unsafe_install_dir_is_still_rejected(self) -> None:
        result, _ = self.run_precheck(install_dir="/tmp/evil")
        self.assert_blocked_before_install(result, "安装目录必须是 /opt/ 下的安全绝对路径")

    def test_unsupported_arch_is_still_rejected(self) -> None:
        result, _ = self.run_precheck(arch="riscv64")
        self.assert_blocked_before_install(result, "当前 CPU 架构暂不支持")

    def test_precheck_does_not_run_real_apt_get(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            sandbox = Sandbox(Path(temp), mem_total_mb=921)
            harness = PrecheckHarness(sandbox)
            result = harness.run()
            calls = harness.call_log.read_text(encoding="utf-8")
        self.assert_reached_install_stage(result)
        # 预检区域不含 apt-get；即使含，命中的也只是替身。
        self.assertEqual(calls, "")


class InstallTailContainerGateTests(unittest.TestCase):
    """容器启动失败仍必须让安装失败（不能被"删门槛"顺手删掉）。"""

    def run_install_tail(self, status: str) -> subprocess.CompletedProcess[str]:
        text = script_source()
        tail = text[text.index(INSTALL_TAIL_START):]
        prelude = (
            "set -euo pipefail\n"
            + log_fail_helpers(text)
            + "sleep() { :; }\n"
            + "docker() {\n"
            + "  if [[ \"$1\" == \"inspect\" ]]; then\n"
            + "    printf '%s\\n' \"${TEST_CONTAINER_STATUS}\"\n"
            + "  fi\n"
            + "  return 0\n"
            + "}\n"
            + "DESTINATION_LABEL=115\nDESTINATION_TYPE=clouddrive2\n"
            + "DEPLOY_CLOUDDRIVE2=false\nINSTALL_DIR=/opt/tg115\n"
        )
        return subprocess.run(
            [BASH, "-c", prelude + tail],
            env={**os.environ, "TEST_CONTAINER_STATUS": status},
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, check=False,
        )

    def test_exited_container_still_fails_install(self) -> None:
        result = self.run_install_tail("exited")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("TG115_RESULT=SUCCESS", result.stdout)
        self.assertIn("未启动", result.stderr)

    def test_running_container_still_reaches_success_marker(self) -> None:
        result = self.run_install_tail("running")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TG115_RESULT=SUCCESS", result.stdout)


class InstallTailStabilityWindowTests(unittest.TestCase):
    """up -d 之后要观察一段时间：反复重启的 Bot 不能被报成部署成功。"""

    # 每轮观察调用 docker inspect 的次数：先读一次起始重启次数，再检查 5 次。
    INSPECTS_PER_WINDOW = 6

    def run_with_states(
        self, states: list[str], *, deploy_clouddrive2: bool = False
    ) -> subprocess.CompletedProcess[str]:
        """docker inspect 依次返回 states 里的每一行，用完后一直返回最后一行。

        修复脚本被替换成只记一笔事件的 bash 函数，事件顺序写入 self.events。
        """
        text = script_source()
        tail = text[text.index(INSTALL_TAIL_START):]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            states_file = root / "states"
            states_file.write_text("\n".join(states) + "\n", encoding="utf-8", newline="\n")
            counter = root / "counter"
            counter.write_text("0", encoding="utf-8", newline="\n")
            events = root / "events"
            events.write_text("", encoding="utf-8", newline="\n")
            deploy = "true" if deploy_clouddrive2 else "false"
            prelude = (
                "set -Eeuo pipefail\n"
                + log_fail_helpers(text)
                + "sleep() { :; }\n"
                + "docker() {\n"
                + "  if [[ \"$1\" == \"inspect\" ]]; then\n"
                + "    printf 'inspect\\n' >> \"$TEST_EVENTS\"\n"
                + "    local n total\n"
                + "    n=$(( $(<\"$TEST_COUNTER\") + 1 ))\n"
                + "    printf '%s' \"$n\" > \"$TEST_COUNTER\"\n"
                + "    total=$(awk 'END {print NR}' \"$TEST_STATES\")\n"
                + "    (( n <= total )) || n=$total\n"
                + "    awk -v n=\"$n\" 'NR == n {print; exit}' \"$TEST_STATES\"\n"
                + "  fi\n"
                + "  return 0\n"
                + "}\n"
                + "bash() { printf 'repair %s\\n' \"$*\" >> \"$TEST_EVENTS\"; }\n"
                + "DESTINATION_LABEL=115\nDESTINATION_TYPE=clouddrive2\n"
                + f"DEPLOY_CLOUDDRIVE2={deploy}\nINSTALL_DIR=/opt/tg115\n"
            )
            result = subprocess.run(
                [BASH, "-c", prelude + tail],
                env={
                    **os.environ,
                    "TEST_STATES": states_file.as_posix(),
                    "TEST_COUNTER": counter.as_posix(),
                    "TEST_EVENTS": events.as_posix(),
                },
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=60, check=False,
            )
            self.events = events.read_text(encoding="utf-8").splitlines()
            return result

    def test_stable_running_container_succeeds(self) -> None:
        result = self.run_with_states(["running 0"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TG115_RESULT=SUCCESS", result.stdout)
        self.assertIn("TG115_BOT_STATUS=running", result.stdout)

    def test_restart_count_increase_fails_install(self) -> None:
        result = self.run_with_states(["running 0", "running 0", "running 1"])
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("TG115_RESULT=SUCCESS", result.stdout)
        self.assertIn("反复重启", result.stderr)
        self.assertIn("TG115_RESULT=FAILED", result.stderr)

    def test_restarting_status_fails_install(self) -> None:
        result = self.run_with_states(["running 0", "restarting 1"])
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("TG115_RESULT=SUCCESS", result.stdout)
        self.assertIn("未启动", result.stderr)
        self.assertIn("restarting", result.stderr)

    def test_container_that_exits_during_window_fails_install(self) -> None:
        result = self.run_with_states(["running 0", "running 0", "running 0", "exited 0"])
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("TG115_RESULT=SUCCESS", result.stdout)
        self.assertIn("未启动", result.stderr)

    def test_final_container_is_checked_after_network_repair(self) -> None:
        result = self.run_with_states(["running 0"], deploy_clouddrive2=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TG115_RESULT=SUCCESS", result.stdout)
        repair = self.events.index(
            "repair /opt/tg115/repair_clouddrive_network.sh --network-only"
        )
        self.assertEqual(repair, self.INSPECTS_PER_WINDOW)
        self.assertEqual(
            self.events[repair + 1:], ["inspect"] * self.INSPECTS_PER_WINDOW
        )
        self.assertIn("网络修复后", result.stderr + result.stdout)

    def test_container_recreated_by_repair_that_crash_loops_fails_install(self) -> None:
        stable_first_window = ["running 0"] * self.INSPECTS_PER_WINDOW
        result = self.run_with_states(
            stable_first_window + ["running 0", "running 0", "running 2"],
            deploy_clouddrive2=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("TG115_RESULT=SUCCESS", result.stdout)
        self.assertIn("网络修复后，重启次数 0 → 2", result.stderr)
        self.assertIn("TG115_RESULT=FAILED", result.stderr)

    def test_container_recreated_by_repair_that_exits_fails_install(self) -> None:
        stable_first_window = ["running 0"] * self.INSPECTS_PER_WINDOW
        result = self.run_with_states(
            stable_first_window + ["exited 0"], deploy_clouddrive2=True
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("网络修复后，当前状态：exited", result.stderr)

    def test_without_clouddrive2_deploy_repair_is_skipped(self) -> None:
        result = self.run_with_states(["running 0"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events, ["inspect"] * self.INSPECTS_PER_WINDOW)


if __name__ == "__main__":
    unittest.main()
