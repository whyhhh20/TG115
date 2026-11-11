"""部署日志窗口是纯文本：远端 docker compose 的 ANSI/终端控制序列不能原样显示。"""
from __future__ import annotations

import sys
import tkinter as tk
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))

import installer
from installer import InstallerApp, bounded_output_tail, strip_terminal_controls

ESC = "\x1b"
# 部署日志里实际出现过的片段：光标隐藏/显示、清行、上移、颜色。
COMPOSE_SAMPLES = {
    f"{ESC}[?25l[+] Building 0.0s (0/1){ESC}[?25h": "[+] Building 0.0s (0/1)",
    f"{ESC}[2K{ESC}[1A{ESC}[2K{ESC}[G ✔ Container tg115-bot  {ESC}[32mStarted{ESC}[0m":
        " ✔ Container tg115-bot  Started",
    f"{ESC}[1;31mError{ESC}[0m response from daemon": "Error response from daemon",
    f"{ESC}]0;docker compose{ESC}\\done": "done",
    f"{ESC}]2;title\x07ok": "ok",
    f"{ESC}(Bplain{ESC}=": "plain",
    "\x9b32mgreen\x9b0m": "green",
    "tab\tkept\x08\x07": "tab\tkept",
}


class StripTerminalControlsTests(unittest.TestCase):
    def test_compose_progress_samples_become_plain_text(self) -> None:
        for raw, expected in COMPOSE_SAMPLES.items():
            with self.subTest(raw=raw):
                self.assertEqual(strip_terminal_controls(raw), expected)

    def test_plain_text_and_chinese_are_untouched(self) -> None:
        text = "[TG115] 确认 Bot 容器已稳定运行（首次启动，观察约 15 秒）\n[2K is literal"
        self.assertEqual(strip_terminal_controls(text), text)

    def test_failure_dialog_tail_is_plain(self) -> None:
        output = f"{ESC}[31mfailed to solve{ESC}[0m\n{ESC}[?25h"
        self.assertEqual(bounded_output_tail(output), ("failed to solve",))


class DeployLogWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = InstallerApp(self.root)
        self.root.update()

    def tearDown(self) -> None:
        self.app.busy = False
        self.app._stop_ui_drain()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def log_text(self) -> str:
        return self.app.log.get("1.0", tk.END)

    def test_log_window_shows_no_control_sequences(self) -> None:
        for raw in COMPOSE_SAMPLES:
            self.app._log(raw)
        text = self.log_text()
        self.assertNotIn(ESC, text)
        self.assertNotIn("\x9b", text)
        self.assertNotIn("[?25h", text)
        self.assertNotIn("[32m", text)
        self.assertIn("✔ Container tg115-bot  Started", text)

    def test_secret_split_by_colour_codes_is_still_redacted(self) -> None:
        self.app._secrets = ["cd2-secret-value"]
        self.app._log(f"password cd2-{ESC}[1msecret{ESC}[0m-value")
        text = self.log_text()
        self.assertNotIn("cd2-secret-value", text)
        self.assertIn("***", text)


class RemoteInstallPlainProgressTests(unittest.TestCase):
    def test_remote_install_requests_plain_compose_output(self) -> None:
        script = (SOURCE / "payload" / "remote_install.sh").read_text(encoding="utf-8")
        self.assertIn("export COMPOSE_ANSI=never COMPOSE_PROGRESS=plain BUILDKIT_PROGRESS=plain", script)
        self.assertLess(
            script.index("COMPOSE_ANSI=never"), script.index("docker compose build")
        )

    def test_module_exposes_the_shared_stripper(self) -> None:
        self.assertIs(installer.strip_terminal_controls, strip_terminal_controls)


if __name__ == "__main__":
    unittest.main()
