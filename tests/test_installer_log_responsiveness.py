"""日志/队列响应性回归：大量 Docker/PTY 输出不得卡死 Tk 主线程。

真实症状（py-spy 抓取用户 EXE 主线程栈）：
    MainThread -> Text.see -> InstallerApp._log.append -> _drain_ui_queue -> after 回调
即主线程在一次 after 回调里逐行 insert+see 了海量日志，心跳与完成回调被饿死。

这里的测试全部使用 withdraw() 的隐藏窗口，只在本进程内压测，不接触用户窗口。
"""

from __future__ import annotations

import sys
import threading
import time
import tkinter as tk
import unittest
from itertools import pairwise
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))

import installer
from installer import InstallerApp


class LogResponsivenessTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = InstallerApp(self.root)
        self.root.update()
        self.insert_calls = 0
        self.see_calls = 0
        self.insert_threads: list[str] = []
        self.max_update_seconds = 0.0
        self._heartbeat_job: str | None = None
        original_insert = self.app.log.insert
        original_see = self.app.log.see

        def insert(*args, **kwargs):
            self.insert_calls += 1
            self.insert_threads.append(threading.current_thread().name)
            return original_insert(*args, **kwargs)

        def see(*args, **kwargs):
            self.see_calls += 1
            return original_see(*args, **kwargs)

        self.app.log.insert = insert
        self.app.log.see = see

    def tearDown(self) -> None:
        self.app.busy = False
        if self._heartbeat_job is not None:
            try:
                self.root.after_cancel(self._heartbeat_job)
            except tk.TclError:
                pass
            self._heartbeat_job = None
        try:
            self.app.progress.stop()
        except tk.TclError:
            pass
        self.app._stop_ui_drain()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    # --- helpers ---------------------------------------------------------
    def pump_for(self, seconds: float, until=None) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            started = time.monotonic()
            self.root.update()
            self.max_update_seconds = max(self.max_update_seconds, time.monotonic() - started)
            if until is not None and until():
                return True
            time.sleep(0.002)
        return until() if until is not None else True

    def emit_from_worker(
        self,
        count: int,
        template: str = "Docker build progress {index}: ",
        pace_every: int | None = None,
    ) -> threading.Thread:
        def worker() -> None:
            for index in range(count):
                self.app._log(template.format(index=index) + "x" * 60)
                if pace_every and index % pace_every == 0:
                    # 模拟 Docker/PTY 持续输出，让心跳有机会与日志交错。
                    time.sleep(0.001)

        thread = threading.Thread(target=worker, name="log-stress")
        thread.start()
        return thread

    def widget_line_count(self) -> int:
        return int(self.app.log.index("end-1c").split(".")[0])

    def log_text(self) -> str:
        return self.app.log.get("1.0", tk.END)

    # --- 契约：主线程日志仍然立即可读 ------------------------------------
    def test_main_thread_log_is_visible_without_pumping(self) -> None:
        self.app._log("即时可见的一行")
        self.assertIn("即时可见的一行", self.log_text())

    def test_redaction_and_colour_tags_survive_batching(self) -> None:
        self.app._secrets = ["cd2-secret-value"]
        self.app._log("密码 cd2-secret-value", "error")
        self.app._log("普通行")
        text = self.log_text()
        self.assertNotIn("cd2-secret-value", text)
        self.assertIn("***", text)
        self.assertTrue(self.app.log.tag_ranges("error"))

    # --- 有界批量 ---------------------------------------------------------
    def test_drain_processes_a_bounded_batch_per_tick(self) -> None:
        processed: list[int] = []
        for index in range(installer.UI_DRAIN_MAX_CALLBACKS * 4):
            self.app._ui_queue.put(lambda i=index: processed.append(i))
        self.app._stop_ui_drain()
        self.app._drain_ui_queue()
        self.app._stop_ui_drain()
        self.assertEqual(len(processed), installer.UI_DRAIN_MAX_CALLBACKS)
        self.assertFalse(self.app._ui_queue.empty())

    def test_single_log_flush_is_bounded_by_the_batch_size(self) -> None:
        thread = self.emit_from_worker(5000, "批量 {index}: ")
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.app._stop_ui_drain()
        self.app._flush_log_pending()
        self.assertLessEqual(self.widget_line_count(), installer.LOG_FLUSH_MAX_LINES + 3)
        self.assertGreater(self.app._pending_log_count(), 0)

    def test_main_thread_burst_does_not_render_line_by_line(self) -> None:
        for index in range(5000):
            self.app._log(f"主线程 {index}")
        # 额度用完后只排队，主线程不再逐行 insert。
        self.assertLess(self.insert_calls, 100)
        self.assertGreater(self.app._pending_log_count(), 1000)
        self.assertTrue(
            self.pump_for(10.0, until=lambda: self.app._pending_log_count() == 0),
            "主线程积压的日志没有在预算内渲染完",
        )
        self.assertLess(self.insert_calls, 100)
        self.assertIn("主线程 4999", self.log_text())

    # --- 真实压测：心跳不被饿死 ------------------------------------------
    def test_heartbeat_keeps_ticking_while_8000_lines_stream_in(self) -> None:
        beats: list[float] = []
        completed = threading.Event()
        thread = self.emit_from_worker(8000, pace_every=50)

        def worker_tail() -> None:
            thread.join(30)
            self.app._post(completed.set)

        threading.Thread(target=worker_tail, name="log-stress-tail").start()

        def heartbeat() -> None:
            beats.append(time.monotonic())
            if not completed.is_set():
                self._heartbeat_job = self.root.after(20, heartbeat)

        self._heartbeat_job = self.root.after(20, heartbeat)
        finished = self.pump_for(30.0, until=completed.is_set)
        self.assertTrue(finished, "完成回调被日志淹没，主线程事件循环没有回到空闲")
        self.assertGreaterEqual(len(beats), 3)
        gaps = [later - earlier for earlier, later in pairwise(beats)]
        self.assertLess(max(gaps), 0.5, f"心跳被日志阻塞，最长间隔 {max(gaps):.2f}s")
        # 用户看到的“卡死”就是单次事件循环回调长时间不返回。
        self.assertLess(
            self.max_update_seconds,
            0.5,
            f"单次事件循环被阻塞 {self.max_update_seconds:.2f}s",
        )

    def test_completion_callback_is_not_starved_by_100k_line_backlog(self) -> None:
        total = 100_000
        completed = threading.Event()
        thread = self.emit_from_worker(total)

        def worker_tail() -> None:
            thread.join(60)
            self.app._post(completed.set)

        threading.Thread(target=worker_tail, name="log-stress-tail").start()

        started = time.monotonic()
        finished = self.pump_for(20.0, until=completed.is_set)
        elapsed = time.monotonic() - started
        self.assertTrue(finished, f"{total} 行日志后完成回调仍未执行")
        self.assertLess(elapsed, 20.0)
        # 批量渲染：插入次数远少于日志行数，且可见历史有界。
        self.assertLess(self.insert_calls, total // 10)
        self.assertLessEqual(self.widget_line_count(), installer.LOG_HISTORY_MAX_LINES + 3)
        # 不再逐行 see：see() 会同步布局文本，是用户卡死栈里的那一帧。
        self.assertEqual(self.see_calls, 0)

    # --- 有界历史与长单行 -------------------------------------------------
    def test_history_is_trimmed_to_the_cap(self) -> None:
        total = installer.LOG_HISTORY_MAX_LINES + 500
        thread = self.emit_from_worker(total, "历史 {index}")
        thread.join(20)
        self.assertFalse(thread.is_alive())
        self.assertTrue(
            self.pump_for(10.0, until=lambda: self.app._pending_log_count() == 0),
            "日志缓冲没有在预算内消化完",
        )
        self.assertLessEqual(self.widget_line_count(), installer.LOG_HISTORY_MAX_LINES + 3)
        text = self.log_text()
        self.assertIn(f"历史 {total - 1}", text)
        self.assertNotIn("历史 0\n", text)
        # 自动滚动仍然停在末尾（用 yview_moveto 而不是 see）。
        self.assertGreaterEqual(self.app.log.yview()[1], 0.99)

    def test_single_huge_line_is_truncated(self) -> None:
        self.app._log("z" * 200_000)
        text = self.log_text()
        self.assertLessEqual(len(text), installer.LOG_MAX_LINE_CHARS + 200)
        self.assertIn("截断", text)

    # --- 清空与窗口销毁 ---------------------------------------------------
    def test_clear_log_drops_pending_backlog(self) -> None:
        thread = self.emit_from_worker(5000, "待清空 {index}")
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.app._clear_log()
        self.assertEqual(self.app._pending_log_count(), 0)
        self.assertEqual(self.log_text().strip(), "")
        self.pump_for(0.4)
        self.assertEqual(self.log_text().strip(), "")

    def test_background_log_after_window_destroy_is_safe(self) -> None:
        self.app._stop_ui_drain()
        self.root.destroy()
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                self.app._log("窗口销毁后的日志")
            except BaseException as exc:  # noqa: BLE001 - test probe
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    # --- 后台线程绝不直接碰 Tk -------------------------------------------
    def test_worker_log_never_touches_tk_from_the_worker_thread(self) -> None:
        thread = self.emit_from_worker(200, "后台 {index}")
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertTrue(
            self.pump_for(10.0, until=lambda: self.app._pending_log_count() == 0),
            "后台日志没有在预算内渲染",
        )
        self.assertTrue(self.insert_threads)
        self.assertEqual(
            {name for name in self.insert_threads},
            {threading.main_thread().name},
        )


if __name__ == "__main__":
    unittest.main()
